#!/usr/bin/env python3
"""
audit.py — PostToolUse audit hook.

Appends one JSON line per tool call to .claude/audit.log (gitignored via the
.claude/ conventions). Records a short, secret-free summary: timestamp, tool,
and the target path or a redacted command. Never logs file contents or env
values. Fails open (exit 0) on any error.
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

LOG = Path(__file__).resolve().parent.parent / "audit.log"


def _summary(tool: str, ti: dict) -> str:
    if tool in ("Write", "Edit", "MultiEdit", "Read"):
        return str(ti.get("file_path", ""))
    if tool == "Bash":
        cmd = str(ti.get("command", ""))
        # Redact anything that looks like a token/key just in case.
        cmd = re.sub(r"(shpat_|sk-ant-)[A-Za-z0-9_\-]+", r"\1<redacted>", cmd)
        return cmd[:200]
    return ""


def main() -> None:
    try:
        data = json.load(sys.stdin)
        tool = data.get("tool_name", "")
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "tool": tool,
            "target": _summary(tool, data.get("tool_input", {}) or {}),
        }
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — audit must never break the session
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
