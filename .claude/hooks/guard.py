#!/usr/bin/env python3
"""
guard.py — PreToolUse guard hook.

Blocks (exit code 2 = deny + show message to the model):
  - Writing/editing the .env secrets file.
  - Staging or committing .env or config_local.py (secrets / client data).
  - Printing the raw .env to a terminal (cat/type/Get-Content .env).

Reads the tool-call JSON from stdin. Fails OPEN (exit 0) on any parsing error so
a hook bug never wedges the session — the guard is defence-in-depth, not the
only control (.gitignore already excludes these paths).
"""

import json
import re
import sys


def deny(reason: str) -> None:
    print(f"[guard] BLOCKED: {reason}", file=sys.stderr)
    sys.exit(2)


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)  # fail open

    tool = data.get("tool_name", "")
    ti = data.get("tool_input", {}) or {}

    if tool in ("Write", "Edit", "MultiEdit"):
        path = str(ti.get("file_path", "")).replace("\\", "/").lower()
        base = path.rsplit("/", 1)[-1]
        if base == ".env":
            deny("editing .env — put secrets in .env manually, never via the agent.")

    if tool == "Bash":
        cmd = str(ti.get("command", ""))
        low = cmd.lower()
        if re.search(r"git\s+(add|commit).*(\.env\b|config_local\.py)", low):
            deny("staging/committing a secret or client-data file "
                 "(.env / config_local.py). These are gitignored — do not force them in.")
        # Block READING/printing .env (cat/type/Get-Content .env). Exclude '>'
        # from the gap so a redirected WRITE (cat > .env) is NOT caught.
        if re.search(r"\b(cat|type|get-content)\b[^>|\n]*\.env\b", low):
            deny("printing .env — secret values must never be echoed to the terminal.")

    sys.exit(0)


if __name__ == "__main__":
    main()
