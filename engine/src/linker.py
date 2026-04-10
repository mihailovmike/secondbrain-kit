"""Wiki-links, tags, folder classification, value evaluation, and note merging via LLM."""

import json
import logging
import os
import re
from pathlib import Path

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

_client: genai.Client | None = None
_existing_notes_cache: list[str] | None = None
_existing_tags_cache: set[str] | None = None

LLM_MODEL = os.getenv("LLM_MODEL", "gemini-2.5-pro")

_SKIP_DIRS = {"templates", ".obsidian", ".git", ".lightrag", ".entire", ".trash"}


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    return _client


def _scan_existing_notes(vault_path: str) -> list[str]:
    """Get list of all note titles in vault (cached)."""
    global _existing_notes_cache
    if _existing_notes_cache is not None:
        return _existing_notes_cache

    notes = []
    vault = Path(vault_path)
    for d in vault.iterdir():
        if not d.is_dir() or d.name in _SKIP_DIRS or d.name.startswith("."):
            continue
        for f in d.rglob("*.md"):
            name = f.stem.replace("-", " ").replace("_", " ")
            notes.append(name)
    _existing_notes_cache = notes
    return notes


def _scan_existing_tags(vault_path: str) -> set[str]:
    """Get set of all tags used in vault (cached)."""
    global _existing_tags_cache
    if _existing_tags_cache is not None:
        return _existing_tags_cache

    tags = set()
    vault = Path(vault_path)
    tag_re = re.compile(r'tags:\s*\[([^\]]*)\]')
    for md_file in vault.rglob("*.md"):
        try:
            content = md_file.read_text(encoding="utf-8")[:2000]
            match = tag_re.search(content)
            if match:
                raw = match.group(1)
                for t in raw.split(","):
                    t = t.strip().strip('"').strip("'")
                    if t:
                        tags.add(t)
        except Exception:
            continue
    _existing_tags_cache = tags
    return tags


def invalidate_cache() -> None:
    """Clear cached notes and tags (call after vault changes)."""
    global _existing_notes_cache, _existing_tags_cache
    _existing_notes_cache = None
    _existing_tags_cache = None


def _scan_vault_tree(vault_path: str) -> list[str]:
    """Get list of all folder paths in vault (for LLM folder suggestion)."""
    vault = Path(vault_path)
    skip = {"templates", ".obsidian", ".git", ".lightrag", ".entire", ".trash"}
    inbox = os.getenv("INBOX_DIR_NAME", "_inbox")
    paths = []
    for d in vault.rglob("*"):
        if not d.is_dir():
            continue
        rel = d.relative_to(vault)
        parts = rel.parts
        if any(p in skip or p.startswith(".") for p in parts):
            continue
        if parts[0] == inbox:
            continue
        paths.append(str(rel))
    return sorted(paths)


def evaluate_value(text: str) -> tuple[bool, str]:
    """L4: Value gate. Ask LLM if this text has long-term knowledge value.

    Returns (is_valuable, reason).
    """
    prompt = f"""Evaluate if this text contains long-term knowledge value.

Valuable (accept): decisions and reasons, project facts, principles, concepts,
definitions, goals, insights, lessons learned.

Not valuable (reject): temporary notes, shopping lists, chat logs, code snippets,
logs, stream-of-consciousness without substance, trivial reminders.

Text:
{text[:2000]}

Return JSON: {{"valuable": true/false, "reason": "one sentence why"}}"""

    try:
        resp = _get_client().models.generate_content(
            model=LLM_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.1,
                response_mime_type="application/json",
            ),
        )
        result = json.loads(resp.text)
        return result.get("valuable", True), result.get("reason", "")
    except Exception as e:
        logger.warning("Value evaluation failed, accepting by default: %s", e)
        return True, "evaluation_error"


def merge_notes(existing_text: str, new_text: str) -> str:
    """Merge new information into an existing note, preserving structure.

    Returns merged body text (without frontmatter — caller handles that).
    """
    prompt = f"""You are merging new information into an existing note.

Rules:
- Keep ALL existing information intact.
- Add only genuinely NEW facts from the new text.
- Do not duplicate information that already exists.
- If new info contradicts existing, keep BOTH with a note: "[contradiction — needs review]"
- Maintain the existing structure and style.
- Write in the same language as the existing note.
- Return ONLY the merged body text, no frontmatter.

Existing note:
{existing_text[:3000]}

New information:
{new_text[:3000]}

Return the merged note body:"""

    try:
        resp = _get_client().models.generate_content(
            model=LLM_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.2),
        )
        return resp.text.strip()
    except Exception as e:
        logger.warning("Merge failed, appending raw: %s", e)
        return f"{existing_text}\n\n---\n\n## Added\n\n{new_text}"


def _get_graph_suggestions(text: str) -> list[str]:
    """Get related entity names from knowledge graph for wiki-link suggestions."""
    try:
        from .lightrag_engine import get_related_entities
        return get_related_entities(text, limit=15)
    except Exception as e:
        logger.warning("Graph suggestions failed: %s", e)
        return []


def analyze(text: str, vault_path: str) -> dict:
    """Use LLM to generate title, tags, wiki-links, type, and target folder.

    Returns: {title, type, tags, links, folder, confidence}
    """
    existing_notes = _scan_existing_notes(vault_path)
    existing_tags = _scan_existing_tags(vault_path)
    vault_folders = _scan_vault_tree(vault_path)
    graph_entities = _get_graph_suggestions(text)

    notes_sample = existing_notes[:100]
    tags_list = sorted(existing_tags)[:50]

    prompt = f"""Analyze this note and return JSON with:
- "title": concise descriptive title (Russian)
- "type": one of [concept, project, person, principle, decision, goal, source]
- "tags": list of 2-5 tags STRICTLY from the existing tags list below. Do NOT invent new tags.
- "links": list of related notes for [[wiki-links]]. Prefer matches from graph entities and existing notes.
- "folder": best matching folder path STRICTLY from the folder list below
- "confidence": 0.0-1.0 how confident you are in the classification

Vault folder tree (pick ONLY from this list): {json.dumps(vault_folders, ensure_ascii=False)}
Existing note titles: {json.dumps(notes_sample, ensure_ascii=False)}
Related entities from knowledge graph: {json.dumps(graph_entities, ensure_ascii=False)}
Existing tags (use ONLY these, do NOT create new ones): {json.dumps(tags_list, ensure_ascii=False)}

For "links": match graph entities to existing note titles. Only link to notes that actually exist.
If no existing tag fits, return an empty tags list.
If no folder fits well, return empty string for folder.

Note text:
{text[:3000]}

Return ONLY valid JSON, no markdown fences."""

    resp = _get_client().models.generate_content(
        model=LLM_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.2,
            response_mime_type="application/json",
        ),
    )

    try:
        result = json.loads(resp.text)
    except (json.JSONDecodeError, TypeError, ValueError):
        result = {
            "title": text[:60],
            "type": "concept",
            "tags": [],
            "links": [],
            "confidence": 0.3,
        }

    # Enforce closed vocabulary: filter out any tags not in existing set
    if existing_tags:
        result["tags"] = [t for t in result.get("tags", []) if t in existing_tags]

    return result
