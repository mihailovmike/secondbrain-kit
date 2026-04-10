"""Path sync: auto-update [[wiki-links]] when notes are renamed, moved, or retitled.

Detection sources:
- Periodic scan: compare frontmatter cache with vault state (works after git pull)
- Watchdog events: real-time move/modify detection (works with direct edits)

On detection:
1. Update [[wiki-links]] across all vault notes
2. Re-insert into LightRAG with correct file_path
3. Send Telegram notification
"""

import hashlib
import json
import logging
import os
import re
import urllib.request
import urllib.parse
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

VAULT_PATH = os.getenv("VAULT_PATH", "/app/vault")
INBOX_DIR_NAME = os.getenv("INBOX_DIR_NAME", "_inbox")
_SKIP_DIRS = {"templates", ".obsidian", ".git", ".lightrag", ".entire", ".trash"}
_CACHE_FILE = os.path.join(VAULT_PATH, ".frontmatter_cache.json")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_DM_CHAT_ID", "")


# ---------------------------------------------------------------------------
# Frontmatter cache
# ---------------------------------------------------------------------------

class FrontmatterCache:
    """Persistent cache: {relative_path: {title, body_hash}}.

    Used to detect title changes and match renames by content.
    """

    def __init__(self, cache_file: str = _CACHE_FILE):
        self._file = cache_file
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self):
        try:
            p = Path(self._file)
            if p.exists():
                self._data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            self._data = {}

    def _save(self):
        try:
            Path(self._file).write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("Could not save frontmatter cache: %s", e)

    def get(self, rel_path: str) -> dict | None:
        return self._data.get(rel_path)

    def set(self, rel_path: str, title: str, body_hash: str):
        self._data[rel_path] = {"title": title, "body_hash": body_hash}
        self._save()

    def remove(self, rel_path: str):
        if rel_path in self._data:
            self._data.pop(rel_path)
            self._save()

    def all(self) -> dict[str, dict]:
        return dict(self._data)

    def titles_map(self) -> dict[str, str]:
        """Return {title: rel_path} for conflict detection."""
        return {v["title"]: k for k, v in self._data.items() if v.get("title")}

    def build(self, vault_path: str):
        """Full scan of vault to build/rebuild cache."""
        self._data = {}
        vault = Path(vault_path)
        for entry in _iter_vault_notes(vault):
            rel = str(entry.relative_to(vault))
            title, body_hash = _read_note_meta(entry)
            if title:
                self._data[rel] = {"title": title, "body_hash": body_hash}
        self._save()
        logger.info("Frontmatter cache built: %d entries", len(self._data))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iter_vault_notes(vault: Path):
    """Yield all .md files in vault, skipping system dirs and inbox."""
    for d in vault.iterdir():
        if not d.is_dir():
            # Root-level .md files
            if d.suffix == ".md" and not d.name.startswith("."):
                yield d
            continue
        if d.name in _SKIP_DIRS or d.name.startswith(".") or d.name == INBOX_DIR_NAME:
            continue
        for f in d.rglob("*.md"):
            if not f.name.startswith("."):
                yield f


def _read_note_meta(path: Path) -> tuple[str, str]:
    """Extract (title, body_hash) from a note file."""
    try:
        content = path.read_text(encoding="utf-8")
    except Exception:
        return "", ""
    title = _extract_title(content)
    body_hash = _body_hash(content)
    return title, body_hash


def _extract_title(content: str) -> str:
    """Get title from YAML frontmatter."""
    if not content.startswith("---"):
        return ""
    end = content.find("---", 3)
    if end == -1:
        return ""
    try:
        meta = yaml.safe_load(content[3:end])
        return (meta or {}).get("title", "")
    except Exception:
        return ""


def _body_hash(content: str) -> str:
    """Hash content body (without frontmatter) for rename matching."""
    body = content
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            body = content[end + 3:]
    return hashlib.sha256(body.strip().encode("utf-8")).hexdigest()[:16]


def _is_vault_note(path: str, vault_path: str) -> bool:
    """Check if path is a valid vault note (not in skip dirs, not inbox).

    Only accepts files inside subdirectories (knowledge/, goals/, etc.).
    Root-level files like CLAUDE.md are system files, not notes.
    """
    if not path.endswith(".md"):
        return False
    try:
        rel = os.path.relpath(path, vault_path)
    except ValueError:
        return False
    parts = Path(rel).parts
    # Must be inside a subdirectory, not root-level
    if len(parts) < 2:
        return False
    if any(p in _SKIP_DIRS or p.startswith(".") for p in parts):
        return False
    if parts and parts[0] == INBOX_DIR_NAME:
        return False
    return True


# ---------------------------------------------------------------------------
# Wiki-link update
# ---------------------------------------------------------------------------

def update_wiki_links(old_title: str, new_title: str, vault_path: str) -> int:
    """Replace [[old_title...]] with [[new_title...]] in all vault notes.

    Handles:
    - [[Old Title]]              -> [[New Title]]
    - [[Old Title|display text]] -> [[New Title|display text]]
    - [[Old Title#heading]]      -> [[New Title#heading]]
    - [[Old Title#h|display]]    -> [[New Title#h|display]]

    Returns number of files modified.
    """
    if not old_title or not new_title or old_title == new_title:
        return 0

    # Pattern: [[Old Title followed by optional #/| suffix then ]]
    pattern = re.compile(
        r"\[\[" + re.escape(old_title) + r"((?:[#|][^\]]*)?)\]\]"
    )

    vault = Path(vault_path)
    modified_count = 0

    for note_path in _iter_vault_notes(vault):
        try:
            content = note_path.read_text(encoding="utf-8")
        except Exception:
            continue

        if f"[[{old_title}" not in content:
            continue

        new_content = pattern.sub(
            lambda m: f"[[{new_title}{m.group(1)}]]", content
        )

        if new_content != content:
            note_path.write_text(new_content, encoding="utf-8")
            modified_count += 1
            logger.info(
                "Updated wiki-links in %s: [[%s]] -> [[%s]]",
                note_path.relative_to(vault), old_title, new_title,
            )

    return modified_count


# ---------------------------------------------------------------------------
# LightRAG re-index
# ---------------------------------------------------------------------------

def _reindex_in_lightrag(file_path: Path, vault_path: str):
    """Re-insert a note into LightRAG with correct file_path metadata."""
    try:
        from .lightrag_engine import insert as lightrag_insert

        content = file_path.read_text(encoding="utf-8")
        rel_path = str(file_path.relative_to(Path(vault_path)))
        lightrag_insert(content, file_path=rel_path)
        logger.info("LightRAG re-indexed: %s", rel_path)
    except Exception as e:
        logger.warning("LightRAG re-index failed for %s: %s", file_path, e)


# ---------------------------------------------------------------------------
# Telegram notification
# ---------------------------------------------------------------------------

def _notify_telegram(message: str):
    """Send notification to Telegram (non-blocking, best-effort)."""
    if not TELEGRAM_BOT_TOKEN:
        logger.debug("Telegram notification skipped (no bot token)")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }).encode("utf-8")

    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                logger.warning("Telegram notification failed: HTTP %d", resp.status)
    except Exception as e:
        logger.warning("Telegram notification failed: %s", e)


# ---------------------------------------------------------------------------
# Event handlers (called from watcher)
# ---------------------------------------------------------------------------

def handle_move(src_path: str, dest_path: str, cache: FrontmatterCache):
    """Handle file rename/move detected by watchdog or periodic sync.

    Args:
        src_path: absolute old path
        dest_path: absolute new path
        cache: frontmatter cache instance
    """
    vault = Path(VAULT_PATH)
    old_rel = str(Path(src_path).relative_to(vault))
    new_rel = str(Path(dest_path).relative_to(vault))
    dest = Path(dest_path)

    # Get old title from cache
    cached = cache.get(old_rel)
    old_title = cached["title"] if cached else ""

    # Get new title from file
    new_title, new_hash = _read_note_meta(dest)

    if not old_title and not new_title:
        logger.debug("Move event ignored (no titles): %s -> %s", old_rel, new_rel)
        cache.remove(old_rel)
        return

    # Conflict check: does another note already have this title?
    if new_title and old_title != new_title:
        titles = cache.titles_map()
        conflict_path = titles.get(new_title)
        if conflict_path and conflict_path != old_rel and conflict_path != new_rel:
            msg = (
                f"\u26a0\ufe0f Title conflict: \u00ab{new_title}\u00bb already exists "
                f"at {conflict_path}. Wiki-links NOT updated."
            )
            logger.warning(msg)
            _notify_telegram(msg)
            cache.remove(old_rel)
            cache.set(new_rel, new_title, new_hash)
            return

    # Update wiki-links if title changed
    link_count = 0
    if old_title and new_title and old_title != new_title:
        link_count = update_wiki_links(old_title, new_title, VAULT_PATH)

    # Update cache
    cache.remove(old_rel)
    cache.set(new_rel, new_title or old_title, new_hash)

    # Re-index in LightRAG with new path
    _reindex_in_lightrag(dest, VAULT_PATH)

    # Invalidate linker caches
    try:
        from .linker import invalidate_cache
        invalidate_cache()
    except Exception:
        pass

    # Notify
    if old_title and new_title and old_title != new_title:
        _notify_telegram(
            f"\U0001f4dd Rename: \u00ab{old_title}\u00bb \u2192 \u00ab{new_title}\u00bb. "
            f"Links updated: {link_count}."
        )
        logger.info(
            "Path sync: '%s' -> '%s', %d links updated", old_title, new_title, link_count
        )
    else:
        logger.info("Path sync: moved %s -> %s (title unchanged)", old_rel, new_rel)


def handle_modify(file_path: str, cache: FrontmatterCache):
    """Handle file modify — check if title changed in frontmatter.

    Args:
        file_path: absolute path to modified file
        cache: frontmatter cache instance
    """
    vault = Path(VAULT_PATH)
    p = Path(file_path)
    rel = str(p.relative_to(vault))

    new_title, new_hash = _read_note_meta(p)
    if not new_title:
        return

    cached = cache.get(rel)
    if not cached:
        # New file or not in cache — just add it
        cache.set(rel, new_title, new_hash)
        return

    old_title = cached["title"]
    old_hash = cached["body_hash"]

    # Title unchanged — update hash if content changed
    if old_title == new_title:
        if old_hash != new_hash:
            cache.set(rel, new_title, new_hash)
        return

    # Title changed — update wiki-links
    # Conflict check
    titles = cache.titles_map()
    conflict_path = titles.get(new_title)
    if conflict_path and conflict_path != rel:
        msg = (
            f"\u26a0\ufe0f Title conflict: \u00ab{new_title}\u00bb already exists "
            f"at {conflict_path}. Wiki-links NOT updated."
        )
        logger.warning(msg)
        _notify_telegram(msg)
        cache.set(rel, new_title, new_hash)
        return

    link_count = update_wiki_links(old_title, new_title, VAULT_PATH)
    cache.set(rel, new_title, new_hash)

    # Re-index if content also changed
    if old_hash != new_hash:
        _reindex_in_lightrag(p, VAULT_PATH)

    try:
        from .linker import invalidate_cache
        invalidate_cache()
    except Exception:
        pass

    _notify_telegram(
        f"\U0001f4dd Rename: \u00ab{old_title}\u00bb \u2192 \u00ab{new_title}\u00bb. "
        f"Links updated: {link_count}."
    )
    logger.info(
        "Path sync (title change): '%s' -> '%s', %d links updated",
        old_title, new_title, link_count,
    )


# ---------------------------------------------------------------------------
# Periodic sync (main detection mechanism for git-pull scenarios)
# ---------------------------------------------------------------------------

def sync_paths(cache: FrontmatterCache, vault_path: str | None = None) -> dict:
    """Compare frontmatter cache with current vault state. Detect renames and title changes.

    Returns: {renames: [{old, new, links}], title_changes: [{old, new, links}], warnings: [str]}
    """
    vault_path = vault_path or VAULT_PATH
    vault = Path(vault_path)
    results: dict = {"renames": [], "title_changes": [], "warnings": []}

    # Scan current vault state
    current: dict[str, dict] = {}
    for note in _iter_vault_notes(vault):
        rel = str(note.relative_to(vault))
        title, body_hash = _read_note_meta(note)
        if title:
            current[rel] = {"title": title, "body_hash": body_hash}

    cached = cache.all()

    # Sets of paths
    cached_paths = set(cached.keys())
    current_paths = set(current.keys())

    removed = cached_paths - current_paths  # in cache but gone from disk
    added = current_paths - cached_paths    # on disk but not in cache
    common = cached_paths & current_paths   # present in both

    # --- Match removed -> added by body_hash (detect renames/moves) ---
    unmatched_removed = set(removed)
    unmatched_added = set(added)

    # Build body_hash -> path index for added files
    added_by_hash: dict[str, list[str]] = {}
    for p in added:
        h = current[p]["body_hash"]
        added_by_hash.setdefault(h, []).append(p)

    for old_path in removed:
        old_entry = cached[old_path]
        old_hash = old_entry["body_hash"]
        old_title = old_entry["title"]

        candidates = added_by_hash.get(old_hash, [])
        if not candidates:
            continue

        new_path = candidates.pop(0)
        if not candidates:
            del added_by_hash[old_hash]

        new_entry = current[new_path]
        new_title = new_entry["title"]

        unmatched_removed.discard(old_path)
        unmatched_added.discard(new_path)

        # Update wiki-links if title changed
        link_count = 0
        if old_title != new_title:
            # Conflict check
            titles = cache.titles_map()
            conflict = titles.get(new_title)
            if conflict and conflict != old_path and conflict != new_path:
                warn = (
                    f"Title conflict: '{new_title}' already at {conflict}. "
                    f"Links not updated for rename {old_path} -> {new_path}."
                )
                results["warnings"].append(warn)
                logger.warning(warn)
                _notify_telegram(f"\u26a0\ufe0f {warn}")
            else:
                link_count = update_wiki_links(old_title, new_title, vault_path)

        # Update cache
        cache.remove(old_path)
        cache.set(new_path, new_title, new_entry["body_hash"])

        # Re-index
        _reindex_in_lightrag(vault / new_path, vault_path)

        if old_title != new_title:
            results["renames"].append({
                "old": old_title, "new": new_title, "links": link_count,
            })

        logger.info("Sync: matched rename %s -> %s", old_path, new_path)

    # --- Check title changes in common paths ---
    for path in common:
        old_entry = cached[path]
        new_entry = current[path]
        old_title = old_entry["title"]
        new_title = new_entry["title"]

        if old_title == new_title:
            # Update hash if content changed
            if old_entry["body_hash"] != new_entry["body_hash"]:
                cache.set(path, new_title, new_entry["body_hash"])
            continue

        # Title changed — update wiki-links
        titles = cache.titles_map()
        conflict = titles.get(new_title)
        if conflict and conflict != path:
            warn = (
                f"Title conflict: '{new_title}' already at {conflict}. "
                f"Links not updated for {path}."
            )
            results["warnings"].append(warn)
            logger.warning(warn)
            _notify_telegram(f"\u26a0\ufe0f {warn}")
            cache.set(path, new_title, new_entry["body_hash"])
            continue

        link_count = update_wiki_links(old_title, new_title, vault_path)
        cache.set(path, new_title, new_entry["body_hash"])

        if old_entry["body_hash"] != new_entry["body_hash"]:
            _reindex_in_lightrag(vault / path, vault_path)

        results["title_changes"].append({
            "old": old_title, "new": new_title, "links": link_count,
        })
        logger.info(
            "Sync: title change in %s: '%s' -> '%s', %d links",
            path, old_title, new_title, link_count,
        )

    # --- Add new files to cache ---
    for path in unmatched_added:
        entry = current[path]
        cache.set(path, entry["title"], entry["body_hash"])

    # --- Remove deleted files from cache ---
    for path in unmatched_removed:
        cache.remove(path)

    # Invalidate linker caches if anything changed
    if results["renames"] or results["title_changes"]:
        try:
            from .linker import invalidate_cache
            invalidate_cache()
        except Exception:
            pass

    # Telegram summary
    total_renames = len(results["renames"])
    total_changes = len(results["title_changes"])
    if total_renames or total_changes:
        for r in results["renames"]:
            _notify_telegram(
                f"\U0001f4dd Rename: \u00ab{r['old']}\u00bb \u2192 \u00ab{r['new']}\u00bb. "
                f"Links updated: {r['links']}."
            )
        for c in results["title_changes"]:
            _notify_telegram(
                f"\U0001f4dd Rename: \u00ab{c['old']}\u00bb \u2192 \u00ab{c['new']}\u00bb. "
                f"Links updated: {c['links']}."
            )
        logger.info(
            "Path sync complete: %d renames, %d title changes, %d warnings",
            total_renames, total_changes, len(results["warnings"]),
        )

    return results
