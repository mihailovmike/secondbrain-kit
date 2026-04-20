"""Wiki-links, tags, folder classification, value evaluation, and note merging via LLM."""

import json
import logging
import os
import re
from pathlib import Path

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

import yaml as yaml_lib

from .path_sync import VAULT_SKIP_DIRS

_client: genai.Client | None = None
_existing_notes_cache: list[str] | None = None
_existing_tags_cache: set[str] | None = None
_note_types_cache: dict[str, str] | None = None

LLM_MODEL = os.getenv("LLM_MODEL", "gemini-2.5-pro")
CLASSIFY_MODEL = os.getenv("CLASSIFY_MODEL", "gemini-2.5-flash")

# Anchor hub: if a note mentions the owner or any alias, auto-link to hub title.
# Configured via env so ops can adjust without code changes.
ANCHOR_HUB_TITLE = os.getenv("ANCHOR_HUB_TITLE", "").strip()
ANCHOR_HUB_ALIASES = tuple(
    a.strip() for a in os.getenv("ANCHOR_HUB_ALIASES", "").split("|") if a.strip()
)


def _mentions_anchor(text: str) -> bool:
    """True if text mentions hub title or any alias. Case-insensitive, word-bounded."""
    if not ANCHOR_HUB_TITLE:
        return False
    needles = [ANCHOR_HUB_TITLE, *ANCHOR_HUB_ALIASES]
    hay = text.lower()
    for needle in needles:
        if not needle:
            continue
        n = needle.lower()
        # Crude word-boundary: require non-alphanumeric neighbours unless at edge
        idx = hay.find(n)
        while idx != -1:
            left_ok = idx == 0 or not hay[idx - 1].isalnum()
            right = idx + len(n)
            right_ok = right == len(hay) or not hay[right].isalnum()
            if left_ok and right_ok:
                return True
            idx = hay.find(n, idx + 1)
    return False


def _is_anchor_hub_note(text: str) -> bool:
    """True if the note itself is the anchor hub (by frontmatter title or role=owner)."""
    if not text.startswith("---"):
        return False
    end = text.find("\n---", 3)
    if end < 0:
        return False
    fm = text[3:end]
    if "role: owner" in fm or "role:owner" in fm:
        return True
    if ANCHOR_HUB_TITLE:
        # match title: "ANCHOR_HUB_TITLE" or title: ANCHOR_HUB_TITLE
        import re as _re
        pat = _re.compile(r'^title:\s*["\']?' + _re.escape(ANCHOR_HUB_TITLE) + r'["\']?\s*$', _re.MULTILINE)
        if pat.search(fm):
            return True
    return False



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
        if not d.is_dir() or d.name in VAULT_SKIP_DIRS or d.name.startswith("."):
            continue
        for f in d.rglob("*.md"):
            name = f.stem.replace("-", " ").replace("_", " ")
            notes.append(name)
    _existing_notes_cache = notes
    return notes


def get_existing_note_titles(vault_path: str) -> list[str]:
    """Public API: get all note titles in vault."""
    return _scan_existing_notes(vault_path)


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
    """Clear cached notes, tags, and types (call after vault changes)."""
    global _existing_notes_cache, _existing_tags_cache, _note_types_cache
    _existing_notes_cache = None
    _existing_tags_cache = None
    _note_types_cache = None


_DEFAULT_TYPES = {
    "concept": "концепция",
    "decision": "решение",
    "principle": "принцип",
    "project": "проект",
    "person": "персона",
    "goal": "цель",
    "source": "источник",
    "pattern": "паттерн",
    "insight": "инсайт",
}


def get_note_types(vault_path: str | None = None) -> dict[str, str]:
    """Load note types from vault/types.yaml (cached). Returns {key: ru_label}."""
    global _note_types_cache
    if _note_types_cache is not None:
        return _note_types_cache

    vault_path = vault_path or os.getenv("VAULT_PATH", "/app/vault")
    types_file = Path(vault_path) / "_system" / "types.yaml"
    try:
        if types_file.exists():
            raw = yaml_lib.safe_load(types_file.read_text("utf-8"))
            if isinstance(raw, dict):
                # Strip comments from values
                _note_types_cache = {
                    k: v.split("#")[0].strip() if isinstance(v, str) else str(v)
                    for k, v in raw.items()
                }
                return _note_types_cache
    except Exception as e:
        logger.warning("Failed to load types.yaml: %s", e)

    _note_types_cache = dict(_DEFAULT_TYPES)
    return _note_types_cache


def _scan_vault_tree(vault_path: str) -> list[str]:
    """Get list of all folder paths in vault (for LLM folder suggestion)."""
    return list(_scan_vault_tree_with_descriptions(vault_path).keys())


def _read_folder_description(folder_path: Path) -> str:
    """Read short domain description from folder's README.md or index file.

    Looks for README.md first, then index.md. Returns first non-heading,
    non-empty line (max 120 chars) or empty string.
    """
    for name in ("README.md", "index.md"):
        candidate = folder_path / name
        if not candidate.exists():
            continue
        try:
            text = candidate.read_text(encoding="utf-8")[:500]
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("---"):
                    continue
                return line[:120]
        except Exception:
            continue
    return ""


def _scan_vault_tree_with_descriptions(vault_path: str) -> dict[str, str]:
    """Get folder paths with their descriptions (from README.md/index.md).

    Returns {relative_path: description} sorted by path.
    """
    vault = Path(vault_path)
    result: dict[str, str] = {}
    inbox = os.getenv("INBOX_DIR_NAME", "_inbox")
    for d in vault.rglob("*"):
        if not d.is_dir():
            continue
        rel = d.relative_to(vault)
        parts = rel.parts
        if any(p in VAULT_SKIP_DIRS or p.startswith(".") for p in parts):
            continue
        if parts[0] == inbox:
            continue
        desc = _read_folder_description(d)
        result[str(rel)] = desc
    return dict(sorted(result.items()))


def classify_content_type(text: str) -> str:
    """Classify content type before value assessment.

    Returns one of: 'knowledge-note', 'author-content', 'personal-data', 'raw-dump'.
    Uses flash model for cost efficiency.
    """
    # Check frontmatter hints first — provide as context, not bypass
    hints = []
    if "type: channel-post" in text[:500]:
        hints.append("frontmatter indicates channel post")
    if "source: telegram-channel" in text[:500]:
        hints.append("source is telegram channel")
    if "author: mihailov" in text[:500]:
        hints.append("author is mihailov")

    hint_line = ""
    if hints:
        hint_line = f"\nMetadata hints: {', '.join(hints)}.\n"

    prompt = f"""Classify this text into exactly one content type.

Types:
- "knowledge-note" — a note, definition, concept, principle, reference, how-to, or factual explanation
- "author-content" — original authored content: channel post, article, essay, book chapter, opinion piece, publication, personal reflection with insights
- "personal-data" — personal records: health data, lab results, body metrics, financial records, investment calculations, receipts, personal budgets, fitness logs
- "raw-dump" — logs, garbage, raw data without value, clipboard dumps, unstructured fragments
{hint_line}
Text (first 1500 chars):
{text[:1500]}

Return JSON: {{"content_type": "knowledge-note|author-content|personal-data|raw-dump", "reason": "one sentence"}}"""

    try:
        resp = _get_client().models.generate_content(
            model=CLASSIFY_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.1,
                response_mime_type="application/json",
            ),
        )
        result = json.loads(resp.text)
        ct = result.get("content_type", "knowledge-note")
        if ct not in ("knowledge-note", "author-content", "personal-data", "raw-dump"):
            ct = "knowledge-note"
        logger.info("Content type classified: %s — %s", ct, result.get("reason", ""))
        return ct
    except Exception as e:
        logger.warning("Content type classification failed, defaulting to knowledge-note: %s", e)
        return "knowledge-note"


def evaluate_value(text: str, content_type: str = "knowledge-note") -> tuple[bool, str]:
    """L4: Value gate. Ask LLM if this text has long-term knowledge value.

    Returns (is_valuable, reason).
    """
    is_session = "source: claude-session" in text or "source: claude-compact" in text

    # Content type context for the value assessment
    type_context = ""
    if content_type == "author-content":
        type_context = """
IMPORTANT CONTEXT: This is original authored content (channel post, article, essay,
book chapter, or personal publication). Evaluate it as a source of the author's
knowledge, experience, and insights — NOT as a generic knowledge note.
Author content is valuable even if short, because it captures unique perspective and expertise.
Accept unless it is completely trivial or empty of any substance.
"""
    elif content_type == "raw-dump":
        type_context = """
CONTEXT: This text was pre-classified as raw data / dump. Apply strict evaluation.
"""

    if is_session:
        prompt = f"""This is a Claude Code session transcript (dialogue between User and Claude).
Ignore the chat format. Look only at the SUBSTANCE of what was discussed.

Accept if the session contains ANY of: architectural decisions, lessons learned,
solved problems, discovered patterns, important project facts, principles agreed upon.

Reject only if the entire session is: trivial tasks with no insights, pure
mechanical execution with zero decisions, or completely empty of knowledge.

Session transcript (first 2000 chars):
{text[:2000]}

Return JSON: {{"valuable": true/false, "reason": "one sentence about the knowledge found or absent"}}"""
    else:
        prompt = f"""Evaluate if this text contains long-term knowledge value.
{type_context}
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


def extract_knowledge(text: str) -> list[dict]:
    """Extract atomic knowledge units from session/compact transcripts.

    Each unit: {title, type, body, tags, confidence}.
    Returns empty list on failure (not an error).
    """
    note_types = get_note_types()
    types_list = ", ".join(note_types.keys())

    prompt = f"""You are a knowledge extraction system. Analyze this Claude Code
session transcript and extract ALL distinct atomic knowledge units.

For each unit, identify:
- Architectural decisions and their reasons
- User's thinking patterns and preferences
- Discoveries and insights
- Solved problems with root causes
- Principles agreed upon
- Key concepts explained or learned

Return a JSON array. Each element:
{{
  "title": "concise Russian title",
  "type": "one of [{types_list}]",
  "body": "150-500 words in Russian, self-contained explanation",
  "tags": ["2-4 tags in Russian"],
  "confidence": 0.0-1.0
}}

Rules:
- Each unit must be ATOMIC — one idea per unit
- Body must be self-contained (understandable without the session)
- Write in Russian
- Skip trivial/mechanical actions with no insight
- Return empty array [] if no knowledge found

Session transcript:
{text[:8000]}

Return ONLY valid JSON array, no markdown fences."""

    try:
        resp = _get_client().models.generate_content(
            model=LLM_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.2,
                response_mime_type="application/json",
            ),
        )
        result = json.loads(resp.text)
        if not isinstance(result, list):
            return []
        return [
            u for u in result
            if isinstance(u, dict) and u.get("title") and u.get("body")
        ]
    except Exception as e:
        logger.warning("Knowledge extraction failed: %s", e)
        return []


def suggest_folder(text: str, vault_path: str) -> str:
    """Suggest a new folder name when no existing folder matches.

    Uses flash model. Returns lowercase folder path (e.g. 'nutrition/data').
    """
    existing = _scan_vault_tree(vault_path)
    prompt = f"""A note doesn't fit any existing vault folder. Suggest a new folder path.

Rules:
- Use lowercase, no spaces (use hyphens if needed)
- Max 2 levels deep (e.g. "domain/subfolder")
- Follow the style of existing folders
- Return a folder that could hold similar notes in the future

Existing folders: {json.dumps(existing, ensure_ascii=False)}

Note text (first 1000 chars):
{text[:1000]}

Return JSON: {{"folder": "suggested/path", "reason": "one sentence why"}}"""

    try:
        resp = _get_client().models.generate_content(
            model=CLASSIFY_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.2,
                response_mime_type="application/json",
            ),
        )
        result = json.loads(resp.text)
        folder = result.get("folder", "").lower().strip("/")
        logger.info("Folder suggestion: %s — %s", folder, result.get("reason", ""))
        return folder
    except Exception as e:
        logger.warning("Folder suggestion failed: %s", e)
        return ""


def suggest_links(text: str, vault_path: str, limit: int = 5) -> list[str]:
    """Find related existing notes for wiki-links (lighter than full analyze).

    Uses graph entities + fuzzy title matching. No LLM call.
    Returns list of matched note titles.
    """
    existing_notes = _scan_existing_notes(vault_path)
    if not existing_notes:
        return []

    existing_lower = {n.lower(): n for n in existing_notes}
    graph_entities = _get_graph_suggestions(text)
    matched = []

    # Match graph entities to existing note titles
    for entity in graph_entities:
        entity_lower = entity.lower().strip()
        if not entity_lower:
            continue
        # Exact match (highest confidence)
        if entity_lower in existing_lower:
            if existing_lower[entity_lower] not in matched:
                matched.append(existing_lower[entity_lower])
            continue
        # Prefix match or bounded containment (avoid short substrings like "api")
        for note_lower, note_orig in existing_lower.items():
            if note_orig in matched:
                continue
            # Only match if the shorter string is >4 chars (avoid false positives)
            shorter = min(len(entity_lower), len(note_lower))
            if shorter <= 4:
                continue
            # Note title is a prefix of entity or vice versa
            if entity_lower.startswith(note_lower) or note_lower.startswith(entity_lower):
                matched.append(note_orig)
            # Containment only if length ratio > 0.6 (bounded)
            elif shorter / max(len(entity_lower), len(note_lower)) > 0.6:
                if note_lower in entity_lower or entity_lower in note_lower:
                    matched.append(note_orig)

    # Anchor-hub auto-injection: if the note mentions the owner (or an alias)
    # and the hub title exists in vault, always include it. Owner hub itself is
    # excluded to avoid self-referencing.
    if (
        ANCHOR_HUB_TITLE
        and ANCHOR_HUB_TITLE not in matched
        and not _is_anchor_hub_note(text)
        and _mentions_anchor(text)
        and ANCHOR_HUB_TITLE.lower() in {n.lower() for n in existing_notes}
    ):
        # Insert at front so it survives the limit truncation.
        matched = [ANCHOR_HUB_TITLE] + matched

    return matched[:limit]


def analyze(text: str, vault_path: str, content_type: str = "knowledge-note") -> dict:
    """Use LLM to generate title, tags, wiki-links, type, and target folder.

    Returns: {title, type, tags, links, folder, confidence}
    """
    existing_notes = _scan_existing_notes(vault_path)
    existing_tags = _scan_existing_tags(vault_path)
    vault_folders_with_desc = _scan_vault_tree_with_descriptions(vault_path)
    vault_folders = list(vault_folders_with_desc.keys())
    graph_entities = _get_graph_suggestions(text)

    notes_sample = existing_notes[:100]
    tags_list = sorted(existing_tags)[:50]

    # Build folder list with descriptions for better LLM routing
    folder_lines = []
    for path, desc in vault_folders_with_desc.items():
        if desc:
            folder_lines.append(f"{path} — {desc}")
        else:
            folder_lines.append(path)
    folders_display = "\n".join(folder_lines)

    # Content type hints
    folder_hint = ""
    if content_type == "author-content":
        folder_hint = '\nThis is original authored content (channel post, article, essay). For Telegram channel posts, prefer "projects/tg-channel/posts" folder. For other authored content, pick the closest matching folder. Use type "source" for published content.\n'
    elif content_type == "personal-data":
        folder_hint = '\nThis is personal data (health records, financial data, metrics). Pick the most specific domain subfolder (e.g. health/data/, investments/). Use type "source".\n'

    note_types = get_note_types(vault_path)
    types_list = list(note_types.keys())
    types_display = ", ".join(f"{k} ({v})" for k, v in note_types.items())

    prompt = f"""Analyze this note and return JSON with:
- "title": concise descriptive title (Russian)
- "type": pick from existing types: [{', '.join(types_list)}]
  If NONE of the existing types fit well, you may propose a NEW type. In that case:
  set "type" to your proposed key (lowercase english, one word),
  add "new_type_label": "Russian label",
  add "new_type_reason": "one sentence why this type is needed and why existing types don't fit"
- "tags": list of 2-5 tags STRICTLY from the existing tags list below. Do NOT invent new tags.
- "links": list of related notes for [[wiki-links]]. Prefer matches from graph entities and existing notes.
- "folder": best matching folder path STRICTLY from the folder list below. Use the descriptions to understand what each folder contains.
- "confidence": 0.0-1.0 how confident you are in the classification

Existing note types: {types_display}
{folder_hint}
Vault folder tree with descriptions (pick ONLY from this list):
{folders_display}
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

    # Check if LLM proposed a new type
    proposed_type = result.get("type", "concept")
    if proposed_type not in note_types:
        result["is_new_type"] = True
        # Ensure we have label and reason
        if not result.get("new_type_label"):
            result["new_type_label"] = proposed_type
        if not result.get("new_type_reason"):
            result["new_type_reason"] = "LLM proposed without explanation"
        logger.info(
            "New type proposed: %s (%s) — %s",
            proposed_type, result["new_type_label"], result["new_type_reason"],
        )

    return result
