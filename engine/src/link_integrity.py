"""Link integrity: detect deleted notes and clean broken [[wiki-links]] across vault."""

import hashlib
import json
import logging
import os
import re
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

VAULT_PATH = os.getenv("VAULT_PATH", "/app/vault")
INBOX_DIR_NAME = os.getenv("INBOX_DIR_NAME", "_inbox")
_SKIP_DIRS = {"templates", ".obsidian", ".git", ".lightrag", ".entire", ".trash"}

WIKI_LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
LINKS_SECTION_LINE_RE = re.compile(r"^\s*-\s*\[\[([^\]]+)\]\]\s*$")


# --- Title index: persistent {doc_id: title} map ---

def _title_index_path() -> Path:
    return Path(VAULT_PATH) / ".title_index.json"


def load_title_index() -> dict[str, str]:
    p = _title_index_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_title_index(index: dict[str, str]) -> None:
    _title_index_path().write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")


def register_title(doc_id: str, title: str) -> None:
    index = load_title_index()
    index[doc_id] = title
    save_title_index(index)


def rebuild_title_index(vault_path: str) -> dict[str, str]:
    """Full rebuild: scan vault, compute doc_id from body hash, extract title from frontmatter."""
    from .lightrag_engine import strip_frontmatter

    vault = Path(vault_path)
    index: dict[str, str] = {}

    for d in vault.iterdir():
        if not d.is_dir() or d.name in _SKIP_DIRS or d.name.startswith(".") or d.name == INBOX_DIR_NAME:
            continue
        for f in d.rglob("*.md"):
            try:
                raw = f.read_text(encoding="utf-8").strip()
                body = strip_frontmatter(raw)
                if not body or len(body) < 20:
                    continue
                doc_id = f"doc-{hashlib.md5(body.encode()).hexdigest()}"
                # Extract title from YAML frontmatter
                title = _extract_title(raw, f)
                if title:
                    index[doc_id] = title
            except Exception:
                continue

    save_title_index(index)
    logger.info("Title index rebuilt: %d entries", len(index))
    return index


def _extract_title(raw: str, file_path: Path) -> str:
    """Extract title from YAML frontmatter, fall back to filename."""
    if raw.startswith("---"):
        end = raw.find("---", 3)
        if end != -1:
            try:
                meta = yaml.safe_load(raw[3:end])
                if isinstance(meta, dict) and meta.get("title"):
                    return meta["title"].strip('"').strip("'")
            except Exception:
                pass
    return file_path.stem.replace("-", " ").replace("_", " ")


# --- Broken link scan ---

def _vault_md_files(vault_path: str):
    """Yield all .md files in vault (excluding skip dirs and inbox)."""
    vault = Path(vault_path)
    for d in vault.iterdir():
        if not d.is_dir() or d.name in _SKIP_DIRS or d.name.startswith(".") or d.name == INBOX_DIR_NAME:
            continue
        yield from d.rglob("*.md")


def scan_broken_links(vault_path: str, deleted_titles: list[str]) -> dict[str, list[str]]:
    """Find all vault files containing [[wiki-links]] to deleted titles.

    Returns: {file_path_str: [matched_title, ...]}
    """
    if not deleted_titles:
        return {}

    lower_titles = {t.lower(): t for t in deleted_titles}
    broken: dict[str, list[str]] = {}

    for f in _vault_md_files(vault_path):
        try:
            content = f.read_text(encoding="utf-8")
        except Exception:
            continue
        found = []
        for match in WIKI_LINK_RE.finditer(content):
            link_text = match.group(1).strip()
            if link_text.lower() in lower_titles:
                found.append(lower_titles[link_text.lower()])
        if found:
            broken[str(f)] = list(set(found))

    return broken


# --- Link cleanup ---

def clean_broken_links(broken_map: dict[str, list[str]]) -> int:
    """Remove broken wiki-links from vault files. Returns total links cleaned."""
    total = 0

    for file_path, titles in broken_map.items():
        try:
            content = Path(file_path).read_text(encoding="utf-8")
        except Exception:
            continue

        original = content
        cleaned_count = 0

        for title in titles:
            escaped = re.escape(title)

            # Remove lines in Links/References sections: `- [[Title]]`
            pattern = re.compile(rf"^\s*-\s*\[\[{escaped}\]\]\s*\n?", re.MULTILINE | re.IGNORECASE)
            content, n = pattern.subn("", content)
            cleaned_count += n

            # Replace remaining inline [[Title]] with plain Title
            inline_pattern = re.compile(rf"\[\[{escaped}\]\]", re.IGNORECASE)
            content, n2 = inline_pattern.subn(title, content)
            cleaned_count += n2

        # Remove empty ## Links section (header with no list items after it)
        content = re.sub(r"\n## Links\s*\n(?=\n|## |\Z)", "\n", content)

        if content != original:
            Path(file_path).write_text(content, encoding="utf-8")
            logger.info("Cleaned %d broken links in %s", cleaned_count, file_path)
            total += cleaned_count

    return total


# --- Telegram notification ---

def notify_deleted(deleted_titles: list[str], link_count: int) -> bool:
    """Send Telegram notification about deleted notes and cleaned links."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.debug("TELEGRAM_BOT_TOKEN not set, skipping notification")
        return False

    chat_id = os.getenv("TELEGRAM_DM_CHAT_ID")
    if not chat_id:
        try:
            with open(Path(__file__).parent.parent / "config.yaml", encoding="utf-8") as fh:
                cfg = yaml.safe_load(fh)
            chat_id = cfg.get("notifications", {}).get("telegram_dm_chat_id")
        except Exception:
            pass

    if not chat_id:
        logger.warning("telegram_dm_chat_id not found in config.yaml")
        return False

    titles_str = ", ".join(f"\u00ab{t}\u00bb" for t in deleted_titles)
    text = f"\U0001f5d1 Deleted: {titles_str}. Cleaned links: {link_count}."

    try:
        import httpx
        resp = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.warning("Telegram notification failed: %s", e)
        return False


# --- Orchestrator ---

def run_link_integrity(vault_path: str, deleted_doc_ids: list[str]) -> dict:
    """Main entry point: resolve titles, scan, clean, notify.

    Called after sync_with_vault detects deleted docs.
    """
    index = load_title_index()
    if not index:
        logger.info("Title index empty, rebuilding...")
        index = rebuild_title_index(vault_path)

    # Resolve doc_ids to titles
    deleted_titles = []
    for doc_id in deleted_doc_ids:
        title = index.pop(doc_id, None)
        if title:
            deleted_titles.append(title)
        else:
            logger.debug("No title found for doc_id %s", doc_id)

    if not deleted_titles:
        logger.info("Link integrity: no titles resolved for %d deleted docs", len(deleted_doc_ids))
        save_title_index(index)
        return {"deleted_titles": [], "files_cleaned": 0, "links_removed": 0}

    # Save updated index (deleted entries removed)
    save_title_index(index)

    # Scan and clean
    broken_map = scan_broken_links(vault_path, deleted_titles)
    links_removed = clean_broken_links(broken_map) if broken_map else 0

    # Notify
    notify_deleted(deleted_titles, links_removed)

    logger.info(
        "Link integrity: deleted %s, cleaned %d links in %d files",
        deleted_titles, links_removed, len(broken_map),
    )
    return {
        "deleted_titles": deleted_titles,
        "files_cleaned": len(broken_map),
        "links_removed": links_removed,
    }
