#!/usr/bin/env python3
"""Claude Code session-start hook: inject vault index into every session.

Reads _index.md from vault and outputs it as hookSpecificOutput.
Caps at MAX_NOTES rows; warns when truncated.
Zero API calls, pure file I/O, under 1 second.
"""

import json
import os
from pathlib import Path

MAX_NOTES = 50


def main():
    vault_path = os.environ.get("VAULT_PATH", os.path.expanduser("~/coding/SecondBrain"))
    index_path = Path(vault_path) / "_index.md"

    if not index_path.exists():
        return

    try:
        content = index_path.read_text(encoding="utf-8")
    except Exception:
        return

    lines = content.splitlines()

    # Parse total note count from header line "Total: N notes"
    total_notes = None
    for line in lines:
        if line.startswith("Total:"):
            try:
                total_notes = int(line.split()[1])
            except (IndexError, ValueError):
                pass
            break

    # Find table data rows (not header, not separator)
    table_row_indices = [
        i for i, line in enumerate(lines)
        if line.startswith("| ") and "---" not in line and "Title" not in line
    ]

    truncated = len(table_row_indices) > MAX_NOTES

    if truncated:
        cut_at = table_row_indices[MAX_NOTES]
        lines = lines[:cut_at]
        shown = MAX_NOTES
        lines.append(
            f"\n⚠️  ИНДЕКС ОБРЕЗАН: показано {shown} из {total_notes} заметок. "
            f"Варианты: (1) используй `recall` MCP для поиска по vault, "
            f"(2) увеличь MAX_NOTES в secondbrain-session-start.py (сейчас {MAX_NOTES}), "
            f"(3) сократи формат index_generator.py, "
            f"(4) измени порядок сортировки в index_generator.py — приоритетные заметки наверх "
            f"(по кол-ву входящих ссылок 'In' или по дате изменения)."
        )
        content = "\n".join(lines)

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": content,
        }
    }))


if __name__ == "__main__":
    main()
