"""Approval workflow: pending notes queue + Telegram callback handlers."""

import fcntl
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from datetime import date
from pathlib import Path

from .telegram import (
    answer_callback, edit_message, delete_message, send_approval,
    notify_inbox, TELEGRAM_INBOX_CHAT_ID,
)

logger = logging.getLogger(__name__)

VAULT_PATH = os.getenv("VAULT_PATH", "/app/vault")
INBOX_DIR_NAME = os.getenv("INBOX_DIR_NAME", "_inbox")
APPROVAL_MODE = os.getenv("APPROVAL_MODE", "approve")
STALE_HOURS = int(os.getenv("APPROVAL_STALE_HOURS", "24"))
_QUEUE_FILE = os.path.join(VAULT_PATH, ".approval_queue.json")


class ApprovalQueue:
    """Persistent queue of notes pending approval.

    Store: {slug: {filename, proposed_folder, title, tags, type, message_id}}
    """

    def __init__(self):
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self):
        try:
            p = Path(_QUEUE_FILE)
            if p.exists():
                with open(p, "r", encoding="utf-8") as fh:
                    fcntl.flock(fh, fcntl.LOCK_SH)
                    try:
                        self._data = json.load(fh)
                    finally:
                        fcntl.flock(fh, fcntl.LOCK_UN)
        except Exception:
            self._data = {}

    def _save(self):
        try:
            p = Path(_QUEUE_FILE)
            p.parent.mkdir(parents=True, exist_ok=True)
            data = json.dumps(self._data, ensure_ascii=False, indent=2)
            fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
            closed = False
            try:
                os.write(fd, data.encode("utf-8"))
                os.fsync(fd)
                os.close(fd)
                closed = True
                os.replace(tmp, str(p))
            except Exception:
                if not closed:
                    os.close(fd)
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
        except Exception as e:
            logger.warning("Queue save failed: %s", e)

    def add(self, slug: str, info: dict):
        self._data[slug] = info
        self._save()

    def get(self, slug: str) -> dict | None:
        return self._data.get(slug)

    def remove(self, slug: str):
        self._data.pop(slug, None)
        self._save()

    def all(self) -> dict[str, dict]:
        return dict(self._data)


_queue = ApprovalQueue()


def submit_for_approval(
    slug: str, filename: str, proposed_folder: str,
    title: str, tags: list[str], note_type: str,
    content_type: str = "",
    confidence: float = 0.0,
    needs_folder: bool = False,
    suggested_folder: str = "",
    new_type_label: str = "",
    new_type_reason: str = "",
    links: list[str] | None = None,
) -> None:
    """Submit a note for user approval via Telegram."""
    msg_id = send_approval(
        title=title,
        folder=proposed_folder,
        tags=tags,
        note_type=note_type,
        slug=slug,
        content_type=content_type,
        confidence=confidence,
        needs_folder=needs_folder,
        suggested_folder=suggested_folder,
        new_type_label=new_type_label,
        filename=filename,
        new_type_reason=new_type_reason,
    )
    cb_slug = slug[:30]
    _queue.add(cb_slug, {
        "filename": filename,
        "proposed_folder": suggested_folder if needs_folder else proposed_folder,
        "title": title,
        "tags": tags,
        "type": note_type,
        "content_type": content_type,
        "needs_folder": needs_folder,
        "new_type_label": new_type_label,
        "new_type_reason": new_type_reason,
        "links": links or [],
        "message_id": msg_id,
        "created_at": time.time(),
    })
    logger.info("Submitted for approval: %s → %s", title, proposed_folder)


def _update_forward_links(target_path: Path, existing_links: list[str]) -> None:
    """Discover additional related notes via LightRAG graph and add to ## Links section.

    Runs after LightRAG insert to catch cross-links with already-indexed notes.
    Only adds new links — does not remove existing ones.
    """
    try:
        from .linker import suggest_links
        content = target_path.read_text("utf-8")
        discovered = suggest_links(content, VAULT_PATH, limit=10)
        new_links = [l for l in discovered if l not in existing_links]
        if not new_links:
            return

        for link in new_links:
            if f"[[{link}]]" in content:
                continue
            if "\n## Links\n" in content:
                content = content.replace(
                    "\n## Links\n",
                    f"\n## Links\n- [[{link}]]\n",
                )
            else:
                content = content.rstrip("\n") + f"\n\n## Links\n- [[{link}]]\n"

        target_path.write_text(content, "utf-8")
        logger.info("Added %d graph-discovered links to '%s'", len(new_links), target_path.name)
    except Exception as e:
        logger.warning("Forward link update failed: %s", e)


def _inject_backlinks_for_note(title: str, links: list[str]) -> None:
    """Inject backlinks into referenced vault notes after approval.

    Lazy import from processor to avoid circular dependency at module load.
    Validates that link targets exist before injecting.
    """
    if not links:
        return
    try:
        vault = Path(VAULT_PATH)
        existing_titles = {
            f.stem for d in vault.iterdir() if d.is_dir() and not d.name.startswith(".")
            for f in d.rglob("*.md")
        }
        valid_links = [l for l in links if _slugify_simple(l) in existing_titles or
                       any((vault / d / f"{_slugify_simple(l)}.md").exists()
                           for d in os.listdir(VAULT_PATH)
                           if (vault / d).is_dir() and not d.startswith("."))]
        if not valid_links:
            logger.info("No valid backlink targets found for '%s'", title)
            return
        dropped = len(links) - len(valid_links)
        if dropped:
            logger.info("Dropped %d dead backlink targets for '%s'", dropped, title)
        from .processor import _inject_backlinks
        injected = _inject_backlinks(title, valid_links)
        if injected:
            logger.info("Injected %d backlinks for '%s'", injected, title)
    except Exception as e:
        logger.warning("Backlink injection failed: %s", e)


def _slugify_simple(text: str) -> str:
    """Simple slugify for title → filename matching."""
    import unicodedata
    text = unicodedata.normalize("NFKD", text)
    text = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[-\s]+", "-", text).strip("-")


def _ensure_post_approval_links(target_path: Path, folder: str) -> None:
    """Verify approved note has at least 1 wiki-link; add structural fallback if not."""
    try:
        content = target_path.read_text("utf-8")
        import re as _re
        if _re.search(r"\[\[[^\]]+\]\]", content):
            return  # Has at least one wiki-link

        # Import structural fallback from processor
        from .processor import STRUCTURAL_LINK_MAP, _DEFAULT_FALLBACK_LINK
        target_link = _DEFAULT_FALLBACK_LINK
        for prefix, link in STRUCTURAL_LINK_MAP.items():
            if folder.startswith(prefix):
                target_link = link
                break

        if f"## Links" in content:
            content = content.replace("## Links\n", f"## Links\n- [[{target_link}]]\n")
        else:
            content = content.rstrip("\n") + f"\n\n## Links\n- [[{target_link}]]\n"
        target_path.write_text(content, "utf-8")
        logger.info("Post-approval structural link added: [[%s]] → %s", target_link, target_path.name)
    except Exception as e:
        logger.warning("Post-approval link check failed: %s", e)


def _background_index_worker(
    target_path: Path, vault: Path, title: str, folder: str,
    existing_links: list[str], run_dedup_after: bool,
    include_definition_drafts: bool,
) -> None:
    """Heavy post-approval work: LightRAG insert, dedup, link enrichment.

    Runs in a daemon thread so Telegram callback can return instantly.
    """
    rel_path = str(target_path.relative_to(vault))
    try:
        from .lightrag_engine import insert as lightrag_insert
        content = target_path.read_text("utf-8")
        lightrag_insert(content, file_path=rel_path)
        logger.info("Approved + indexed: %s → %s", title, rel_path)
        if include_definition_drafts:
            _create_definition_drafts(content, vault)
        if run_dedup_after:
            try:
                from .graph_dedup import run_dedup
                dedup = run_dedup(vault_path=str(vault), dry_run=False)
                if dedup.get("merged"):
                    logger.info(
                        "Entity dedup: merged %d cluster(s) after insert",
                        len(dedup["merged"]),
                    )
            except Exception as dedup_err:
                logger.warning("Entity dedup (non-blocking): %s", dedup_err)
    except Exception as e:
        logger.warning("LightRAG insert after approval failed: %s", e)

    try:
        _update_forward_links(target_path, existing_links)
        _inject_backlinks_for_note(title, existing_links)
        _ensure_post_approval_links(target_path, folder)
    except Exception as e:
        logger.warning("Post-approval link enrichment failed: %s", e)


def _run_background_index(
    target_path: Path, vault: Path, title: str, folder: str,
    existing_links: list[str], run_dedup_after: bool,
    include_definition_drafts: bool,
) -> None:
    """Spawn a daemon thread to run post-approval indexing."""
    threading.Thread(
        target=_background_index_worker,
        args=(
            target_path, vault, title, folder, existing_links,
            run_dedup_after, include_definition_drafts,
        ),
        daemon=True,
        name=f"approve-index-{target_path.stem[:20]}",
    ).start()


def handle_callback(
    action: str, slug: str,
    callback_id: str, chat_id: str, message_id: int,
) -> None:
    """Handle Telegram inline button callback."""
    # Dismiss system notifications
    if action == "d":
        answer_callback(callback_id, "✓")
        delete_message(chat_id, message_id)
        return

    # Delete orphan docs from graph
    if action == "o" and slug == "delete":
        try:
            from .lightrag_engine import sync_with_vault
            result = sync_with_vault(VAULT_PATH, dry_run=False)
            deleted = len(result.get("deleted", []))
            answer_callback(callback_id, f"🗑 Удалено {deleted}")
            edit_message(chat_id, message_id, f"✅ Удалено {deleted} сирот из графа")
        except Exception as e:
            logger.error("Orphan delete failed: %s", e)
            answer_callback(callback_id, "❌ Ошибка удаления")
        return

    entry = _queue.get(slug)
    if not entry:
        answer_callback(callback_id, "⚠️ Заметка не найдена")
        delete_message(chat_id, message_id)
        return

    vault = Path(VAULT_PATH)
    inbox = vault / INBOX_DIR_NAME
    filename = entry["filename"]
    source_path = inbox / filename
    title = entry["title"]

    if not source_path.exists():
        answer_callback(callback_id, "⚠️ Файл удалён")
        delete_message(chat_id, message_id)
        _queue.remove(slug)
        return

    if action == "a":
        # Approve: move to proposed folder + LightRAG insert
        folder = entry["proposed_folder"]
        target_dir = vault / folder
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / filename

        shutil.move(str(source_path), str(target_path))
        _remove_proposed_folder_from_frontmatter(target_path)

        # If new type was proposed, add it to types.yaml
        if entry.get("new_type_label"):
            _add_type_to_yaml(
                vault, entry["type"], entry["new_type_label"],
            )

        answer_callback(callback_id, f"✅ {title} → {folder}")
        delete_message(chat_id, message_id)
        _queue.remove(slug)

        ct = entry.get("content_type", "")
        if ct != "personal-data":
            _run_background_index(
                target_path, vault, title, folder,
                entry.get("links", []), run_dedup_after=True,
                include_definition_drafts=True,
            )

    elif action == "r":
        # Reject: delete file
        source_path.unlink()
        answer_callback(callback_id, f"❌ Удалено: {title}")
        delete_message(chat_id, message_id)
        _queue.remove(slug)
        logger.info("Rejected: %s", title)

    elif action == "f":
        # Create new folder and move note there
        folder = entry["proposed_folder"]
        if not folder:
            answer_callback(callback_id, "⚠️ Нет предложенной папки")
            return

        target_dir = vault / folder
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / filename

        shutil.move(str(source_path), str(target_path))
        _remove_proposed_folder_from_frontmatter(target_path)

        answer_callback(callback_id, f"📂 {title} → {folder}")
        delete_message(chat_id, message_id)
        _queue.remove(slug)
        logger.info("Created folder + moved: %s → %s", title, folder)

        ct = entry.get("content_type", "")
        if ct != "personal-data":
            _run_background_index(
                target_path, vault, title, folder,
                entry.get("links", []), run_dedup_after=False,
                include_definition_drafts=False,
            )

    elif action == "k":
        # Keep in inbox for manual editing
        answer_callback(callback_id, f"📁 {title} — в inbox")
        delete_message(chat_id, message_id)
        _queue.remove(slug)
        logger.info("Kept in inbox: %s", title)


def _add_type_to_yaml(vault: Path, type_key: str, label: str) -> None:
    """Add a new note type to vault/types.yaml when user approves it."""
    types_file = vault / "_system" / "types.yaml"
    try:
        content = types_file.read_text("utf-8") if types_file.exists() else ""
        if f"{type_key}:" in content:
            return  # already exists
        content = content.rstrip() + f"\n{type_key}: {label}\n"
        types_file.write_text(content, "utf-8")
        # Invalidate linker cache so new type is picked up
        from .linker import invalidate_cache
        invalidate_cache()
        logger.info("Added new type to types.yaml: %s (%s)", type_key, label)
    except Exception as e:
        logger.warning("Failed to add type to yaml: %s", e)


def cleanup_stale() -> int:
    """Delete stale approval messages and resend. Returns count of refreshed."""
    now = time.time()
    max_age = STALE_HOURS * 3600
    refreshed = 0

    for slug, entry in list(_queue.all().items()):
        created_at = entry.get("created_at", 0)
        if not created_at or (now - created_at) < max_age:
            continue

        msg_id = entry.get("message_id")
        title = entry.get("title", "?")

        # Delete old message
        if msg_id:
            delete_message(TELEGRAM_INBOX_CHAT_ID, msg_id)

        # Check file still exists
        vault = Path(VAULT_PATH)
        source = vault / INBOX_DIR_NAME / entry.get("filename", "")
        if not source.exists():
            _queue.remove(slug)
            logger.info("Stale cleanup: %s — file gone, removed from queue", title)
            continue

        # Resend
        new_msg_id = send_approval(
            title=title,
            folder=entry.get("proposed_folder", ""),
            tags=entry.get("tags", []),
            note_type=entry.get("type", "concept"),
            slug=slug,
            content_type=entry.get("content_type", ""),
            needs_folder=entry.get("needs_folder", False),
            new_type_label=entry.get("new_type_label", ""),
            new_type_reason=entry.get("new_type_reason", ""),
            filename=entry.get("filename", ""),
        )
        entry["message_id"] = new_msg_id
        entry["created_at"] = now
        _queue.add(slug, entry)
        refreshed += 1
        logger.info("Stale cleanup: resent %s", title)

    return refreshed


def _load_definition_titles(vault: Path) -> list[tuple[str, list[str]]]:
    """Return (title, aliases) pairs from knowledge/definitions/*.md frontmatter."""
    defs_dir = vault / "knowledge" / "definitions"
    if not defs_dir.exists():
        return []
    result = []
    for md_file in defs_dir.glob("*.md"):
        text = md_file.read_text("utf-8")
        fm = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
        if not fm:
            continue
        body = fm.group(1)
        title_m = re.search(r'^title:\s*["\']?(.+?)["\']?\s*$', body, re.MULTILINE)
        if not title_m:
            continue
        title = title_m.group(1).strip()
        aliases_m = re.search(r'^aliases:\s*\[(.+?)\]', body, re.MULTILINE)
        aliases = []
        if aliases_m:
            aliases = [a.strip().strip('"\'') for a in aliases_m.group(1).split(",")]
        result.append((title, aliases))
    return result


def _has_definition(term: str, defs: list[tuple[str, list[str]]]) -> bool:
    """Return True if term matches any known definition title or alias."""
    term_lower = term.lower()
    for title, aliases in defs:
        if title.lower() == term_lower:
            return True
        if any(a.lower() == term_lower for a in aliases):
            return True
    return False


def _create_definition_drafts(content: str, vault: Path) -> None:
    """Extract key terms from content and create _inbox/def-*.md drafts if undefined."""
    try:
        import google.generativeai as genai
        model = genai.GenerativeModel("gemini-2.5-flash")
        prompt = (
            "Выдели ключевые термины из текста, которые могут требовать отдельного файла "
            "определения (концепции, имена собственные, фреймворки). "
            "Верни только JSON-массив строк. Максимум 5 терминов. Только самые важные.\n\n"
            f"{content[:3000]}"
        )
        response = model.generate_content(prompt)
        raw = response.text.strip()
        # Strip markdown code fences if present
        raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("` \n")
        terms = json.loads(raw)
        if not isinstance(terms, list):
            return
    except Exception as e:
        logger.warning("Definition term extraction failed: %s", e)
        return

    defs = _load_definition_titles(vault)
    inbox = vault / INBOX_DIR_NAME
    today = date.today().isoformat()

    for term in terms:
        if not isinstance(term, str) or not term.strip():
            continue
        term = term.strip()
        if _has_definition(term, defs):
            continue
        slug = re.sub(r"[^\w\-]", "-", term.lower())[:60].strip("-")
        draft_path = inbox / f"def-{slug}.md"
        if draft_path.exists():
            continue
        draft_path.write_text(
            f"---\ntitle: \"{term}\"\ntype: concept\ntags: [needs_definition]\n"
            f"created: {today}\nsource: auto\nconfidence: 0.5\nneeds_definition: true\n---\n",
            "utf-8",
        )
        logger.info("Created definition draft: %s", draft_path.name)


def _remove_proposed_folder_from_frontmatter(path: Path) -> None:
    """Remove proposed_folder and needs_review from frontmatter after approval."""
    content = path.read_text("utf-8")
    lines = content.split("\n")
    cleaned = [
        ln for ln in lines
        if not ln.startswith("proposed_folder:")
        and not ln.startswith("needs_review:")
        and not ln.startswith("needs_folder:")
    ]
    path.write_text("\n".join(cleaned), "utf-8")
