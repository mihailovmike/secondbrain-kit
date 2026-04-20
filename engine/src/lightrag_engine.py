"""Singleton wrapper around LightRAG with sync API for the watcher pipeline."""

import asyncio
import hashlib
import json
import logging
import os
import re
import threading
from pathlib import Path

from lightrag import LightRAG, QueryParam
from lightrag.llm.gemini import gemini_model_complete, gemini_embed
from lightrag.llm.openai import openai_complete_if_cache
from lightrag.utils import EmbeddingFunc

from .path_sync import VAULT_SKIP_DIRS

logger = logging.getLogger(__name__)

_instance: LightRAG | None = None
_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None
_loop_lock = threading.Lock()

_TIMEOUT = int(os.getenv("LIGHTRAG_TIMEOUT", "300"))


def _get_config() -> dict:
    return {
        "working_dir": os.getenv("LIGHTRAG_WORKING_DIR", ".lightrag"),
        "llm_model": os.getenv("LIGHTRAG_LLM_MODEL", "gemini-2.5-pro"),
        "embedding_model": os.getenv("LIGHTRAG_EMBEDDING_MODEL", "gemini-embedding-001"),
        "embedding_dim": int(os.getenv("LIGHTRAG_EMBEDDING_DIMENSIONS", "3072")),
        "chunk_size": int(os.getenv("LIGHTRAG_CHUNK_SIZE", "1200")),
        # NanoVectorDBStorage (local dev) or QdrantVectorDBStorage (production)
        "vector_storage": os.getenv("LIGHTRAG_VECTOR_STORAGE", "NanoVectorDBStorage"),
    }


def _ensure_loop() -> asyncio.AbstractEventLoop:
    """Get or create a dedicated event loop running in a background thread."""
    global _loop, _thread
    with _loop_lock:
        if _loop is not None and _loop.is_running():
            return _loop

        _loop = asyncio.new_event_loop()

        def _run():
            asyncio.set_event_loop(_loop)
            _loop.run_forever()

        _thread = threading.Thread(target=_run, daemon=True, name="lightrag-loop")
        _thread.start()
        return _loop


def _run_sync(coro, timeout: int | None = None):
    """Run an async coroutine synchronously using the background event loop."""
    loop = _ensure_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout or _TIMEOUT)


def _retry_sync(coro_factory, retries: int = 3, base_delay: float = 2.0, label: str = ""):
    """Retry a coroutine with exponential backoff.

    coro_factory must be a callable that returns a NEW coroutine each call.
    """
    import time as _time
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            return _run_sync(coro_factory())
        except Exception as e:
            last_exc = e
            if attempt < retries:
                delay = base_delay * (2 ** (attempt - 1))
                logger.warning("Retry %d/%d for %s (delay=%.1fs): %s",
                               attempt, retries, label or "operation", delay, e)
                _time.sleep(delay)
    raise last_exc


def _load_definitions_context(vault_path: str) -> str:
    """Read all knowledge/definitions/*.md and build a canonical entity hint string."""
    defs_dir = Path(vault_path) / "knowledge" / "definitions"
    if not defs_dir.exists():
        return ""

    lines = []
    for md_file in sorted(defs_dir.glob("*.md")):
        text = md_file.read_text("utf-8")
        # Extract frontmatter block
        fm_match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
        if not fm_match:
            continue
        fm = fm_match.group(1)

        title_match = re.search(r'^title:\s*["\']?(.+?)["\']?\s*$', fm, re.MULTILINE)
        if not title_match:
            continue
        title = title_match.group(1).strip()

        aliases_match = re.search(r'^aliases:\s*\[(.+?)\]', fm, re.MULTILINE)
        if aliases_match:
            raw = aliases_match.group(1)
            aliases = [a.strip().strip('"\'') for a in raw.split(",")]
            lines.append(f"- {title} (aliases: {', '.join(aliases)})")
        else:
            lines.append(f"- {title}")

    if not lines:
        return ""

    header = "Known entities (use these exact names, do not create variations):"
    return header + "\n" + "\n".join(lines)


_ENTITY_EXTRACTION_GUARDRAIL = """
IMPORTANT: Only extract meaningful knowledge entities (people, concepts, projects, tools, organizations, metrics).
NEVER extract as entities:
- File paths or directory names (/coding/..., ./src/..., ~/.config/...)
- Config file names (.bashrc, .env, .zshrc, .claude.json)
- URLs or URIs (https://..., http://...)
- JSON/YAML keys or values ("approved": true, status: done)
- Code tokens, variable names, function signatures
- UUIDs, hashes, or random identifiers
- Pure numbers or dates without context (2025, 100, 3.14)
- Markdown syntax or formatting tokens
""".strip()


def _make_llm_with_context(base_func, context_str: str):
    """Wrap gemini_model_complete to inject entity hints into extraction prompts."""
    import functools

    guardrail = _ENTITY_EXTRACTION_GUARDRAIL
    if context_str:
        guardrail = f"{context_str}\n\n{guardrail}"

    @functools.wraps(base_func)
    async def wrapper(prompt, system_prompt=None, **kwargs):
        if system_prompt and any(
            kw in system_prompt.lower() for kw in ("entity", "named", "extract")
        ):
            system_prompt = f"{guardrail}\n\n{system_prompt}"
        return await base_func(prompt, system_prompt=system_prompt, **kwargs)

    return wrapper


async def _openrouter_complete(prompt, system_prompt=None, history_messages=None,
                                keyword_extraction=False, **kwargs):
    """LightRAG-compatible wrapper over OpenRouter's OpenAI-compatible API.

    Activated when OPENROUTER_API_KEY is set. Model name comes from
    LIGHTRAG_LLM_MODEL (must be an OpenRouter slug like
    ``google/gemini-2.5-pro``). Embedding stays on Gemini direct.
    """
    return await openai_complete_if_cache(
        os.getenv("LIGHTRAG_LLM_MODEL", "google/gemini-2.5-pro"),
        prompt,
        system_prompt=system_prompt,
        history_messages=history_messages or [],
        api_key=os.getenv("OPENROUTER_API_KEY"),
        base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        **kwargs,
    )


async def _create_instance() -> LightRAG:
    cfg = _get_config()
    working_dir = cfg["working_dir"]
    Path(working_dir).mkdir(parents=True, exist_ok=True)

    vault_path = os.getenv("VAULT_PATH", "")
    ctx_str = _load_definitions_context(vault_path) if vault_path else ""
    if ctx_str:
        logger.info("definitions_context loaded (%d entities)", ctx_str.count("\n- ") + 1)

    # Pick LLM backend: OpenRouter (paid, preferred when key is set) or Gemini direct.
    if os.getenv("OPENROUTER_API_KEY"):
        base_llm = _openrouter_complete
        llm_backend = f"openrouter:{cfg['llm_model']}"
    else:
        base_llm = gemini_model_complete
        llm_backend = f"gemini:{cfg['llm_model']}"

    # Always wrap LLM with guardrail (entity extraction filter) + optional definitions
    llm_func = _make_llm_with_context(base_llm, ctx_str)

    rag = LightRAG(
        working_dir=working_dir,
        llm_model_func=llm_func,
        llm_model_name=cfg["llm_model"],
        embedding_func=EmbeddingFunc(
            embedding_dim=cfg["embedding_dim"],
            max_token_size=2048,
            func=gemini_embed.func,
            model_name=cfg["embedding_model"],
        ),
        embedding_func_max_async=16,
        chunk_token_size=cfg["chunk_size"],
        vector_storage=cfg["vector_storage"],
        addon_params={
            "language": "Russian",
            "entity_extraction_context": ctx_str,
        },
    )
    await rag.initialize_storages()
    logger.info(
        "LightRAG initialized: working_dir=%s llm=%s embedding=%s dim=%d storage=%s",
        working_dir, llm_backend, cfg["embedding_model"],
        cfg["embedding_dim"], cfg["vector_storage"],
    )
    return rag


def get_instance() -> LightRAG:
    """Get or create the singleton LightRAG instance."""
    global _instance
    if _instance is None:
        _instance = _run_sync(_create_instance())
    return _instance


def _cleanup_failed_docs_for_path(rag: "LightRAG", file_path: str) -> int:
    """Delete failed or dup- doc_status records for a given file path.

    Prevents dup- prefix accumulation when a file is retried after a prior
    failed insertion (e.g. LLM API was unavailable). Only removes records
    with status 'failed'/'error' or doc_id starting with 'dup-'.
    Successfully processed records are left untouched.

    Returns: number of records deleted.
    """
    if not hasattr(rag.doc_status, "_data"):
        return 0

    to_delete = []
    for doc_id, info in list(rag.doc_status._data.items()):
        stored_path = getattr(info, "file_path", None) or (
            info.get("file_path") if isinstance(info, dict) else None
        )
        status = getattr(info, "status", None) or (
            info.get("status") if isinstance(info, dict) else None
        )
        if stored_path == file_path and (
            doc_id.startswith("dup-") or str(status) in ("failed", "error")
        ):
            to_delete.append(doc_id)

    deleted = 0
    for doc_id in to_delete:
        if delete_doc(doc_id):
            deleted += 1
            logger.info("Pre-insert cleanup: removed %s record for %s", doc_id[:20], file_path)

    return deleted


def insert(text: str, file_path: str | None = None) -> str | None:
    """Insert a document into LightRAG with retry. Returns track_id."""
    rag = get_instance()

    # Strip YAML frontmatter before insertion — prevents LLM from
    # extracting metadata keys (type, tags, etc.) as entities.
    text = strip_frontmatter(text)

    if file_path:
        _cleanup_failed_docs_for_path(rag, file_path)

    kwargs = {}
    if file_path:
        kwargs["file_paths"] = [file_path]

    track_id = _retry_sync(
        lambda: rag.ainsert(text, **kwargs),
        retries=3,
        base_delay=2.0,
        label=f"insert({file_path or text[:40]})",
    )
    logger.info("Inserted document: %s (track=%s)", file_path or text[:60], track_id)
    return track_id


def query(question: str, mode: str = "mix", top_k: int = 10, stream: bool = False) -> str:
    """Query LightRAG. Returns LLM-generated answer."""
    rag = get_instance()
    param = QueryParam(mode=mode, top_k=top_k, stream=stream)
    result = _run_sync(rag.aquery(question, param=param))
    return result


def query_data(question: str, mode: str = "mix", top_k: int = 10) -> dict:
    """Query LightRAG and return raw context data without LLM generation."""
    rag = get_instance()
    param = QueryParam(mode=mode, top_k=top_k, only_need_context=True)
    result = _run_sync(rag.aquery(question, param=param))
    return result


def stats() -> dict:
    """Return graph statistics."""
    rag = get_instance()
    try:
        storage = rag.chunk_entity_relation_graph
        graph = storage._graph if hasattr(storage, "_graph") else storage
        return {
            "entities": graph.number_of_nodes(),
            "relations": graph.number_of_edges(),
        }
    except Exception:
        return {"entities": 0, "relations": 0}


def find_similar(text: str, top_k: int = 3) -> list[dict]:
    """Search for similar content in the knowledge graph.

    Returns list of {content, source, score} where score is a proxy metric
    based on content overlap (0.0-1.0). LightRAG doesn't expose raw cosine
    scores, so we estimate relevance from content length and overlap.
    """
    from difflib import SequenceMatcher

    try:
        data = query_data(text, mode="mix", top_k=top_k)
        results = []
        if isinstance(data, dict):
            chunks = data.get("chunks", [])
            for c in chunks:
                if not isinstance(c, dict):
                    continue
                content = c.get("content", "")
                source = c.get("file_path", "")
                # Proxy score: sequence similarity between query and result
                score = SequenceMatcher(None, text[:500].lower(), content[:500].lower()).ratio()
                results.append({"content": content, "source": source, "score": round(score, 3)})
        if not results and isinstance(data, str) and data.strip():
            score = SequenceMatcher(None, text[:500].lower(), data[:500].lower()).ratio()
            results.append({"content": data[:500], "source": "graph_context", "score": round(score, 3)})
        return results
    except Exception as e:
        logger.warning("Similarity search failed: %s", e)
    return []


def get_related_entities(text: str, limit: int = 20) -> list[str]:
    """Get entity names related to text, for wiki-link suggestions."""
    try:
        data = query_data(text, mode="local", top_k=limit)
        if isinstance(data, dict):
            entities = data.get("entities", [])
            return [e.get("entity_name", e.get("name", "")) for e in entities if isinstance(e, dict)]
        if isinstance(data, str):
            # Parse entity names from context string
            names = []
            for line in data.split("\n"):
                line = line.strip("- ").strip()
                if line and not line.startswith("#") and len(line) < 100:
                    names.append(line)
            return names[:limit]
    except Exception as e:
        logger.warning("Failed to get related entities: %s", e)
    return []


def get_indexed_doc_ids() -> dict[str, str]:
    """Get all indexed document IDs and their content summaries.

    Returns: {doc_id: content_summary_first_line}
    """
    rag = get_instance()
    try:
        docs = {}
        if hasattr(rag.doc_status, '_data'):
            for doc_id, info in rag.doc_status._data.items():
                summary = info.get("content_summary", "")
                first_line = summary.split("\n")[0][:100] if summary else ""
                docs[doc_id] = first_line
        return docs
    except Exception as e:
        logger.warning("Failed to get indexed docs: %s", e)
        return {}


def get_indexed_paths() -> dict[str, str]:
    """Get all indexed file paths and their doc_ids.

    More reliable than get_indexed_doc_ids() for orphan detection because
    file_path doesn't change when file content changes (unlike content hash).

    Returns: {file_path: doc_id}
    """
    rag = get_instance()
    try:
        paths: dict[str, str] = {}
        if hasattr(rag.doc_status, "_data"):
            for doc_id, info in rag.doc_status._data.items():
                stored_path = getattr(info, "file_path", None) or (
                    info.get("file_path") if isinstance(info, dict) else None
                )
                if stored_path:
                    paths[stored_path] = doc_id
        return paths
    except Exception as e:
        logger.warning("Failed to get indexed paths: %s", e)
        return {}


def delete_doc(doc_id: str) -> bool:
    """Delete a document and its entities/relations from LightRAG."""
    rag = get_instance()
    try:
        result = _run_sync(rag.adelete_by_doc_id(doc_id))
        logger.info("Deleted doc %s: %s", doc_id, result)
        return True
    except Exception as e:
        logger.warning("Failed to delete doc %s: %s", doc_id, e)
        return False


def find_doc_id_by_path(rel_path: str) -> str | None:
    """Find doc_id in LightRAG by file path (relative to vault)."""
    rag = get_instance()
    try:
        if not hasattr(rag.doc_status, '_data'):
            return None
        for doc_id, info in rag.doc_status._data.items():
            stored = getattr(info, "file_path", None) or (
                info.get("file_path") if isinstance(info, dict) else None
            )
            if stored and stored == rel_path:
                return doc_id
    except Exception as e:
        logger.warning("Failed to find doc by path %s: %s", rel_path, e)
    return None


def strip_frontmatter(text: str) -> str:
    """Remove YAML frontmatter from note (same as reindex script)."""
    if not text.startswith("---"):
        return text.strip()
    m = re.match(r"^---\n(.*?)\n---\n*", text, re.DOTALL)
    if m:
        return text[m.end():].strip()
    return text.strip()


def compute_doc_id(content: str) -> str:
    """Canonical doc_id from note content: strip frontmatter, MD5 the body."""
    body = strip_frontmatter(content)
    return f"doc-{hashlib.md5(body.encode()).hexdigest()}"


def get_related_docs_from_graph(file_path: str, working_dir: str, limit: int = 5) -> list[str]:
    """Find documents related to a given file via shared LightRAG entities.

    Uses local KV store files — no LLM/embedding calls required.
    Returns list of relative file paths (e.g. 'knowledge/definitions/законы-логики.md').
    """
    try:
        wdir = Path(working_dir)
        ec_file = wdir / "kv_store_entity_chunks.json"
        tc_file = wdir / "kv_store_text_chunks.json"
        if not ec_file.exists() or not tc_file.exists():
            return []

        entity_chunks = json.loads(ec_file.read_text("utf-8"))
        text_chunks = json.loads(tc_file.read_text("utf-8"))

        # Build: chunk_id → file_path
        chunk_to_doc: dict[str, str] = {}
        for chunk_id, info in text_chunks.items():
            fp = info.get("file_path") if isinstance(info, dict) else None
            if fp:
                chunk_to_doc[chunk_id] = fp

        # Build: entity_name → set(file_paths)
        entity_to_docs: dict[str, set[str]] = {}
        for entity_name, data in entity_chunks.items():
            chunk_ids = data.get("chunk_ids", []) if isinstance(data, dict) else []
            docs = {chunk_to_doc[cid] for cid in chunk_ids if cid in chunk_to_doc}
            if docs:
                entity_to_docs[entity_name] = docs

        # Find entities in this document
        doc_entities = {e for e, docs in entity_to_docs.items() if file_path in docs}
        if not doc_entities:
            return []

        # Count shared entities per related document
        related: dict[str, int] = {}
        for entity in doc_entities:
            for other_doc in entity_to_docs.get(entity, set()):
                if other_doc != file_path:
                    related[other_doc] = related.get(other_doc, 0) + 1

        # Sort by number of shared entities
        return [doc for doc, _ in sorted(related.items(), key=lambda x: -x[1])[:limit]]

    except Exception as e:
        logger.warning("get_related_docs_from_graph failed for %s: %s", file_path, e)
        return []


def find_similar_notes(text: str, vault_path: str, limit: int = 3,
                       exclude_title: str = "") -> list[str]:
    """Find semantically similar notes by graph + embedding search.

    Returns note TITLES (human-readable, for [[wiki-links]]).
    Two strategies combined for best coverage:
      1. KV store graph — free, instant, works for indexed notes
      2. Embedding search (query_data global) — cheap, covers all content

    Cost: one embedding API call (~$0.001) if graph lookup insufficient.
    """
    vault = Path(vault_path)
    exclude_lower = exclude_title.lower()
    working_dir = os.getenv("LIGHTRAG_WORKING_DIR", ".lightrag")

    def _path_to_title(file_path: str) -> str | None:
        """Convert relative vault path to note title."""
        fp = vault / file_path
        if not fp.exists():
            return None
        try:
            raw = fp.read_text("utf-8")[:500]
            if raw.startswith("---"):
                end = raw.find("---", 3)
                if end != -1:
                    for line in raw[3:end].splitlines():
                        if line.strip().startswith("title:"):
                            return line.split(":", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass
        return fp.stem.replace("-", " ").replace("_", " ")

    titles: list[str] = []
    seen: set[str] = set()

    # Strategy 1: KV store graph (free, instant)
    try:
        # Use a dummy file_path to find related docs by shared entities
        # query_data in local mode returns entities, from which we extract doc links
        related_paths = get_related_docs_from_graph("__query__", working_dir, limit=limit * 2)
        for rp in related_paths:
            title = _path_to_title(rp)
            if title and title.lower() != exclude_lower and title.lower() not in seen:
                titles.append(title)
                seen.add(title.lower())
                if len(titles) >= limit:
                    break
    except Exception as e:
        logger.debug("Graph-based similar notes failed: %s", e)

    if len(titles) >= limit:
        return titles[:limit]

    # Strategy 2: Embedding search (cheap — only embedding cost, no LLM)
    try:
        data = query_data(text, mode="global", top_k=limit * 2)
        if isinstance(data, dict):
            chunks = data.get("chunks", [])
            for chunk in chunks:
                if not isinstance(chunk, dict):
                    continue
                fp = chunk.get("file_path", "")
                if not fp:
                    continue
                title = _path_to_title(fp)
                if title and title.lower() != exclude_lower and title.lower() not in seen:
                    titles.append(title)
                    seen.add(title.lower())
                    if len(titles) >= limit:
                        break
    except Exception as e:
        logger.debug("Embedding-based similar notes failed: %s", e)

    return titles[:limit]


# Module-level cache: when we first observed each orphan path.
# Used to enforce min_orphan_age_sec — protects against transient disappearances
# (git pull mid-checkout, rsync, rename-as-delete+create).
_orphan_first_seen: dict[str, float] = {}


def sync_with_vault(vault_path: str, skip_dirs: set[str] | None = None,
                    dry_run: bool = False,
                    min_orphan_age_sec: int = 0) -> dict:
    """Sync LightRAG graph with actual vault files.

    dry_run=True  — report orphans only, no deletion.
    dry_run=False — delete orphans older than min_orphan_age_sec from KG and
                    from Layer 2 (Qdrant archives). Owner hub files are
                    reported via 'owner_missing' but never auto-deleted.
    min_orphan_age_sec — orphan is deleted only after being continuously
                    missing for at least this many seconds. 0 means immediate.

    Returns: {deleted, orphans, deferred, owner_missing, kept}
    """
    import time as _time
    from .path_sync import list_owner_root_paths, classify_orphans

    vault = Path(vault_path)
    if skip_dirs is None:
        skip_dirs = VAULT_SKIP_DIRS
    inbox = os.getenv("INBOX_DIR_NAME", "_inbox")

    # Collect relative paths of all vault files
    vault_paths: set[str] = set()
    for d in vault.iterdir():
        if not d.is_dir() or d.name in skip_dirs or d.name.startswith(".") or d.name == inbox:
            continue
        for f in d.rglob("*.md"):
            if not f.name.startswith("."):
                vault_paths.add(str(f.relative_to(vault)))
    for rel in list_owner_root_paths(str(vault)):
        vault_paths.add(rel)

    indexed_paths = get_indexed_paths()  # {file_path: doc_id}
    if not indexed_paths:
        return {"deleted": [], "orphans": [], "deferred": [], "owner_missing": [], "kept": 0}

    orphan_items = [(fp, did) for fp, did in indexed_paths.items() if fp not in vault_paths]
    ready, deferred, owner_missing = classify_orphans(
        orphan_items, _time.time(), _orphan_first_seen, min_orphan_age_sec,
    )
    orphan_paths = [fp for fp, _ in orphan_items]

    deleted: list[str] = []
    archive_points_removed = 0
    if not dry_run and ready:
        try:
            from . import vector_store
            _delete_archive = vector_store.delete_archive_by_path
        except Exception:
            _delete_archive = None

        for fp, did in ready:
            if delete_doc(did):
                deleted.append(did)
                _orphan_first_seen.pop(fp, None)
                logger.info("Sync: removed orphan doc %s (path=%s)", did, fp)
                if _delete_archive:
                    try:
                        archive_points_removed += _delete_archive(fp)
                    except Exception as e:
                        logger.warning("Archive delete failed for %s: %s", fp, e)
        if deleted:
            logger.info(
                "Vault sync: deleted %d orphan docs (archive points: %d), kept %d",
                len(deleted), archive_points_removed,
                len(indexed_paths) - len(deleted),
            )

    if owner_missing:
        logger.error(
            "Sync: owner hub file(s) missing from vault: %s — NOT deleting from graph",
            owner_missing,
        )

    return {
        "deleted": deleted,
        "orphans": orphan_paths,
        "deferred": deferred,
        "owner_missing": owner_missing,
        "kept": len(indexed_paths) - len(deleted),
        "archive_points_removed": archive_points_removed,
    }


def shutdown():
    """Finalize storages and stop the background event loop."""
    global _instance, _loop
    if _instance is not None:
        try:
            _run_sync(_instance.finalize_storages())
        except Exception as e:
            logger.warning("Error finalizing LightRAG: %s", e)
        _instance = None
    if _loop is not None and _loop.is_running():
        _loop.call_soon_threadsafe(_loop.stop)
        _loop = None
    logger.info("LightRAG shutdown complete")
