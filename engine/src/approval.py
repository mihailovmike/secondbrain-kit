"""Approval workflow: pending notes queue + Telegram callback handlers."""

import json
import logging
import os
import shutil
import time
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
                self._data = json.loads(p.read_text("utf-8"))
        except Exception:
            self._data = {}

    def _save(self):
        try:
            Path(_QUEUE_FILE).write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2), "utf-8",
            )
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
    """
    if not links:
        return
    try:
        from .processor import _inject_backlinks
        injected = _inject_backlinks(title, links)
        if injected:
            logger.info("Injected %d backlinks for '%s'", injected, title)
    except Exception as e:
        logger.warning("Backlink injection failed: %s", e)


def handle_callback(
    action: str, slug: str,
    callback_id: str, chat_id: str, message_id: int,
) -> None:
    """Handle Telegram inline button callback."""
    entry = _queue.get(slug)
    if not entry:
        answer_callback(callback_id, "⚠️ Заметка не найдена")
        return

    vault = Path(VAULT_PATH)
    inbox = vault / INBOX_DIR_NAME
    filename = entry["filename"]
    source_path = inbox / filename
    title = entry["title"]

    if not source_path.exists():
        answer_callback(callback_id, "⚠️ Файл удалён")
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

        # LightRAG insert + link sync (skip for personal-data)
        ct = entry.get("content_type", "")
        rel_path = str(target_path.relative_to(vault))
        existing_links = entry.get("links", [])
        if ct != "personal-data":
            try:
                from .lightrag_engine import insert as lightrag_insert
                content = target_path.read_text("utf-8")
                lightrag_insert(content, file_path=rel_path)
                logger.info("Approved + indexed: %s → %s", title, rel_path)
            except Exception as e:
                logger.warning("LightRAG insert after approval failed: %s", e)
            _update_forward_links(target_path, existing_links)
            _inject_backlinks_for_note(title, existing_links)

        answer_callback(callback_id, f"✅ {title}")
        edit_message(chat_id, message_id,
                     f"✅ <b>Одобрено</b>\n{title}\n📁 {folder}")
        _queue.remove(slug)

    elif action == "r":
        # Reject: delete file
        source_path.unlink()
        answer_callback(callback_id, f"❌ {title}")
        edit_message(chat_id, message_id,
                     f"❌ <b>Отклонено</b>\n{title}")
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

        # LightRAG insert + link sync (skip for personal-data)
        ct = entry.get("content_type", "")
        rel_path = str(target_path.relative_to(vault))
        existing_links = entry.get("links", [])
        if ct != "personal-data":
            try:
                from .lightrag_engine import insert as lightrag_insert
                content = target_path.read_text("utf-8")
                lightrag_insert(content, file_path=rel_path)
            except Exception as e:
                logger.warning("LightRAG insert after folder create failed: %s", e)
            _update_forward_links(target_path, existing_links)
            _inject_backlinks_for_note(title, existing_links)

        answer_callback(callback_id, f"📂 {folder}")
        edit_message(chat_id, message_id,
                     f"📂 <b>Создано</b>\n{title}\n📁 {folder}")
        _queue.remove(slug)
        logger.info("Created folder + moved: %s → %s", title, folder)

    elif action == "k":
        # Keep in inbox for manual editing
        answer_callback(callback_id, f"📁 Оставлено в inbox")
        edit_message(chat_id, message_id,
                     f"📁 <b>В inbox</b>\n{title}\nОтредактируй и перемести вручную")
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
        )
        entry["message_id"] = new_msg_id
        entry["created_at"] = now
        _queue.add(slug, entry)
        refreshed += 1
        logger.info("Stale cleanup: resent %s", title)

    return refreshed


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
