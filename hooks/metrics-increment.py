#!/usr/bin/env python3
"""PostToolUse metrics collector — Phase 9 observability.

Silent hook: writes per-session JSON to ~/.claude/metrics/<sid>.json
using atomic rename. Never prints to stdout (would pollute context).
Never fails the tool call (exit 0 on all errors).

Captured metrics
----------------
* tools[tool_name]: count + approximate output bytes
* violations: list of rule breaches worth flagging in audit
    - read_large_file: Read on file >1000 lines without local-worker
    - mcp_oversize: MCP response >2000 chars (cc-routing candidate)
* repeat_hashes: sha1(tool+args-preview) for repeat-pattern detection
* started_at / last_at timestamps
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

METRICS_DIR = Path(os.path.expanduser("~/.claude/metrics"))
READ_LINE_LIMIT = 1000
MCP_SIZE_LIMIT = 2000


def _preview(value) -> str:
    if isinstance(value, str):
        return value[:200]
    try:
        return json.dumps(value, ensure_ascii=False)[:200]
    except Exception:
        return str(value)[:200]


def _hash(tool_name: str, tool_input) -> str:
    h = hashlib.sha1()
    h.update(tool_name.encode("utf-8", "ignore"))
    h.update(_preview(tool_input).encode("utf-8", "ignore"))
    return h.hexdigest()[:12]


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, delete=False, encoding="utf-8"
    ) as tmp:
        json.dump(data, tmp, ensure_ascii=False)
        tmp_path = tmp.name
    os.replace(tmp_path, path)


def _load_existing(path: Path) -> dict:
    if not path.exists():
        return {"tools": {}, "violations": [], "repeat_hashes": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"tools": {}, "violations": [], "repeat_hashes": {}}


def _output_bytes(tool_response) -> int:
    if isinstance(tool_response, str):
        return len(tool_response.encode("utf-8", "ignore"))
    if isinstance(tool_response, dict):
        content = tool_response.get("content") or tool_response.get("output")
        if content:
            return _output_bytes(content)
    try:
        return len(json.dumps(tool_response, ensure_ascii=False))
    except Exception:
        return 0


def _check_violations(tool_name: str, tool_input, tool_response, out_bytes: int) -> list[str]:
    v: list[str] = []
    if tool_name == "Read" and isinstance(tool_input, dict):
        fp = tool_input.get("file_path", "")
        try:
            if fp and Path(fp).exists() and Path(fp).is_file():
                with open(fp, "rb") as fh:
                    lines = sum(1 for _ in fh)
                if lines > READ_LINE_LIMIT:
                    v.append(f"read_large_file:{fp}:{lines}")
        except Exception:
            pass
    if tool_name.startswith("mcp__") and out_bytes > MCP_SIZE_LIMIT:
        v.append(f"mcp_oversize:{tool_name}:{out_bytes}")
    return v


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except Exception:
        return

    sid = hook_input.get("session_id") or "unknown"
    tool_name = hook_input.get("tool_name") or "unknown"
    tool_input = hook_input.get("tool_input")
    tool_response = hook_input.get("tool_response")

    if sid == "unknown" and tool_name == "unknown":
        return

    # Per-subagent file suffix if we detect a subagent marker
    suffix = ""
    if isinstance(tool_input, dict) and tool_input.get("subagent_type"):
        suffix = f"-{tool_input['subagent_type']}"
    path = METRICS_DIR / f"{sid}{suffix}.json"

    data = _load_existing(path)
    now = time.time()
    data.setdefault("session_id", sid)
    data.setdefault("started_at", now)
    data["last_at"] = now

    tools = data.setdefault("tools", {})
    entry = tools.setdefault(tool_name, {"count": 0, "out_bytes": 0})
    entry["count"] += 1

    out_bytes = _output_bytes(tool_response)
    entry["out_bytes"] += out_bytes

    viols = _check_violations(tool_name, tool_input, tool_response, out_bytes)
    if viols:
        data.setdefault("violations", []).extend(viols[-20:])

    rh = data.setdefault("repeat_hashes", {})
    h = _hash(tool_name, tool_input)
    rh[h] = rh.get(h, 0) + 1

    try:
        _atomic_write(path, data)
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
