"""Main processing pipeline: raw note → gates → semantic dedup → LightRAG → vault.

See docs/PRINCIPLES.md for architectural invariants.
"""

import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .gate import run_all_gates, check_title_exists, mark_processed
from .lightrag_engine import insert as lightrag_insert, find_similar
from .linker import (
    analyze, evaluate_value, merge_notes, invalidate_cache,
    get_existing_note_titles, extract_knowledge, classify_content_type,
    suggest_links, suggest_folder,
)
from .telegram import notify_inbox
from .approval import submit_for_approval, APPROVAL_MODE

logger = logging.getLogger(__name__)

VAULT_PATH = os.getenv("VAULT_PATH", "/app/vault")
INBOX_DIR_NAME = os.getenv("INBOX_DIR_NAME", "_inbox")
_APPROVE = APPROVAL_MODE == "approve"


def _register_doc_title(content: str, title: str) -> None:
    """Register doc_id → title mapping for link integrity tracking."""
    try:
        from .lightrag_engine import compute_doc_id
        from .link_integrity import register_title
        register_title(compute_doc_id(content), title)
    except Exception:
        pass

_SKIP_DIRS = {"templates", ".obsidian", ".git", ".lightrag", ".entire", ".trash", "_system"}

# Similarity threshold for merging (S1: uniqueness principle)
MERGE_THRESHOLD = float(os.getenv("MERGE_THRESHOLD", "0.85"))


def _list_vault_paths() -> list[str]:
    """Discover all note folder paths in vault (recursive, relative)."""
    vault = Path(VAULT_PATH)
    paths = []
    for d in vault.rglob("*"):
        if not d.is_dir():
            continue
        rel = d.relative_to(vault)
        parts = rel.parts
        if any(p in _SKIP_DIRS or p.startswith(".") for p in parts):
            continue
        if parts[0] == INBOX_DIR_NAME:
            continue
        paths.append(str(rel))
    return sorted(paths)


def _pick_folder(analysis: dict, vault: Path) -> str | None:
    """Pick target folder from LLM suggestion, matched against real vault tree.

    Returns None if no folder matches (S2: closed vocabulary).
    """
    paths = _list_vault_paths()
    if not paths:
        return None

    suggested = analysis.get("folder", "").lower().strip("/")
    if not suggested:
        return None

    if suggested in paths:
        return suggested

    for p in paths:
        if p.endswith("/" + suggested) or p == suggested:
            return p

    return None


def _slugify(text: str, max_len: int = 48) -> str:
    """Convert text to filename-safe slug."""
    text = text.lower().strip()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[\s_]+', '-', text)
    text = re.sub(r'-+', '-', text).strip('-')
    return text[:max_len]


def _render_note(analysis: dict, text: str, needs_review: bool = False,
                 proposed_folder: str = "",
                 needs_folder: bool = False) -> str:
    """Render processed note with YAML frontmatter."""
    title = analysis.get("title", "Untitled")
    note_type = analysis.get("type", "concept")
    tags = analysis.get("tags", [])
    links = analysis.get("links", [])
    confidence = analysis.get("confidence", 0.5)
    source = analysis.get("source", "unknown")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    tags_str = ", ".join(tags)
    extra_lines = ""
    if needs_review:
        extra_lines += "\nneeds_review: true"
    if needs_folder:
        extra_lines += "\nneeds_folder: true"
    if proposed_folder:
        extra_lines += f"\nproposed_folder: {proposed_folder}"
    frontmatter = f"""---
title: "{title}"
type: {note_type}
tags: [{tags_str}]
created: {today}
source: {source}
confidence: {confidence}{extra_lines}
---"""

    links_section = ""
    if links:
        links_lines = "\n".join(f"- [[{link}]]" for link in links)
        links_section = f"\n\n## Links\n{links_lines}"

    return f"{frontmatter}\n\n# {title}\n\n{text}{links_section}\n"


def _find_vault_file(slug: str) -> Path | None:
    """Find existing vault file by slug (any folder)."""
    vault = Path(VAULT_PATH)
    for match in vault.rglob(f"{slug}.md"):
        rel = match.relative_to(vault)
        parts = rel.parts
        if any(p in _SKIP_DIRS or p.startswith(".") for p in parts):
            continue
        if parts[0] != INBOX_DIR_NAME:
            return match
    return None


def _extract_body(content: str) -> str:
    """Extract body text from a note (strip frontmatter and heading)."""
    # Remove YAML frontmatter
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            content = content[end + 3:].strip()
    # Remove first heading
    lines = content.split("\n")
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    return "\n".join(lines).strip()


def _check_semantic_duplicate(text: str) -> tuple[bool, str]:
    """L6: Check if similar content already exists in the graph.

    Returns (is_duplicate, existing_file_source).
    """
    similar = find_similar(text, top_k=1)
    if not similar:
        return False, ""

    # Check if the returned context is substantial enough to indicate a real match
    top = similar[0]
    source = top.get("source", "")
    content = top.get("content", "")

    # If we got meaningful content back, it's likely a semantic match
    if content and len(content) > 50:
        return True, source

    return False, ""


def _send_review_notification(analysis: dict, folder: str, confidence: float) -> None:
    """Send Telegram notification for needs_review notes."""
    title = analysis.get("title", "Untitled")
    tags = analysis.get("tags", [])
    vault_paths = _list_vault_paths()

    # Determine reason
    reasons = []
    if confidence < 0.7:
        reasons.append(f"low confidence ({confidence:.1f})")
    if folder == INBOX_DIR_NAME and not reasons:
        reasons.append("no matching folder")
    if not tags:
        reasons.append("no matching tags")
    reason_str = ", ".join(reasons) or "manual review needed"

    # Top-3 folder suggestions from vault
    folders_str = ", ".join(vault_paths[:3]) if vault_paths else "—"
    tags_str = ", ".join(tags) if tags else "—"

    msg = (
        f"📋 <b>Needs review</b>\n"
        f"<b>{title}</b>\n"
        f"Folders: {folders_str}\n"
        f"Tags: {tags_str}\n"
        f"Reason: {reason_str}"
    )
    notify_inbox(msg)


def _find_note_file_by_title(title: str) -> Path | None:
    """Find a vault note file by its title (case-insensitive slug match)."""
    slug = _slugify(title)
    if not slug:
        return None
    return _find_vault_file(slug)


def _inject_backlinks(source_title: str, target_titles: list[str]) -> int:
    """Add [[source_title]] backlink to each target note's Links section.

    Returns number of notes updated.
    """
    updated = 0
    source_slug = _slugify(source_title)
    for target_title in target_titles:
        # Never self-link
        if _slugify(target_title) == source_slug:
            continue

        target_path = _find_note_file_by_title(target_title)
        if not target_path or not target_path.exists():
            continue

        content = target_path.read_text(encoding="utf-8")

        # Check if backlink already exists
        if f"[[{source_title}]]" in content:
            continue

        # Find or create ## Links section
        if "\n## Links\n" in content:
            content = content.replace(
                "\n## Links\n",
                f"\n## Links\n- [[{source_title}]]\n",
            )
        else:
            # Append Links section at the end
            content = content.rstrip("\n") + f"\n\n## Links\n- [[{source_title}]]\n"

        target_path.write_text(content, encoding="utf-8")
        logger.info("Backlink added: [[%s]] → %s", source_title, target_path.name)
        updated += 1

    return updated


def process_file(file_path: str, source: str = "unknown") -> list[str]:
    """Process a single file from Inbox.

    Pipeline (see docs/PRINCIPLES.md):
    1. L1-L3: cheap gates (hash, size, content quality)
    2. L4: value gate (LLM — is this long-term knowledge?)
    3. L6: semantic dedup (LightRAG search — does this already exist?)
    4. If duplicate → merge into existing note
    5. If new → analyze, pick folder, write to vault
    6. Insert into LightRAG
    Returns list of created/modified file paths.
    """
    path = Path(file_path)
    if not path.exists():
        logger.warning(f"File not found: {file_path}")
        return []

    raw_text = path.read_text(encoding="utf-8")
    if not raw_text.strip():
        logger.info(f"Empty file, skipping: {file_path}")
        return []

    # === L1-L3: Cheap gates ===
    gate_ok, gate_reason = run_all_gates(raw_text, file_path)
    if not gate_ok:
        logger.info(f"Rejected by gate [{gate_reason}]: {file_path}")
        return []

    # === Content type classification (before value gate) ===
    is_session = (
        "source: claude-session" in raw_text
        or "source: claude-compact" in raw_text
    )
    if is_session:
        content_type = "knowledge-note"  # sessions use their own evaluation path
    else:
        content_type = classify_content_type(raw_text)
        # raw-dump: reject early without LLM value call
        if content_type == "raw-dump":
            logger.info(f"Rejected by content type [raw-dump]: {file_path}")
            mark_processed(raw_text)
            return []

    # === L4: Value gate (LLM) ===
    is_valuable, value_reason = evaluate_value(raw_text, content_type)
    if not is_valuable:
        logger.info(f"Rejected by value gate [{value_reason}]: {file_path}")
        mark_processed(raw_text)
        return []

    # === Session/compact: deep knowledge extraction ===
    if is_session:
        results = _process_session(raw_text, path)
        mark_processed(raw_text)
        try:
            path.unlink()
        except Exception:
            pass
        return results

    # === Personal data: skip LightRAG, route to domain folder ===
    if content_type == "personal-data":
        return _create_new_note(raw_text, path, source, content_type, skip_lightrag=True)

    # === L6: Semantic dedup — check before inserting ===
    is_dup, existing_source = _check_semantic_duplicate(raw_text)

    if is_dup and existing_source:
        # Merge into existing note (S1: one meaning = one place)
        return _merge_into_existing(raw_text, existing_source, path)

    # === New note: analyze, write, insert ===
    return _create_new_note(raw_text, path, source, content_type)


def _process_session(raw_text: str, inbox_path: Path) -> list[str]:
    """Extract multiple knowledge units from a session/compact transcript."""
    units = extract_knowledge(raw_text)
    if not units:
        logger.info(f"No knowledge units in session: {inbox_path}")
        return []

    created: list[str] = []
    for i, unit in enumerate(units):
        if i > 0:
            time.sleep(5)  # Gemini rate limit

        body = unit.get("body", "")
        # Semantic dedup per unit
        is_dup, existing_src = _check_semantic_duplicate(body)
        if is_dup and existing_src:
            merged = _merge_into_existing(body, existing_src, inbox_path)
            created.extend(merged)
            continue

        # Build analysis dict compatible with _create_new_note flow
        # Find related notes for links (no orphan notes rule)
        unit_links = suggest_links(body, VAULT_PATH, limit=3)
        existing_titles = {t.lower() for t in get_existing_note_titles(VAULT_PATH)}
        unit_links = [l for l in unit_links if l.lower() in existing_titles]

        analysis = {
            "title": unit.get("title", "Untitled"),
            "type": unit.get("type", "concept"),
            "tags": unit.get("tags", []),
            "links": unit_links,
            "confidence": unit.get("confidence", 0.7),
            "source": "claude-session",
        }

        vault = Path(VAULT_PATH)
        folder = _pick_folder(analysis, vault)
        needs_review = False
        confidence = analysis["confidence"]

        if folder is None:
            folder = INBOX_DIR_NAME
            needs_review = True
        if confidence < 0.7:
            folder = INBOX_DIR_NAME
            needs_review = True
        if not analysis["tags"]:
            needs_review = True

        slug = _slugify(analysis["title"])
        filename = f"{slug}.md"
        title_ok, _ = check_title_exists(slug, folder)
        if not title_ok:
            continue

        # === APPROVAL MODE for session units ===
        if _APPROVE:
            proposed_folder = folder if folder != INBOX_DIR_NAME else ""
            content = _render_note(
                analysis, body, needs_review=True,
                proposed_folder=proposed_folder,
            )
            target_path = vault / INBOX_DIR_NAME / filename
            target_path.write_text(content, encoding="utf-8")
            logger.info(f"Session unit pending: {analysis['title']} → {proposed_folder or 'inbox'}")

            submit_for_approval(
                slug=slug, filename=filename,
                proposed_folder=proposed_folder or INBOX_DIR_NAME,
                title=analysis["title"],
                tags=analysis.get("tags", []),
                note_type=analysis.get("type", "concept"),
                content_type="session",
            )
            mark_processed(content)  # Prevent re-processing after git sync
            created.append(str(target_path))
            continue

        # === NOTIFY / AUTO MODE ===
        target_dir = vault / folder
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / filename

        content = _render_note(analysis, body, needs_review=needs_review)
        target_path.write_text(content, encoding="utf-8")
        logger.info(f"Session unit: {target_path}")

        if needs_review:
            _send_review_notification(analysis, folder, confidence)

        rel_path = str(target_path.relative_to(vault))
        try:
            lightrag_insert(content, file_path=rel_path)
            _register_doc_title(content, analysis["title"])
        except Exception as e:
            logger.warning(f"LightRAG insert failed: {e}")

        if analysis.get("links"):
            try:
                _inject_backlinks(analysis["title"], analysis["links"])
            except Exception as e:
                logger.warning("Backlink injection failed for session unit: %s", e)

        created.append(str(target_path))

    invalidate_cache()
    return created


def _merge_into_existing(new_text: str, existing_source: str, inbox_path: Path) -> list[str]:
    """Merge new information into an existing vault note."""
    vault = Path(VAULT_PATH)

    # Find the existing file
    existing_path = None
    if existing_source and not existing_source.startswith("graph_"):
        candidate = vault / existing_source
        if candidate.exists():
            existing_path = candidate

    if not existing_path:
        # Can't find the file — create as new note
        logger.info(f"Merge target not found ({existing_source}), creating as new note")
        return _create_new_note(new_text, inbox_path, "merge_fallback")

    existing_content = existing_path.read_text(encoding="utf-8")
    existing_body = _extract_body(existing_content)

    # LLM merge: combine existing + new, no duplication
    merged_body = merge_notes(existing_body, new_text)

    # Preserve original frontmatter, replace body
    if existing_content.startswith("---"):
        end = existing_content.find("---", 3)
        if end != -1:
            frontmatter = existing_content[:end + 3]
            # Extract title from frontmatter for heading
            try:
                meta = yaml.safe_load(existing_content[3:end])
                title = meta.get("title", "Untitled")
            except Exception:
                title = "Untitled"
            merged_content = f"{frontmatter}\n\n# {title}\n\n{merged_body}\n"
        else:
            merged_content = f"{existing_content}\n\n{merged_body}\n"
    else:
        merged_content = f"{existing_content}\n\n{merged_body}\n"

    existing_path.write_text(merged_content, encoding="utf-8")
    logger.info(f"Merged into existing: {existing_path}")

    # Update LightRAG with merged content
    rel_path = str(existing_path.relative_to(vault))
    try:
        lightrag_insert(merged_content, file_path=rel_path)
        logger.info(f"LightRAG updated: {rel_path}")
        _register_doc_title(merged_content, title)
    except Exception as e:
        logger.warning(f"LightRAG update failed for {rel_path}: {e}")

    # Cleanup
    mark_processed(new_text)
    mark_processed(merged_content)  # Prevent re-processing merged file after git sync
    try:
        inbox_path.unlink()
        logger.info(f"Removed original: {inbox_path}")
    except Exception as e:
        logger.warning(f"Could not remove original: {e}")

    invalidate_cache()
    return [str(existing_path)]


def _create_new_note(raw_text: str, inbox_path: Path, source: str = "unknown",
                     content_type: str = "knowledge-note",
                     skip_lightrag: bool = False) -> list[str]:
    """Create a new vault note (no existing duplicate found)."""
    # LLM analysis: title, tags, links, folder, type
    analysis = analyze(raw_text, VAULT_PATH, content_type=content_type)
    analysis["source"] = source
    confidence = analysis.get("confidence", 0.5)

    # Personal data: ensure personal_data tag is present
    if content_type == "personal-data":
        tags = analysis.get("tags", [])
        if "personal_data" not in tags:
            tags.append("personal_data")
        analysis["tags"] = tags

    # Validate links — only keep links to existing notes, no self-links (S4: link integrity)
    existing_titles = {t.lower() for t in get_existing_note_titles(VAULT_PATH)}
    own_title = analysis.get("title", "").lower()
    analysis["links"] = [
        link for link in analysis.get("links", [])
        if link.lower() in existing_titles and link.lower() != own_title
    ]

    # Ensure at least 1 link (no orphan notes rule)
    if not analysis["links"]:
        fallback_links = suggest_links(raw_text, VAULT_PATH, limit=3)
        analysis["links"] = [
            l for l in fallback_links
            if l.lower() in existing_titles and l.lower() != own_title
        ]
        if analysis["links"]:
            logger.info("Links found via suggest_links fallback: %s", analysis["links"])

    # Determine target folder (S2: closed vocabulary)
    vault = Path(VAULT_PATH)
    folder = _pick_folder(analysis, vault)
    needs_review = False
    needs_folder = False
    suggested_folder = ""

    if folder is None:
        # No matching folder — suggest new domain, stay in inbox
        suggested_folder = suggest_folder(raw_text, VAULT_PATH)
        folder = INBOX_DIR_NAME
        needs_review = True
        needs_folder = True
        logger.info("No matching folder, suggesting '%s', staying in inbox", suggested_folder)

    if confidence < 0.7:
        folder = INBOX_DIR_NAME
        needs_review = True
        logger.info(f"Low confidence ({confidence}), sending to inbox with needs_review")

    if not analysis.get("tags"):
        needs_review = True

    # Build file path
    title = analysis.get("title", "untitled")
    slug = _slugify(title)

    filename = f"{slug}.md"

    # === APPROVAL MODE: write to inbox, send for approval ===
    # Skip title dedup — we overwrite the inbox file with enriched version
    if _APPROVE:
        proposed_folder = folder if folder != INBOX_DIR_NAME else ""
        content = _render_note(
            analysis, raw_text, needs_review=True,
            proposed_folder=proposed_folder,
        )
        target_path = vault / INBOX_DIR_NAME / filename
        target_path.write_text(content, encoding="utf-8")
        logger.info(f"Pending approval: {title} → {proposed_folder or 'inbox'}")

        submit_for_approval(
            slug=slug, filename=filename,
            proposed_folder=proposed_folder or INBOX_DIR_NAME,
            title=title,
            tags=analysis.get("tags", []),
            note_type=analysis.get("type", "concept"),
            content_type=content_type,
            confidence=confidence,
            needs_folder=needs_folder,
            suggested_folder=suggested_folder,
            new_type_label=analysis.get("new_type_label", ""),
            new_type_reason=analysis.get("new_type_reason", ""),
            links=analysis.get("links", []),
        )

        if inbox_path != target_path and inbox_path.exists():
            try:
                inbox_path.unlink()
            except Exception:
                pass

        mark_processed(raw_text)
        mark_processed(content)  # Also mark rendered content to prevent re-processing after git sync
        invalidate_cache()
        return [str(target_path)]

    # === NOTIFY / AUTO MODE: place directly ===
    # L5: Title dedup (only in non-approve mode)
    title_ok, title_reason = check_title_exists(slug, folder)
    if not title_ok:
        logger.info(f"Title duplicate [{title_reason}], skipping: {title}")
        mark_processed(raw_text)
        return []

    target_dir = vault / folder
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / filename

    content = _render_note(
        analysis, raw_text, needs_review=needs_review,
        needs_folder=needs_folder,
        proposed_folder=suggested_folder if needs_folder else "",
    )
    target_path.write_text(content, encoding="utf-8")
    logger.info(f"Created: {target_path}" + (" [needs_review]" if needs_review else ""))

    if APPROVAL_MODE == "notify":
        notify_inbox(
            f"📝 <b>Добавлено</b>\n{title}\n📁 {folder}\n"
            f"🏷 {', '.join(analysis.get('tags', [])) or '—'}"
        )

    if needs_folder and suggested_folder:
        notify_inbox(
            f"📂 <b>Новый домен?</b>\n"
            f"Заметка «{title}» не подошла ни к одному домену.\n"
            f"Предлагаю создать папку <code>{suggested_folder}</code>.\n"
            f"Создай папку вручную и перемести заметку из _inbox/."
        )
    elif needs_review:
        _send_review_notification(analysis, folder, confidence)

    # Insert into LightRAG (skip for personal data — no KG value)
    rel_path = str(target_path.relative_to(vault))
    if skip_lightrag:
        logger.info(f"LightRAG skipped (personal-data): {rel_path}")
    else:
        try:
            lightrag_insert(content, file_path=rel_path)
            logger.info(f"LightRAG indexed: {rel_path}")
            _register_doc_title(content, title)
        except Exception as e:
            logger.warning(f"LightRAG insert failed for {rel_path}: {e}")

    # Inject backlinks into linked notes (no orphan notes rule)
    if analysis.get("links"):
        try:
            backlinked = _inject_backlinks(title, analysis["links"])
            if backlinked:
                logger.info("Injected %d backlinks for '%s'", backlinked, title)
        except Exception as e:
            logger.warning("Backlink injection failed: %s", e)

    # Cleanup
    mark_processed(raw_text)
    mark_processed(content)  # Prevent re-processing rendered file after git sync
    try:
        inbox_path.unlink()
        logger.info(f"Removed original: {inbox_path}")
    except Exception as e:
        logger.warning(f"Could not remove original: {e}")

    invalidate_cache()
    return [str(target_path)]
