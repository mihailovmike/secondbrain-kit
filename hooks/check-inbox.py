#!/usr/bin/env python3
"""Check _inbox for needs_review notes and send Telegram reminder.

Runs as a cron job on VPS. Sends to SecondBrain inbox topic in Miki group.
"""

import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path


VAULT_PATH = os.environ.get("VAULT_PATH", os.path.expanduser("~/SecondBrain"))
INBOX_DIR_NAME = os.environ.get("INBOX_DIR_NAME", "_inbox")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_INBOX_CHAT_ID = os.environ.get("TELEGRAM_INBOX_CHAT_ID", "-1003599063509")
TELEGRAM_INBOX_THREAD_ID = os.environ.get("TELEGRAM_INBOX_THREAD_ID", "2912")


def count_needs_review() -> tuple[int, list[str]]:
    """Count notes with needs_review: true in _inbox."""
    inbox = Path(VAULT_PATH) / INBOX_DIR_NAME
    if not inbox.exists():
        return 0, []

    titles = []
    for md_file in sorted(inbox.glob("*.md")):
        try:
            content = md_file.read_text(encoding="utf-8")
            if "needs_review: true" in content:
                # Extract title from frontmatter
                title = md_file.stem
                for line in content.splitlines():
                    if line.startswith("title:"):
                        title = line.split(":", 1)[1].strip().strip('"')
                        break
                titles.append(title)
        except Exception:
            continue

    return len(titles), titles


def send_telegram(message: str) -> bool:
    """Send message to SecondBrain inbox topic."""
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set", file=sys.stderr)
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_INBOX_CHAT_ID,
        "message_thread_id": TELEGRAM_INBOX_THREAD_ID,
        "text": message,
        "parse_mode": "HTML",
    }
    data = urllib.parse.urlencode(payload).encode("utf-8")

    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"ERROR: Telegram send failed: {e}", file=sys.stderr)
        return False


def main():
    count, titles = count_needs_review()

    if count == 0:
        print("No notes need review.")
        return

    lines = [f"📥 <b>{count} заметок ждут разбора</b> в SecondBrain _inbox\n"]
    for t in titles[:5]:
        lines.append(f"• {t}")
    if count > 5:
        lines.append(f"• ...и ещё {count - 5}")
    lines.append("\nОткрой сессию в secondbrain-engine и разбери их.")

    message = "\n".join(lines)
    ok = send_telegram(message)
    if ok:
        print(f"Sent: {count} notes need review.")
    else:
        print(f"Failed to send. {count} notes need review.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
