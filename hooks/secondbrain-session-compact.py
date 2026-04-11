#!/usr/bin/env python3
"""Claude Code pre-compact hook: capture context before auto-compaction.

Same as session-end but fires before context window auto-compacts.
Minimum 5 turns threshold to avoid noise from short compactions.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def main():
    if os.environ.get("CLAUDE_INVOKED_BY"):
        return

    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        return

    session_id = hook_input.get("session_id", "unknown")
    transcript_path = hook_input.get("transcript_path")
    cwd = hook_input.get("cwd", "")

    if not transcript_path or not Path(transcript_path).exists():
        return

    turns = []
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    msg = entry.get("message", {})
                    if not isinstance(msg, dict):
                        continue
                    role = msg.get("role", "")
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        texts = []
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                texts.append(block.get("text", ""))
                            elif isinstance(block, str):
                                texts.append(block)
                        content = "\n".join(texts)
                    if role and content:
                        turns.append({"role": role, "content": str(content)[:2000]})
                except json.JSONDecodeError:
                    continue
    except Exception:
        return

    if len(turns) < 5:
        return

    turns = turns[-30:]
    total = 0
    kept = []
    for t in reversed(turns):
        size = len(t["content"])
        if total + size > 15000:
            break
        kept.insert(0, t)
        total += size

    if not kept:
        return

    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M")
    project = Path(cwd).name if cwd else "unknown"

    conversation = []
    for t in kept:
        prefix = "User" if t["role"] == "user" else "Claude"
        conversation.append(f"**{prefix}:** {t['content']}")

    body = "\n\n".join(conversation)

    note = f"""---
title: "Compact: {project} ({date_str} {time_str})"
type: source
tags: [session, compact, {project}]
created: {date_str}
source: claude-compact
confidence: 0.7
---

# Compact: {project} ({date_str} {time_str})

Project: {cwd}
Session: {session_id[:8]}
Reason: pre-compact capture

{body}
"""

    vault_path = os.environ.get("VAULT_PATH", os.path.expanduser("~/coding/SecondBrain"))
    inbox = Path(vault_path) / os.environ.get("INBOX_DIR_NAME", "_inbox")
    inbox.mkdir(parents=True, exist_ok=True)

    filename = f"compact-{date_str}-{session_id[:8]}.md"
    (inbox / filename).write_text(note, encoding="utf-8")


if __name__ == "__main__":
    main()
