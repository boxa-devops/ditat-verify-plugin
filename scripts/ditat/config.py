"""Configuration management for Ditat API."""

import os
from pathlib import Path

from dotenv import load_dotenv


def _resolve_dotenv_path() -> Path | None:
    """Pick the .env to load.

    Precedence:
      1. $DITAT_DOTENV_PATH (explicit override)
      2. $CLAUDE_PROJECT_DIR/.env (when running as Claude plugin)
      3. ./.env in current working directory
      4. None → let python-dotenv search from caller's __file__ upward (legacy)
    """
    explicit = os.getenv("DITAT_DOTENV_PATH")
    if explicit:
        p = Path(explicit)
        return p if p.is_file() else None
    project = os.getenv("CLAUDE_PROJECT_DIR")
    if project:
        p = Path(project) / ".env"
        if p.is_file():
            return p
    cwd_env = Path.cwd() / ".env"
    if cwd_env.is_file():
        return cwd_env
    return None


class Config:
    """Centralized configuration management."""

    def __init__(self):
        dotenv_path = _resolve_dotenv_path()
        if dotenv_path is not None:
            load_dotenv(dotenv_path)
        else:
            load_dotenv()
        self.base_url = os.getenv("DITAT_BASE_URL", "https://tmsapi01.ditat.net").rstrip("/")
        self.account_id = os.getenv("DITAT_ACCOUNT_ID")
        self.client_id = os.getenv("DITAT_CLIENT_ID")
        self.client_secret = os.getenv("DITAT_CLIENT_SECRET")
        self.max_shipments = int(os.getenv("DITAT_MAX_SHIPMENTS", "3"))
        self.download_dir = Path(os.getenv("DITAT_DOWNLOAD_DIR", "./downloads"))

    PLACEHOLDERS = {"your_account_id", "your_client_id", "your_client_secret"}

    def validate(self) -> tuple[bool, list[str]]:
        """Validate required configuration."""
        missing = []
        for key, value in {
            "DITAT_ACCOUNT_ID": self.account_id,
            "DITAT_CLIENT_ID": self.client_id,
            "DITAT_CLIENT_SECRET": self.client_secret,
        }.items():
            if not value or value in self.PLACEHOLDERS:
                missing.append(key)
        return len(missing) == 0, missing
