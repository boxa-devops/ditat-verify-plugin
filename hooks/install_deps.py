#!/usr/bin/env python3
"""SessionStart hook: ensure Python deps in requirements.txt are installed.

Idempotent. Skips work when the marker hash matches. Logs to stderr only —
stdout is reserved for hook protocol output (empty = no-op).
"""

import hashlib
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    plugin_data = os.environ.get("CLAUDE_PLUGIN_DATA")
    if not plugin_root:
        return 0  # Not running as plugin; nothing to do.

    reqs = Path(plugin_root) / "scripts" / "requirements.txt"
    if not reqs.exists():
        return 0

    digest = hashlib.sha256(reqs.read_bytes()).hexdigest()
    if plugin_data:
        marker = Path(plugin_data) / ".deps-hash"
        marker.parent.mkdir(parents=True, exist_ok=True)
        if marker.exists() and marker.read_text().strip() == digest:
            return 0  # Already installed for this requirements hash.
    else:
        marker = None

    print(f"[ditat-verify] installing Python deps from {reqs}", file=sys.stderr)
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "-r", str(reqs)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"[ditat-verify] pip install failed:\n{result.stderr}", file=sys.stderr)
        return 0  # Don't block the session; let user fix manually.

    if marker is not None:
        marker.write_text(digest)
    print("[ditat-verify] deps OK", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
