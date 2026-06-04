"""Send the verification report to a Telegram channel/group.

Uses the Telegram Bot API (no extra deps — just `requests`). Configured via env:
  TELEGRAM_BOT_TOKEN   — the bot token from @BotFather
  TELEGRAM_CHAT_ID     — channel/group id (e.g. -1001234567890) or @channelusername

The bot must be a member/admin of the target channel/group.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger("ditat")

_API = "https://api.telegram.org/bot{token}/{method}"
_MSG_LIMIT = 4096
_CAPTION_LIMIT = 1024


def config_from_env() -> tuple[Optional[str], Optional[str]]:
    """(bot_token, chat_id) from env. dotenv is loaded by remote.ServerConfig, but
    load it here too so `finalize` sees the values even if it ran standalone."""
    try:
        from .remote import _load_dotenv
        _load_dotenv()
    except Exception:  # noqa: BLE001
        pass
    return os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")


def is_configured() -> bool:
    token, chat_id = config_from_env()
    return bool(token and chat_id)


def send_message(token: str, chat_id: str, text: str) -> bool:
    resp = requests.post(
        _API.format(token=token, method="sendMessage"),
        data={"chat_id": chat_id, "text": text[:_MSG_LIMIT], "parse_mode": "HTML"},
        timeout=30,
    )
    if resp.status_code != 200:
        log.warning("Telegram sendMessage failed %s: %s", resp.status_code, resp.text[:300])
        return False
    return True


def send_document(token: str, chat_id: str, path: Path, caption: Optional[str] = None) -> bool:
    with open(path, "rb") as fh:
        resp = requests.post(
            _API.format(token=token, method="sendDocument"),
            data={"chat_id": chat_id,
                  "caption": (caption or "")[:_CAPTION_LIMIT],
                  "parse_mode": "HTML"},
            files={"document": (path.name, fh)},
            timeout=120,
        )
    if resp.status_code != 200:
        log.warning("Telegram sendDocument failed %s: %s", resp.status_code, resp.text[:300])
        return False
    return True


def send_report(summary: str, docx_path: Path,
                token: Optional[str] = None, chat_id: Optional[str] = None) -> dict:
    """Send the summary message + the .docx. Returns a small status dict."""
    if token is None or chat_id is None:
        token, chat_id = config_from_env()
    if not token or not chat_id:
        return {"sent": False, "reason": "not configured"}
    msg_ok = send_message(token, chat_id, summary)
    doc_ok = send_document(token, chat_id, docx_path, caption="Ditat verification report")
    return {"sent": msg_ok and doc_ok, "message": msg_ok, "document": doc_ok}
