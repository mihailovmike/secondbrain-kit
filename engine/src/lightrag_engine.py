"""Singleton wrapper around LightRAG with sync API for the watcher pipeline."""

import asyncio
import logging
import os
import threading
from pathlib import Path

from lightrag import LightRAG, QueryParam
from lightrag.llm.gemini import gemini_model_complete, gemini_embed
from lightrag.utils import EmbeddingFunc

logger = logging.getLogger(__name__)

_instance: LightRAG | None = None
_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None


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
    if _loop is not None and _loop.is_running():
        return _loop

    _loop = asyncio.new_event_loop()

    def _run():
        asyncio.set_event_loop(_loop)
        _loop.run_forever()

    _thread = threading.Thread(target=_run, daemon=True, name="lightrag-loop")
    _thread.start()
    return _loop


def _run_sync(coro):
    """Run an async coroutine synchronously using the background event loop."""
    loop = _ensure_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=300)


async def _create_instance() -> LightRAG:
    cfg = _get_config()
    working_dir = cfg["working_dir"]
    Path(working_dir).mkdir(parents=True, exist_ok=True)

    rag = LightRAG(
        working_dir=working_dir,
        llm_model_func=gemini_model_complete,
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
        },
    )
    await rag.initialize_storages()
    logger.info(
        "LightRAG initialized: working_dir=%s llm=%s embedding=%s dim=%d storage=%s",
        working_dir, cfg["llm_model"], cfg["embedding_model"],
        cfg["embedding_dim"], cfg["vector_storage"],
    )
    return rag


def get_instance() -> LightRAG:
    """Get or create the singleton LightRAG instance."""
    global _instance
    if _instance is None:
        _instance = _run_sync(_create_instance())
    return _instance


def insert(text: str, file_path: str | None = None) -> str | None:
    """Insert a document into LightRAG. Returns track_id."""
    rag = get_instance()
    kwargs = {}
    if file_path:
        kwargs["file_paths"] = [file_path]
    track_id = _run_sync(rag.ainsert(text, **kwargs))
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
    """Search for similar content in the knowledge graph. Returns list of {content, score}."""
    try:
        data = query_data(text, mode="mix", top_k=top_k)
        if isinstance(data, dict):
            chunks = data.get("chunks", [])
            if chunks:
                return [{"content": c.get("content", ""), "source": c.get("file_path", "")}
                        for c in chunks if isinstance(c, dict)]
        if isinstance(data, str) and data.strip():
            return [{"content": data[:500], "source": "graph_context"}]
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
    import re
    if not text.startswith("---"):
        return text.strip()
    m = re.match(r"^---\n(.*?)\n---\n*", text, re.DOTALL)
    if m:
        return text[m.end():].strip()
    return text.strip()


def sync_with_vault(vault_path: str, skip_dirs: set[str] | None = None) -> dict:
    """Sync LightRAG graph with actual vault files.

    Deletes docs from graph that no longer exist in vault.
    Hashes content WITHOUT frontmatter (same as reindex script).
    Returns: {deleted: [doc_ids], kept: int}
    """
    from pathlib import Path
    import hashlib

    vault = Path(vault_path)
    if skip_dirs is None:
        skip_dirs = {"templates", ".obsidian", ".git", ".lightrag", ".entire", ".trash"}
    inbox = os.getenv("INBOX_DIR_NAME", "_inbox")

    # Collect hashes of vault file bodies (stripped of frontmatter)
    vault_hashes = set()
    for d in vault.iterdir():
        if not d.is_dir() or d.name in skip_dirs or d.name.startswith(".") or d.name == inbox:
            continue
        for f in d.rglob("*.md"):
            try:
                raw = f.read_text(encoding="utf-8").strip()
                body = strip_frontmatter(raw)
                if body and len(body) >= 20:
                    vault_hashes.add(f"doc-{hashlib.md5(body.encode()).hexdigest()}")
            except Exception:
                continue

    # Compare with indexed docs
    indexed = get_indexed_doc_ids()
    if not indexed:
        return {"deleted": [], "kept": 0}

    deleted = []
    for doc_id in indexed:
        if doc_id not in vault_hashes:
            if delete_doc(doc_id):
                deleted.append(doc_id)
                logger.info("Sync: removed orphan doc %s (%s)", doc_id, indexed[doc_id])

    result = {"deleted": deleted, "kept": len(indexed) - len(deleted)}
    if deleted:
        logger.info("Vault sync: deleted %d orphan docs, kept %d", len(deleted), result["kept"])
    return result


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
