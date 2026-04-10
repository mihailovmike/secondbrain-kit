"""Main processing pipeline: raw note → gates → semantic dedup → LightRAG → vault.

See docs/PRINCIPLES.md for architectural invariants.
"""

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .gate import run_all_gates, check_title_exists, mark_processed
from .lightrag_engine import insert as lightrag_insert, find_similar
from .linker import analyze, evaluate_value, merge_notes, invalidate_cache

logger = logging.getLogger(__name__)

VAULT_PATH = os.getenv("VAULT_PATH", "/app/vault")
INBOX_DIR_NAME = os.getenv("INBOX_DIR_NAME", "_inbox")


def _register_doc_title(content: str, title: str) -> None:
    """Register doc_id → title mapping for link integrity tracking."""
    try:
        import hashlib
        from .lightrag_engine import strip_frontmatter
        from .link_integrity import register_title
        body = strip_frontmatter(content)
        doc_id = f"doc-{hashlib.md5(body.encode()).hexdigest()}"
        register_title(doc_id, title)
    except Exception:
        pass

_SKIP_DIRS = {"templates", ".obsidian", ".git", ".lightrag", ".entire", ".trash"}

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


def _render_note(analysis: dict, text: str, needs_review: bool = False) -> str:
    """Render processed note with YAML frontmatter."""
    title = analysis.get("title", "Untitled")
    note_type = analysis.get("type", "concept")
    tags = analysis.get("tags", [])
    links = analysis.get("links", [])
    confidence = analysis.get("confidence", 0.5)
    source = analysis.get("source", "unknown")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    tags_str = ", ".join(tags)
    review_line = "\nneeds_review: true" if needs_review else ""
    frontmatter = f"""---
title: "{title}"
type: {note_type}
tags: [{tags_str}]
created: {today}
source: {source}
confidence: {confidence}{review_line}
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

    # === L4: Value gate (LLM) ===
    is_valuable, value_reason = evaluate_value(raw_text)
    if not is_valuable:
        logger.info(f"Rejected by value gate [{value_reason}]: {file_path}")
        mark_processed(raw_text)
        return []

    # === L6: Semantic dedup — check before inserting ===
    is_dup, existing_source = _check_semantic_duplicate(raw_text)

    if is_dup and existing_source:
        # Merge into existing note (S1: one meaning = one place)
        return _merge_into_existing(raw_text, existing_source, path)

    # === New note: analyze, write, insert ===
    return _create_new_note(raw_text, path, source)


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
        # Can't find the file — fall back to creating new
        logger.info(f"Merge target not found ({existing_source}), creating new note")
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
    try:
        inbox_path.unlink()
        logger.info(f"Removed original: {inbox_path}")
    except Exception as e:
        logger.warning(f"Could not remove original: {e}")

    invalidate_cache()
    return [str(existing_path)]


def _create_new_note(raw_text: str, inbox_path: Path, source: str = "unknown") -> list[str]:
    """Create a new vault note (no existing duplicate found)."""
    # LLM analysis: title, tags, links, folder, type
    analysis = analyze(raw_text, VAULT_PATH)
    analysis["source"] = source
    confidence = analysis.get("confidence", 0.5)

    # Determine target folder (S2: closed vocabulary)
    vault = Path(VAULT_PATH)
    folder = _pick_folder(analysis, vault)
    needs_review = False

    if folder is None:
        # No matching folder — inbox + needs_review (Q2: daemon never guesses)
        folder = INBOX_DIR_NAME
        needs_review = True
        logger.info("No matching folder, sending to inbox with needs_review")

    if confidence < 0.7:
        folder = INBOX_DIR_NAME
        needs_review = True
        logger.info(f"Low confidence ({confidence}), sending to inbox with needs_review")

    if not analysis.get("tags"):
        needs_review = True

    # Build file path
    title = analysis.get("title", "untitled")
    slug = _slugify(title)

    # L5: Title dedup
    title_ok, title_reason = check_title_exists(slug, folder)
    if not title_ok:
        logger.info(f"Title duplicate [{title_reason}], skipping: {title}")
        mark_processed(raw_text)
        return []

    filename = f"{slug}.md"
    target_dir = vault / folder
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / filename

    # Render and write
    content = _render_note(analysis, raw_text, needs_review=needs_review)
    target_path.write_text(content, encoding="utf-8")
    logger.info(f"Created: {target_path}" + (" [needs_review]" if needs_review else ""))

    # Insert into LightRAG
    rel_path = str(target_path.relative_to(vault))
    try:
        lightrag_insert(content, file_path=rel_path)
        logger.info(f"LightRAG indexed: {rel_path}")
        # Register title in link integrity index
        _register_doc_title(content, title)
    except Exception as e:
        logger.warning(f"LightRAG insert failed for {rel_path}: {e}")

    # Cleanup
    mark_processed(raw_text)
    try:
        inbox_path.unlink()
        logger.info(f"Removed original: {inbox_path}")
    except Exception as e:
        logger.warning(f"Could not remove original: {e}")

    invalidate_cache()
    return [str(target_path)]
