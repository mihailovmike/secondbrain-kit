"""Input quality gate: reject garbage before it enters the pipeline.

Layers:
  L1: File-level dedup (hash) — same file never processed twice
  L2: Size gate — too short or too long
  L3: Content quality — reject code dumps, logs, binary
  L4: (removed — was Qdrant cosine dedup, now handled by LightRAG)
  L5: Title dedup — same slug already exists in vault root

Each layer returns (pass: bool, reason: str).
"""

import hashlib
import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

VAULT_PATH = os.getenv("VAULT_PATH", "/app/vault")
MIN_WORDS = 20
MAX_WORDS = 5000
CODE_LINE_THRESHOLD = 0.5  # reject if >50% lines look like code/logs

_processed_hashes: set[str] = set()
_HASH_FILE = os.path.join(VAULT_PATH, ".processed_hashes.json")

# Patterns that indicate code/logs/junk
_CODE_PATTERNS = re.compile(
    r'^\s*(import |from |def |class |function |const |let |var |'
    r'return |if \(|for \(|while \(|\{|\}|<[a-zA-Z]|'
    r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}|'  # timestamps in logs
    r'ERROR|WARNING|DEBUG|INFO|TRACE|'
    r'Traceback|File "|Exception|'
    r'^\s*\d+\.\d+\.\d+\.\d+|'  # IP addresses
    r'HTTP/\d|GET /|POST /|'
    r'^[{}\[\]<>]+$)',  # pure brackets
    re.MULTILINE
)


def _load_hashes() -> None:
    """Load processed file hashes from disk."""
    global _processed_hashes
    try:
        path = Path(_HASH_FILE)
        if path.exists():
            _processed_hashes = set(json.loads(path.read_text()))
    except Exception:
        _processed_hashes = set()


def _save_hashes() -> None:
    """Persist processed file hashes to disk."""
    try:
        path = Path(_HASH_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(_processed_hashes)))
    except Exception as e:
        logger.warning(f"Could not save hash file: {e}")


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _word_count(text: str) -> int:
    return len(text.split())


def _code_line_ratio(text: str) -> float:
    """What fraction of lines match code/log patterns."""
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return 0.0
    code_lines = sum(1 for l in lines if _CODE_PATTERNS.search(l))
    return code_lines / len(lines)


def _log_rejection(file_path: str, reason: str) -> None:
    """Append rejection to log file."""
    log_path = Path(VAULT_PATH) / "rejected.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    entry = f"{ts} | {reason} | {file_path}\n"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(entry)


def check_file_hash(text: str) -> tuple[bool, str]:
    """L1: File-level dedup. Returns (pass, reason)."""
    if not _processed_hashes:
        _load_hashes()
    h = _hash_text(text)
    if h in _processed_hashes:
        return False, f"file_hash_duplicate:{h}"
    return True, ""


def mark_processed(text: str) -> None:
    """Mark file hash as processed after successful pipeline completion."""
    h = _hash_text(text)
    _processed_hashes.add(h)
    _save_hashes()


def check_size(text: str) -> tuple[bool, str]:
    """L2: Size gate. Returns (pass, reason)."""
    wc = _word_count(text)
    if wc < MIN_WORDS:
        return False, f"too_short:{wc}_words"
    if wc > MAX_WORDS:
        return False, f"too_long:{wc}_words_needs_split"
    return True, ""


def check_content_quality(text: str) -> tuple[bool, str]:
    """L3: Content quality. Returns (pass, reason)."""
    ratio = _code_line_ratio(text)
    if ratio > CODE_LINE_THRESHOLD:
        return False, f"code_or_logs:{ratio:.0%}_code_lines"

    # Check for binary/non-text content
    non_printable = sum(1 for c in text[:1000] if ord(c) < 32 and c not in '\n\r\t')
    if non_printable > 10:
        return False, "binary_content"

    return True, ""


def check_title_exists(slug: str, folder: str = "inbox") -> tuple[bool, str]:
    """L5: Title dedup — check if note with same slug already exists in any vault folder."""
    vault = Path(VAULT_PATH)
    # Check target folder first
    target = vault / folder / f"{slug}.md"
    if target.exists():
        return False, f"title_exists:{target.relative_to(vault)}"
    # Also check all other folders (global uniqueness)
    for match in vault.rglob(f"{slug}.md"):
        if match.exists():
            return False, f"title_exists:{match.relative_to(vault)}"
    return True, ""


def run_all_gates(text: str, file_path: str = "") -> tuple[bool, str]:
    """Run L1-L3 gates. Returns (pass, reason).
    L5 (title dedup) is checked after LLM generates title.
    """
    ok, reason = check_file_hash(text)
    if not ok:
        _log_rejection(file_path, reason)
        logger.info(f"GATE REJECT [{reason}]: {file_path}")
        return False, reason

    ok, reason = check_size(text)
    if not ok:
        _log_rejection(file_path, reason)
        logger.info(f"GATE REJECT [{reason}]: {file_path}")
        return False, reason

    ok, reason = check_content_quality(text)
    if not ok:
        _log_rejection(file_path, reason)
        logger.info(f"GATE REJECT [{reason}]: {file_path}")
        return False, reason

    return True, ""
