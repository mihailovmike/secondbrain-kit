"""FastAPI server for RAG queries and vault management."""

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Depends, Header, UploadFile
from pydantic import BaseModel

from .gate import run_all_gates
from .lightrag_engine import (
    get_instance,
    insert as lightrag_insert,
    query as lightrag_query,
    query_data,
    stats as lightrag_stats,
    sync_with_vault,
    delete_doc,
)
from .approval import handle_callback, resend_all as _approval_resend_all
from .processor import process_file
from .telegram import answer_callback
from .voice import process_voice
from .lint import run_lint
from .index_generator import generate_index, write_index
from .path_sync import VAULT_SKIP_DIRS

logger = logging.getLogger(__name__)

app = FastAPI(title="SecondBrain API", version="2.0.0")

VAULT_PATH = os.getenv("VAULT_PATH", "/app/vault")
INBOX_DIR_NAME = os.getenv("INBOX_DIR_NAME", "_inbox")
API_KEY = os.getenv("SECONDBRAIN_API_KEY", "")


# --- Auth ---

async def verify_api_key(x_api_key: str = Header(default="")) -> str:
    if not x_api_key:
        return "internal"
    if x_api_key == API_KEY:
        return "authenticated"
    raise HTTPException(status_code=401, detail="Invalid API key")


# --- Models ---

class SearchRequest(BaseModel):
    query: str
    mode: str = "mix"
    top_k: int = 10


class AddRequest(BaseModel):
    text: str
    source: str = "api"


class AddResponse(BaseModel):
    id: str
    path: str


class AskRequest(BaseModel):
    question: str
    mode: str = "mix"
    top_k: int = 10
    save: bool = False


class AskResponse(BaseModel):
    answer: str
    sources: list[dict]
    saved_as: str | None = None


class StatsResponse(BaseModel):
    total_notes: int
    entities: int
    relations: int
    by_folder: dict[str, int]
    last_modified: str | None
    vector_storage: str


class ArchiveSearchRequest(BaseModel):
    query: str
    top_k: int = 5


class ArchiveAddRequest(BaseModel):
    text: str
    file_path: str = ""
    title: str = ""
    tags: list[str] = []


# --- Endpoints ---

@app.on_event("startup")
async def startup():
    get_instance()
    logger.info("SecondBrain API v2 started (LightRAG)")


@app.post("/search")
async def search_vault(req: SearchRequest, _=Depends(verify_api_key)):
    """Semantic search via LightRAG knowledge graph."""
    data = query_data(req.query, mode=req.mode, top_k=req.top_k)
    return {"query": req.query, "mode": req.mode, "context": data}


@app.post("/archive/search")
async def search_archive(req: ArchiveSearchRequest, _=Depends(verify_api_key)):
    """Vector-only semantic search in Layer 2 archives collection.

    Returns raw fragments without LLM synthesis. Use for finding
    session logs, transcripts, or dumps by semantic similarity.
    """
    try:
        from .vector_store import search_archive as _search
        hits = _search(req.query, top_k=req.top_k)
        return {"query": req.query, "collection": "archives", "hits": hits}
    except Exception as e:
        logger.warning("Archive search failed: %s", e)
        raise HTTPException(status_code=500, detail=f"archive search failed: {e}")


@app.post("/approval/resend-all")
async def approval_resend_all(_=Depends(verify_api_key)):
    """Force-resend every pending approval message to Telegram.

    Deletes stale messages, drops queue entries whose file is gone,
    resends the rest with fresh message IDs and current link URL.
    """
    return _approval_resend_all()


@app.post("/approval/auto-process")
async def approval_auto_process(_=Depends(verify_api_key)):
    """Run the auto-approve worker once over the pending queue.

    Behaviour gated by env vars (see src/auto_approve.py):
    - AUTO_APPROVE_ENABLED=false (default): dry-run; returns what would
      have happened without touching files.
    - AUTO_APPROVE_ENABLED=true: acts on eligible entries (auto-delete
      stale sessions, auto-approve high-confidence novel notes), sends
      Telegram undo notifications.

    Meant to be called by cron (hourly or daily).
    """
    from .auto_approve import auto_process_queue
    return auto_process_queue()


@app.post("/maintenance/weekly")
async def maintenance_weekly(_=Depends(verify_api_key)):
    """Run weekly brain hygiene. Safe to call any time.

    Steps:
    1. sync_with_vault — remove orphan doc_ids (file deleted from vault).
    2. cleanup-layer-noise (dry_run=false) — remove Layer 2/3 docs from KG.
    3. sync-links — inject missing [[wikilinks]] based on KG relations.
    4. Identify orphan notes (no in/out wikilinks) — return list, do NOT delete.
    5. Regenerate _index.md.

    Returns a structured report for inspection / Telegram digest.
    """
    from .lightrag_engine import sync_with_vault, delete_doc
    from .index_generator import write_index
    from .link_integrity import WIKI_LINK_RE, _vault_md_files

    report: dict = {"started_at": datetime.now(timezone.utc).isoformat()}

    # 1. Orphan sync (immediate — weekly cadence means no grace period needed)
    try:
        sync = sync_with_vault(VAULT_PATH, dry_run=False, min_orphan_age_sec=0)
        report["orphan_sync"] = {
            "deleted": len(sync.get("deleted", [])),
            "kept": sync.get("kept", 0),
            "archive_points_removed": sync.get("archive_points_removed", 0),
            "owner_missing": sync.get("owner_missing", []),
        }
    except Exception as e:
        report["orphan_sync_error"] = str(e)

    # 2. Layer 2/3 noise cleanup (reusing the endpoint's logic inline)
    try:
        noise_deleted = 0
        import re as _re
        _FM = _re.compile(r"^---\n(.*?)\n---", _re.DOTALL)
        _LAYER = _re.compile(r"^layer:\s*([123])", _re.MULTILINE)
        rag = get_instance()
        for doc_id, info in list((rag.doc_status._data or {}).items()):
            if not isinstance(info, dict):
                continue
            rel = info.get("file_path") or ""
            if not rel:
                continue
            fp = Path(VAULT_PATH) / rel
            if not fp.exists():
                continue
            try:
                head = fp.read_text(encoding="utf-8")[:1000]
            except Exception:
                continue
            m = _FM.match(head)
            if not m:
                continue
            lm = _LAYER.search(m.group(1))
            if lm and int(lm.group(1)) in (2, 3):
                if delete_doc(doc_id):
                    noise_deleted += 1
        report["layer_noise_deleted"] = noise_deleted
    except Exception as e:
        report["layer_noise_error"] = str(e)

    # 3. Wiki-link sync
    try:
        link_result = _sync_all_links()
        report["link_sync"] = {
            "links_added": link_result.get("total_links_added", 0),
            "notes_updated": len(link_result.get("notes_updated", [])),
        }
    except Exception as e:
        report["link_sync_error"] = str(e)

    # 4. Orphan notes (no wiki-links in any direction)
    try:
        vault = Path(VAULT_PATH)
        incoming: dict[str, int] = {}
        outgoing: dict[str, int] = {}
        titles_by_path: dict[str, str] = {}
        for f in _vault_md_files(VAULT_PATH):
            rel = str(f.relative_to(vault))
            text = f.read_text(encoding="utf-8")
            title_m = re.search(r'^title:\s*"?([^"\n]+)"?', text, re.MULTILINE)
            if title_m:
                titles_by_path[rel] = title_m.group(1).strip()
            out_links = set()
            for m in WIKI_LINK_RE.finditer(text):
                base = m.group(1).split("#")[0].split("|")[0].strip().lower()
                out_links.add(base)
                incoming[base] = incoming.get(base, 0) + 1
            outgoing[rel] = len(out_links)
        orphans = []
        for rel, title in titles_by_path.items():
            in_count = incoming.get(title.lower(), 0)
            out_count = outgoing.get(rel, 0)
            if in_count == 0 and out_count == 0:
                orphans.append({"path": rel, "title": title})
        report["orphan_notes"] = orphans[:50]
        report["orphan_notes_total"] = len(orphans)
    except Exception as e:
        report["orphan_notes_error"] = str(e)

    # 5. Index regen
    try:
        write_index(VAULT_PATH)
        report["index_regenerated"] = True
    except Exception as e:
        report["index_error"] = str(e)

    # 6. Entity description cleanup (<SEP> compression)
    try:
        from .entity_cleanup import clean_sep_descriptions
        cleanup = clean_sep_descriptions(dry_run=False, limit=None, use_llm=True)
        report["entity_desc_cleanup"] = {
            "total_with_sep": cleanup.get("total_with_sep", 0),
            "updated": cleanup.get("updated", 0),
            "failed": cleanup.get("failed", 0),
        }
    except Exception as e:
        report["entity_desc_cleanup_error"] = str(e)

    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    return report


@app.post("/cleanup-layer-noise")
async def cleanup_layer_noise(dry_run: bool = True, _=Depends(verify_api_key)):
    """Remove docs whose file has `layer: 2` or `layer: 3` frontmatter from KG.

    These docs don't belong in the knowledge graph (Layer 2 = archives,
    Layer 3 = private profile). Returns count of docs identified + deleted.

    Query param ``dry_run=false`` actually deletes; default is audit-only.
    """
    import re as _re
    _FM = _re.compile(r"^---\n(.*?)\n---", _re.DOTALL)
    _LAYER = _re.compile(r"^layer:\s*([123])", _re.MULTILINE)

    rag = get_instance()
    vault = Path(VAULT_PATH)
    to_delete: list[tuple[str, str, int]] = []

    data = getattr(rag.doc_status, "_data", None) or {}
    for doc_id, info in data.items():
        if not isinstance(info, dict):
            continue
        rel_path = info.get("file_path") or ""
        if not rel_path:
            continue
        fp = vault / rel_path
        if not fp.exists():
            continue  # orphan — handle via sync_with_vault separately
        try:
            head = fp.read_text(encoding="utf-8")[:1000]
        except Exception:
            continue
        m = _FM.match(head)
        if not m:
            continue
        lm = _LAYER.search(m.group(1))
        if not lm:
            continue
        layer = int(lm.group(1))
        if layer in (2, 3):
            to_delete.append((doc_id, rel_path, layer))

    deleted = 0
    failed = 0
    if not dry_run:
        for doc_id, _rp, _l in to_delete:
            if delete_doc(doc_id):
                deleted += 1
            else:
                failed += 1

    return {
        "candidates": len(to_delete),
        "deleted": deleted,
        "failed": failed,
        "dry_run": dry_run,
        "sample": [{"doc_id": d, "file_path": p, "layer": l}
                   for d, p, l in to_delete[:20]],
    }


@app.post("/archive/add")
async def add_to_archive(req: ArchiveAddRequest, _=Depends(verify_api_key)):
    """Store text directly in Layer 2 archives (no gate, no KG extraction)."""
    try:
        from .vector_store import insert_archive
        point_id = insert_archive(req.text, metadata={
            "file_path": req.file_path,
            "title": req.title,
            "tags": req.tags,
        })
        return {"point_id": point_id, "layer": 2, "collection": "archives"}
    except Exception as e:
        logger.warning("Archive add failed: %s", e)
        raise HTTPException(status_code=500, detail=f"archive add failed: {e}")


@app.post("/add", response_model=AddResponse)
async def add_note(req: AddRequest, _=Depends(verify_api_key)):
    """Add a note to Inbox for processing."""
    gate_ok, gate_reason = run_all_gates(req.text, f"api:{req.source}")
    if not gate_ok:
        raise HTTPException(status_code=422, detail=f"Rejected: {gate_reason}")

    vault = Path(VAULT_PATH)
    inbox = vault / INBOX_DIR_NAME
    inbox.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"{req.source}-{ts}.md"
    filepath = inbox / filename

    filepath.write_text(req.text, encoding="utf-8")
    logger.info(f"Added to Inbox: {filename}")

    return AddResponse(id=ts, path=str(filepath.relative_to(vault)))


@app.post("/ask", response_model=AskResponse)
async def ask_vault(req: AskRequest, _=Depends(verify_api_key)):
    """RAG: knowledge graph + vector search + LLM answer."""
    answer = lightrag_query(req.question, mode=req.mode, top_k=req.top_k)
    answer = answer or "No answer found."

    saved_as = None
    if req.save:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        q_title = req.question[:80].rstrip()
        note_content = f'''---
title: "Q: {q_title}"
type: source
tags: [qa, synthesis]
created: {today}
source: ask
confidence: 0.8
---

# Q: {q_title}

{answer}
'''
        inbox_dir = Path(os.getenv("VAULT_PATH", "/app/vault")) / os.getenv("INBOX_DIR_NAME", "_inbox")
        inbox_dir.mkdir(parents=True, exist_ok=True)
        save_path = inbox_dir / f"ask-{timestamp}.md"
        save_path.write_text(note_content, encoding="utf-8")
        saved_as = str(save_path)
        logger.info(f"Saved /ask answer to {save_path}")

    return AskResponse(answer=answer, sources=[], saved_as=saved_as)


@app.get("/stats", response_model=StatsResponse)
async def vault_stats(_=Depends(verify_api_key)):
    """Vault statistics."""
    vault = Path(VAULT_PATH)
    by_folder = {}
    total = 0
    latest_mtime = 0.0

    for d in vault.iterdir():
        if not d.is_dir() or d.name in VAULT_SKIP_DIRS or d.name.startswith(".") or d.name == INBOX_DIR_NAME:
            continue
        files = list(d.rglob("*.md"))
        by_folder[d.name] = len(files)
        total += len(files)
        for f in files:
            mt = f.stat().st_mtime
            if mt > latest_mtime:
                latest_mtime = mt

    last_mod = (
        datetime.fromtimestamp(latest_mtime, tz=timezone.utc).isoformat()
        if latest_mtime else None
    )

    graph_stats = lightrag_stats()
    cfg_storage = os.getenv("LIGHTRAG_VECTOR_STORAGE", "NanoVectorDBStorage")

    return StatsResponse(
        total_notes=total,
        entities=graph_stats["entities"],
        relations=graph_stats["relations"],
        by_folder=by_folder,
        last_modified=last_mod,
        vector_storage=cfg_storage,
    )


@app.post("/voice")
async def add_voice_note(
    file: UploadFile,
    source: str = "api",
    _=Depends(verify_api_key),
):
    """Upload voice file → transcribe → structure → save to inbox.

    Accepts: ogg, mp3, m4a, wav, webm audio files.
    """
    import tempfile
    suffix = Path(file.filename).suffix if file.filename else ".ogg"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    result_path = process_voice(tmp_path, source=source)

    # Cleanup temp file
    try:
        Path(tmp_path).unlink()
    except Exception:
        pass

    if not result_path:
        raise HTTPException(status_code=422, detail="No valuable content in voice message")

    return {"status": "ok", "path": result_path}


class LintRequest(BaseModel):
    fix: bool = False


@app.post("/lint")
async def lint_vault(req: LintRequest, _=Depends(verify_api_key)):
    """Run 7 integrity checks on vault ↔ LightRAG consistency."""
    return run_lint(fix=req.fix)


@app.get("/index")
async def get_index(_=Depends(verify_api_key)):
    """Generate and return vault index for Claude Code context injection."""
    content = generate_index()
    write_index()
    return {"index": content}


@app.post("/sync")
async def sync_graph(_=Depends(verify_api_key)):
    """Sync graph with vault: remove docs for deleted files."""
    result = sync_with_vault(VAULT_PATH)
    return result


@app.get("/graph")
async def graph_view(entity: str = "", _=Depends(verify_api_key)):
    """Get knowledge graph subgraph around an entity."""
    if entity:
        data = query_data(entity, mode="local", top_k=20)
        return {"entity": entity, "context": data}
    stats = lightrag_stats()
    return {"stats": stats}


def _reindex_vault() -> dict:
    """Re-index all vault notes into LightRAG. Shared by API and watcher."""
    from .path_sync import list_owner_root_paths

    vault = Path(VAULT_PATH)
    indexed = 0
    errors = 0

    def _insert(f: Path) -> None:
        nonlocal indexed, errors
        try:
            content = f.read_text(encoding="utf-8")
            rel_path = str(f.relative_to(vault))
            lightrag_insert(content, file_path=rel_path)
            indexed += 1
        except Exception as e:
            logger.warning("Reindex failed for %s: %s", f, e)
            errors += 1

    for d in vault.iterdir():
        if not d.is_dir() or d.name in VAULT_SKIP_DIRS or d.name.startswith(".") or d.name == INBOX_DIR_NAME:
            continue
        for f in d.rglob("*.md"):
            if f.name.startswith("."):
                continue
            _insert(f)

    # Root-level owner hub files
    for rel in list_owner_root_paths(str(vault)):
        _insert(vault / rel)

    logger.info("Reindex complete: %d indexed, %d errors", indexed, errors)

    try:
        from .index_generator import write_index
        write_index(VAULT_PATH)
    except Exception as e:
        logger.warning("Index write after reindex failed: %s", e)

    return {"indexed": indexed, "errors": errors}


@app.post("/reindex")
async def reindex_vault(_=Depends(verify_api_key)):
    """Re-index all vault notes into LightRAG (rebuild graph)."""
    return _reindex_vault()


def _sync_all_links() -> dict:
    """Inject missing [[wiki-links]] into all vault notes based on LightRAG graph.

    Uses graph KV stores (no LLM/embedding calls) — works offline.
    Finds related notes by shared entities in the knowledge graph and injects
    [[stem-name]] wikilinks into ## Links section.
    Skips personal-data notes.
    """
    from .lightrag_engine import get_related_docs_from_graph

    lightrag_dir = os.getenv("LIGHTRAG_WORKING_DIR", os.path.join(VAULT_PATH, ".lightrag"))
    vault = Path(VAULT_PATH)

    total_links = 0
    notes_updated = []

    for d in vault.iterdir():
        if not d.is_dir() or d.name in VAULT_SKIP_DIRS or d.name.startswith(".") or d.name == INBOX_DIR_NAME:
            continue
        for f in d.rglob("*.md"):
            if f.name.startswith("."):
                continue
            try:
                content = f.read_text("utf-8")
                if "content_type: personal-data" in content[:500]:
                    continue

                rel_path = str(f.relative_to(vault))
                related_paths = get_related_docs_from_graph(rel_path, lightrag_dir, limit=8)
                if not related_paths:
                    continue

                new_count = 0
                for related_path in related_paths:
                    # Convert path to wikilink title (filename stem)
                    link_title = Path(related_path).stem
                    wikilink = f"[[{link_title}]]"
                    if wikilink in content:
                        continue
                    if "\n## Links\n" in content:
                        content = content.replace(
                            "\n## Links\n",
                            f"\n## Links\n- {wikilink}\n",
                        )
                    else:
                        content = content.rstrip("\n") + f"\n\n## Links\n- {wikilink}\n"
                    new_count += 1
                    total_links += 1

                if new_count > 0:
                    f.write_text(content, "utf-8")
                    notes_updated.append(rel_path)

            except Exception as e:
                logger.warning("sync-links failed for %s: %s", f, e)

    logger.info("sync-links: %d links added to %d notes", total_links, len(notes_updated))

    # Regenerate _index.md after link changes
    try:
        from .index_generator import write_index
        write_index(VAULT_PATH)
    except Exception as e:
        logger.warning("Index write after sync-links failed: %s", e)

    return {"total_links_added": total_links, "notes_updated": notes_updated}


@app.post("/sync-links")
async def sync_links(_=Depends(verify_api_key)):
    """Inject missing [[wiki-links]] into all vault notes from LightRAG graph.

    Queries each note's content against the knowledge graph, finds related notes,
    and adds [[Title]] references to ## Links sections. Idempotent — safe to run
    multiple times. Changes are picked up by git-sync.sh on next run.
    """
    return _sync_all_links()


@app.post("/cleanup-entity-descriptions")
async def cleanup_entity_descriptions(
    dry_run: bool = False,
    limit: int | None = None,
    use_llm: bool = True,
    _=Depends(verify_api_key),
):
    """Compress <SEP>-aggregated entity descriptions in the KG.

    Dedups duplicates across SEP-separated fragments; when ≥ 2 unique
    fragments remain and total length exceeds the threshold, Gemini
    paraphrases them into a single coherent description.
    """
    import asyncio
    from .entity_cleanup import clean_sep_descriptions
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: clean_sep_descriptions(dry_run=dry_run, limit=limit, use_llm=use_llm),
    )


@app.post("/dedup-entities")
async def dedup_entities(dry_run: bool = True, _=Depends(verify_api_key)):
    """Find and merge duplicate entity names in the LightRAG graph.

    Rules applied automatically (dry_run=false executes merges):
      - Case variants: "content loop" == "Content Loop"
      - Definition anchors: aliases from knowledge/definitions/*.md
      - Path/slug normalization: "mihailov-flow" == "@mihailov_flow"

    WARN-only (never auto-merged):
      - Fuzzy token overlap (Jaccard >= 0.70)

    Query params:
      dry_run=true  (default) — report only, no merges
      dry_run=false — execute merges
    """
    import asyncio
    from .graph_dedup import run_dedup
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, lambda: run_dedup(vault_path=VAULT_PATH, dry_run=dry_run)
    )
    return result


@app.post("/reindex-sync")
async def reindex_sync(_=Depends(verify_api_key)):
    """Delete orphan docs from graph (docs with no matching vault file).

    Safe to call manually. Does NOT auto-delete on periodic sync.
    """
    from .lightrag_engine import sync_with_vault
    result = sync_with_vault(VAULT_PATH, dry_run=False)
    logger.info("Manual sync: deleted %d orphans", len(result["deleted"]))
    return {
        "deleted": len(result["deleted"]),
        "orphans_removed": result["deleted"],
        "kept": result["kept"],
    }


# --- Telegram callback webhook (called by openclaw-gateway) ---

_SECONDBRAIN_PREFIXES = {"a", "r", "k", "f", "d", "o"}


class TelegramCallbackRequest(BaseModel):
    action: str
    slug: str
    callback_id: str
    chat_id: str
    message_id: int


@app.post("/telegram/callback")
async def telegram_callback(req: TelegramCallbackRequest):
    """Handle Telegram inline button callback routed from openclaw."""
    logger.info("Callback: action=%s slug=%s chat_id=%s msg_id=%s", req.action, req.slug, req.chat_id, req.message_id)
    if req.action not in _SECONDBRAIN_PREFIXES:
        raise HTTPException(400, "Unknown action")
    try:
        handle_callback(
            req.action, req.slug,
            req.callback_id, req.chat_id, req.message_id,
        )
        return {"ok": True}
    except Exception as e:
        logger.error("Callback handler error: %s", e)
        answer_callback(req.callback_id, "❌ Ошибка")
        raise HTTPException(500, str(e))
