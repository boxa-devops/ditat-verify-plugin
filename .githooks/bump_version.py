#!/usr/bin/env python3
"""Bump the patch version in .claude-plugin/plugin.json, then stage it.

Invoked by the pre-commit hook so every commit ships a new version — that is
what makes `/plugin update` detect fresh code (the marketplace keys cache
refresh off the version string).

Edits the version line with a regex rather than json.load/dump so the file's
formatting (compact keyword arrays, etc.) is preserved byte-for-byte.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN_JSON = REPO / ".claude-plugin" / "plugin.json"

_VERSION_RE = re.compile(r'("version"\s*:\s*")(\d+)\.(\d+)\.(\d+)(")')


def main() -> int:
    if not PLUGIN_JSON.is_file():
        print(f"bump_version: {PLUGIN_JSON} not found — skipping", file=sys.stderr)
        return 0

    text = PLUGIN_JSON.read_text(encoding="utf-8")
    m = _VERSION_RE.search(text)
    if not m:
        print("bump_version: no semver version field found — skipping", file=sys.stderr)
        return 0

    major, minor, patch = int(m.group(2)), int(m.group(3)), int(m.group(4))
    new_version = f"{major}.{minor}.{patch + 1}"
    new_text = text[: m.start()] + f"{m.group(1)}{new_version}{m.group(5)}" + text[m.end():]
    PLUGIN_JSON.write_text(new_text, encoding="utf-8")

    # Stage the bump so it lands in the commit being created.
    subprocess.run(["git", "add", str(PLUGIN_JSON)], cwd=REPO, check=True)
    print(f"bump_version: {major}.{minor}.{patch} -> {new_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
