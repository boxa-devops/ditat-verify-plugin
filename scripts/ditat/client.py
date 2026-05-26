"""OAuth 2.0 HTTP client for Ditat API."""

import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import requests

log = logging.getLogger("ditat")


class TokenFetchLimitExceeded(RuntimeError):
    """Raised when client refuses to fetch more tokens to avoid hitting rate limit."""


class DitatClient:
    """OAuth client with disk-cached tokens and hard fetch cap.

    Ditat enforces a sliding 60-minute, 12-fetch limit on the token endpoint.
    Cross-process token sharing + per-process cap protect that budget.
    """

    # Hard cap on token fetches per client instance. Normal run needs 1, refresh adds 1.
    MAX_TOKEN_FETCHES = 3

    # Minimum age of current token before a 401 is treated as expiry (vs permissions).
    MIN_TOKEN_AGE_FOR_401_REAUTH = timedelta(seconds=60)

    def __init__(self, base_url: str, account_id: str, client_id: str, client_secret: str):
        self.base_url = base_url
        self.account_id = account_id
        self.client_id = client_id
        self.client_secret = client_secret

        self._token: Optional[str] = None
        self._token_expires_at: Optional[datetime] = None
        self._token_acquired_at: Optional[datetime] = None
        self._token_fetch_count: int = 0
        self._token_lock = threading.Lock()
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})
        self._cache_path = self._compute_cache_path()
        self._load_cached_token()

    def _compute_cache_path(self) -> Path:
        """Per-credential token cache. Resolved from env first so plugin installs
        don't try to write into a read-only / update-wiped plugin dir.

        Precedence:
          1. $DITAT_TOKEN_CACHE_DIR
          2. $CLAUDE_PROJECT_DIR  (when invoked as part of a Claude plugin)
          3. parent of the `ditat/` package dir  (legacy local-checkout layout)
        """
        ident = f"{self.base_url}|{self.account_id}|{self.client_id}"
        digest = hashlib.sha256(ident.encode("utf-8")).hexdigest()[:16]
        root = (
            os.getenv("DITAT_TOKEN_CACHE_DIR")
            or os.getenv("CLAUDE_PROJECT_DIR")
        )
        base = Path(root).resolve() if root else Path(__file__).resolve().parent.parent
        return base / f".ditat_token_{digest}.json"

    def _load_cached_token(self) -> None:
        """Load token from disk cache if still valid."""
        try:
            if not self._cache_path.exists():
                return
            data = json.loads(self._cache_path.read_text())
            expires_at = datetime.fromisoformat(data["expires_at"])
            # Require at least 5 min remaining
            if expires_at - datetime.now(timezone.utc) < timedelta(minutes=5):
                return
            self._token = data["token"]
            self._token_expires_at = expires_at
            self._token_acquired_at = datetime.fromisoformat(
                data.get("acquired_at", expires_at.isoformat())
            )
            log.info("Loaded cached token from %s (expires %s)",
                     self._cache_path, expires_at.isoformat())
        except Exception as e:
            log.debug("Token cache load failed: %s", e)

    def _save_cached_token(self) -> None:
        """Persist token to disk cache atomically."""
        try:
            payload = {
                "token": self._token,
                "expires_at": self._token_expires_at.isoformat() if self._token_expires_at else None,
                "acquired_at": self._token_acquired_at.isoformat() if self._token_acquired_at else None,
            }
            tmp = self._cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload))
            os.replace(tmp, self._cache_path)
            try:
                os.chmod(self._cache_path, 0o600)
            except OSError:
                pass  # Windows may not honor
        except Exception as e:
            log.warning("Token cache save failed: %s", e)

    def _token_endpoint(self) -> str:
        return f"{self.base_url}/identity-provider/{self.account_id}/connect/token"

    def _fetch_token(self) -> None:
        """Fetch a new access token. Bounded by MAX_TOKEN_FETCHES per instance."""
        if self._token_fetch_count >= self.MAX_TOKEN_FETCHES:
            raise TokenFetchLimitExceeded(
                f"Refusing to fetch token: already fetched {self._token_fetch_count} "
                f"in this process (cap={self.MAX_TOKEN_FETCHES}). Ditat allows ~12/hour. "
                f"Wait for the sliding window to reset before retrying."
            )

        self._token_fetch_count += 1
        url = self._token_endpoint()
        log.info("Requesting access token from %s (fetch %d/%d)",
                 url, self._token_fetch_count, self.MAX_TOKEN_FETCHES)
        resp = self._session.post(
            url,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        if resp.status_code != 200:
            log.error("Token request failed: %s — %s", resp.status_code, resp.text[:500])
            resp.raise_for_status()

        body = resp.json()
        self._token = body.get("access_token")
        expires_in = int(body.get("expires_in", 3000))
        now = datetime.now(timezone.utc)
        self._token_acquired_at = now
        self._token_expires_at = now + timedelta(seconds=expires_in)
        log.info("Token acquired (expires in ~%ds)", expires_in)
        self._save_cached_token()

    def _ensure_token(self) -> str:
        """Ensure a valid token exists, refreshing if needed.

        Thread-safe: serialized via `_token_lock` so parallel workers detecting
        an expired token won't both call `_fetch_token` and burn the 12/hour budget.
        """
        with self._token_lock:
            if self._token is None:
                self._fetch_token()
            elif self._token_expires_at and (
                self._token_expires_at - datetime.now(timezone.utc) < timedelta(minutes=5)
            ):
                log.info("Token nearing expiration — refreshing")
                self._fetch_token()
            return self._token  # type: ignore[return-value]

    def _token_age(self) -> Optional[timedelta]:
        """Age of currently-held token, if any."""
        if self._token_acquired_at is None:
            return None
        return datetime.now(timezone.utc) - self._token_acquired_at

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict | None = None,
        stream: bool = False,
    ) -> requests.Response:
        """Make a request with token reuse, conditional 401 retry, 429 backoff."""
        url = f"{self.base_url}/{path.lstrip('/')}"
        backoff = 1.0
        max_backoff = 60.0
        attempts = 0
        max_attempts = 6
        reauth_attempted = False

        while True:
            attempts += 1
            token = self._ensure_token()
            headers = {"Authorization": f"Bearer {token}"}
            if json_body is not None:
                headers["Content-Type"] = "application/json"

            resp = self._session.request(
                method,
                url,
                headers=headers,
                json=json_body,
                params=params,
                stream=stream,
                timeout=60,
            )

            remaining = resp.headers.get("X-RateLimit-Remaining")
            if remaining is not None:
                log.debug("X-RateLimit-Remaining=%s", remaining)

            # 401: only reauth if token is old enough that expiry is plausible.
            # A 401 on a fresh token is a permissions issue, not expiry — don't burn token budget.
            if resp.status_code == 401 and not reauth_attempted:
                age = self._token_age()
                if age is None or age >= self.MIN_TOKEN_AGE_FOR_401_REAUTH:
                    log.warning("401 with token age=%s — refreshing token and retrying once", age)
                    with self._token_lock:
                        if self._token == token:
                            self._token = None
                    reauth_attempted = True
                    continue
                else:
                    log.warning(
                        "401 on fresh token (age=%s) — likely permissions, NOT refreshing",
                        age,
                    )
                    return resp

            if resp.status_code == 429:
                if attempts >= max_attempts:
                    log.error("Hit 429 too many times — giving up on %s", path)
                    return resp
                sleep_for = min(backoff, max_backoff)
                log.warning("429 rate-limited — sleeping %.1fs (attempt %d)", sleep_for, attempts)
                time.sleep(sleep_for)
                backoff *= 2
                continue

            return resp

    def get(self, path: str, **kwargs) -> requests.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> requests.Response:
        return self.request("POST", path, **kwargs)
