"""Shared Telegram notification module with inline keyboard support."""

import html
import json
import logging
import os
import time
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_DM_CHAT_ID = os.getenv("TELEGRAM_DM_CHAT_ID", "")
TELEGRAM_INBOX_CHAT_ID = os.getenv("TELEGRAM_INBOX_CHAT_ID", "")
TELEGRAM_INBOX_THREAD_ID = os.getenv("TELEGRAM_INBOX_THREAD_ID", "")
VAULT_GITHUB_URL = "https://github.com/evidpo/SecondBrain/blob/main"

_NOTIF_STORE = "/tmp/sb_notifications.json"
NOTIF_TTL = int(os.getenv("NOTIF_TTL_SECONDS", str(2 * 3600)))  # auto-delete after 2h


def _load_notif_store() -> dict[str, float]:
    """Load {message_id_str: sent_at_timestamp} store."""
    try:
        if os.path.exists(_NOTIF_STORE):
            return json.loads(open(_NOTIF_STORE).read())
    except Exception:
        pass
    return {}


def _save_notif_store(store: dict[str, float]) -> None:
    try:
        with open(_NOTIF_STORE, "w") as f:
            f.write(json.dumps(store))
    except Exception:
        pass


def _track_notif(message_id: int | None) -> None:
    if not message_id:
        return
    store = _load_notif_store()
    store[str(message_id)] = time.time()
    _save_notif_store(store)


def cleanup_system_notifications(max_age: int | None = None) -> int:
    """Delete tracked system notifications older than max_age seconds.

    If max_age is None, deletes ALL tracked notifications (daily cleanup).
    Returns count deleted.
    """
    chat_id = TELEGRAM_INBOX_CHAT_ID
    if not chat_id:
        return 0
    store = _load_notif_store()
    now = time.time()
    to_delete = []
    to_keep = {}
    for mid_str, sent_at in store.items():
        age = now - sent_at
        if max_age is None or age >= max_age:
            to_delete.append(int(mid_str))
        else:
            to_keep[mid_str] = sent_at

    deleted = 0
    for mid in to_delete:
        if delete_message(chat_id, mid):
            deleted += 1
    _save_notif_store(to_keep)
    if deleted:
        logger.info("Notification cleanup: deleted %d messages (max_age=%s)", deleted, max_age)
    return deleted


def _api_call(method: str, params: dict, timeout: int = 15) -> dict | None:
    """Call Telegram Bot API. Returns parsed JSON or None."""
    if not TELEGRAM_BOT_TOKEN:
        return None
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    data = json.dumps(params).encode("utf-8")
    try:
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e else ""
        logger.warning("Telegram API %s failed: %s — %s", method, e, body)
        return None
    except Exception as e:
        logger.warning("Telegram API %s failed: %s", method, e)
        return None


def send_message(
    text: str,
    chat_id: str | None = None,
    thread_id: str | None = None,
    reply_markup: dict | None = None,
) -> int | None:
    """Send Telegram message. Returns message_id on success."""
    chat_id = chat_id or TELEGRAM_DM_CHAT_ID
    if not chat_id:
        return None
    params: dict = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if thread_id:
        params["message_thread_id"] = int(thread_id)
    if reply_markup:
        params["reply_markup"] = reply_markup
    result = _api_call("sendMessage", params)
    if result and result.get("ok"):
        return result["result"]["message_id"]
    return None


def answer_callback(callback_id: str, text: str = "") -> None:
    """Answer a callback query (dismiss the loading spinner)."""
    _api_call("answerCallbackQuery", {
        "callback_query_id": callback_id,
        "text": text,
    })


def edit_message(chat_id: str, message_id: int, text: str) -> None:
    """Edit an existing message (remove buttons after action)."""
    _api_call("editMessageText", {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
    })


def delete_message(chat_id: str, message_id: int) -> bool:
    """Delete a message from chat."""
    result = _api_call("deleteMessage", {
        "chat_id": chat_id,
        "message_id": message_id,
    })
    return bool(result and result.get("ok"))


def _content_type_icon(content_type: str) -> str:
    """Map content type to emoji."""
    return {
        "knowledge-note": "📋",
        "author-content": "✍️",
        "personal-data": "🏥",
    }.get(content_type, "📋")


def _get_type_label(note_type: str) -> str:
    """Get Russian label for note type from vault types.yaml."""
    try:
        from .linker import get_note_types
        types_map = get_note_types()
        return types_map.get(note_type, note_type)
    except Exception:
        return note_type


def send_approval(
    title: str, folder: str, tags: list[str],
    note_type: str, slug: str, content_type: str = "",
    confidence: float = 0.0,
    needs_folder: bool = False,
    suggested_folder: str = "",
    new_type_label: str = "",
    new_type_reason: str = "",
    filename: str = "",
) -> int | None:
    """Send approval message with inline buttons to inbox topic."""
    tags_str = ", ".join(html.escape(t) for t in tags) if tags else "—"
    esc_title = html.escape(title)
    icon = _content_type_icon(content_type)
    cb_slug = slug[:30]

    type_ru = _get_type_label(note_type)
    if new_type_label:
        type_label = f"🆕 {note_type} ({html.escape(new_type_label)})"
        if new_type_reason:
            type_label += f"\n💬 {html.escape(new_type_reason)}"
    else:
        type_label = f"📝 {note_type} ({type_ru})"

    # GitHub link to read the note
    github_link = ""
    if filename:
        encoded = urllib.parse.quote(f"_inbox/{filename}")
        github_link = f'\n🔗 <a href="{VAULT_GITHUB_URL}/{encoded}">Читать</a>'

    if needs_folder and suggested_folder:
        # --- No matching folder: propose new domain ---
        esc_suggested = html.escape(suggested_folder)
        text = (
            f"📂 <b>{esc_title}</b>\n\n"
            f"⚠️ Нет подходящей папки\n"
            f"💡 Предлагаю: <code>{esc_suggested}</code>\n"
            f"🏷 {tags_str}\n"
            f"{type_label}{github_link}"
        )
        if confidence:
            text += f"\n📊 {confidence:.0%}"
        keyboard = {
            "inline_keyboard": [[
                {"text": f"📂 Создать {suggested_folder}", "callback_data": f"f:{cb_slug}"},
                {"text": "❌ Удалить", "callback_data": f"r:{cb_slug}"},
                {"text": "📁 Inbox", "callback_data": f"k:{cb_slug}"},
            ]]
        }
    else:
        # --- Normal approval ---
        esc_folder = html.escape(folder)
        personal = content_type == "personal-data"
        text = (
            f"{icon} <b>{esc_title}</b>\n\n"
            f"📁 {esc_folder}\n"
            f"🏷 {tags_str}\n"
            f"{type_label}{github_link}"
        )
        if confidence:
            text += f"\n📊 {confidence:.0%}"
        if personal:
            text += "\n⚡ Личные данные (без LightRAG)"

        btn_label = f"✅ В {folder}" if folder != "_inbox" else "✅ Одобрить"
        keyboard = {
            "inline_keyboard": [[
                {"text": btn_label, "callback_data": f"a:{cb_slug}"},
                {"text": "❌ Удалить", "callback_data": f"r:{cb_slug}"},
                {"text": "📁 Inbox", "callback_data": f"k:{cb_slug}"},
            ]]
        }

    return send_message(
        text,
        chat_id=TELEGRAM_INBOX_CHAT_ID,
        thread_id=TELEGRAM_INBOX_THREAD_ID,
        reply_markup=keyboard,
    )


def notify_dm(text: str) -> int | None:
    """Send to user's DM chat."""
    return send_message(text, chat_id=TELEGRAM_DM_CHAT_ID)


def notify_inbox(text: str) -> int | None:
    """Send system notification to inbox topic with ✓ Прочитано button."""
    keyboard = {
        "inline_keyboard": [[
            {"text": "✓ Прочитано", "callback_data": "d:dismiss"},
        ]]
    }
    mid = send_message(
        text,
        chat_id=TELEGRAM_INBOX_CHAT_ID,
        thread_id=TELEGRAM_INBOX_THREAD_ID,
        reply_markup=keyboard,
    )
    _track_notif(mid)
    return mid


def notify_orphans(orphans: list[str]) -> int | None:
    """Send orphan docs notification with action buttons."""
    paths_str = "\n".join(f"• {p}" for p in orphans[:10])
    if len(orphans) > 10:
        paths_str += f"\n…ещё {len(orphans) - 10}"
    text = (
        f"🔍 <b>Сироты в графе: {len(orphans)}</b>\n"
        f"Файлы отсутствуют в vault, но есть в графе:\n"
        f"<code>{paths_str}</code>"
    )
    keyboard = {
        "inline_keyboard": [[
            {"text": "🗑 Удалить всё", "callback_data": "o:delete"},
            {"text": "⏭ Пропустить", "callback_data": "d:dismiss"},
        ]]
    }
    mid = send_message(
        text,
        chat_id=TELEGRAM_INBOX_CHAT_ID,
        thread_id=TELEGRAM_INBOX_THREAD_ID,
        reply_markup=keyboard,
    )
    _track_notif(mid)
    return mid


def poll_callbacks(handler, poll_interval: float = 2.0) -> None:
    """Long-polling loop for callback queries. Runs forever.

    handler(action, slug, callback_id, chat_id, message_id) is called
    for each button press. action is 'a', 'r', or 'k'.
    """
    offset = 0
    logger.info("Telegram callback listener started")
    while True:
        try:
            result = _api_call("getUpdates", {
                "offset": offset,
                "timeout": 30,
                "allowed_updates": ["callback_query"],
            }, timeout=40)
            if not result or not result.get("ok"):
                time.sleep(poll_interval)
                continue

            updates = result.get("result", [])
            if updates:
                logger.info("Received %d callback updates", len(updates))

            for update in updates:
                offset = update["update_id"] + 1
                cq = update.get("callback_query")
                if not cq:
                    continue

                data = cq.get("data", "")
                logger.info("Callback data: %s", data)
                if ":" not in data:
                    continue

                action, slug = data.split(":", 1)
                cb_id = cq["id"]
                msg = cq.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                message_id = msg.get("message_id", 0)

                try:
                    handler(action, slug, cb_id, chat_id, message_id)
                except Exception as e:
                    logger.error("Callback handler error: %s", e)
                    answer_callback(cb_id, "❌ Ошибка")

        except Exception as e:
            logger.warning("Callback poll error: %s", e)
            time.sleep(poll_interval)
