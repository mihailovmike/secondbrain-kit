"""FastAPI server for RAG queries and vault management."""

import logging
import os
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
)
from .processor import process_file
from .voice import process_voice

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


class AskResponse(BaseModel):
    answer: str
    sources: list[dict]


class StatsResponse(BaseModel):
    total_notes: int
    entities: int
    relations: int
    by_folder: dict[str, int]
    last_modified: str | None
    vector_storage: str


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
    return AskResponse(answer=answer or "No answer found.", sources=[])


@app.get("/stats", response_model=StatsResponse)
async def vault_stats(_=Depends(verify_api_key)):
    """Vault statistics."""
    vault = Path(VAULT_PATH)
    by_folder = {}
    total = 0
    latest_mtime = 0.0
    skip = {"templates", ".obsidian", ".git", ".lightrag", ".entire", ".trash"}

    for d in vault.iterdir():
        if not d.is_dir() or d.name in skip or d.name.startswith("."):
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
