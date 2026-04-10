# Architecture

## System Overview

SecondBrain Kit is a pipeline that turns raw text into structured, interlinked knowledge notes.

```
Input sources          Processing daemon          Storage
─────────────         ──────────────────         ─────────
Obsidian (_inbox/)  →  File watcher             → Vault (folders)
REST API (/add)     →  Quality gates (L1-L6)    → LightRAG (knowledge graph)
Voice API (/voice)  →  LLM enrichment           → Qdrant (vectors)
MCP (remember)      →  Auto wiki-links          → Git (history)
```

## Processing Pipeline

Each note goes through 6 quality layers before entering the vault:

```
Raw text
  │
  ├─ L1: Hash dedup ──────── seen this exact file before? → skip
  ├─ L2: Size gate ───────── <20 or >5000 words? → reject
  ├─ L3: Content quality ─── >50% code/logs? → reject
  ├─ L4: Value gate (LLM) ── long-term knowledge? → reject if not
  ├─ L5: Title dedup ─────── same slug exists? → skip
  └─ L6: Semantic dedup ──── >0.85 similarity? → merge into existing
         │
         ├─ [MERGE] → combine new info into existing note
         │            → update LightRAG index
         │
         └─ [NEW]  → LLM analysis: title, type, tags, folder, links
                    → write to vault folder
                    → insert into LightRAG
                    → git commit
```

## Components

### Daemon (`engine/src/`)

| Module | Responsibility |
|--------|---------------|
| `main.py` | Entry point: starts watcher + API server |
| `watcher.py` | File system monitor (inbox + vault changes) |
| `processor.py` | Main pipeline: gates → dedup → enrich → write |
| `gate.py` | Quality gates L1-L5 |
| `linker.py` | LLM calls: analysis, value evaluation, merge |
| `lightrag_engine.py` | LightRAG singleton wrapper |
| `path_sync.py` | Wiki-link updates on rename/move |
| `link_integrity.py` | Broken link detection and cleanup |
| `voice.py` | Audio → structured note via Gemini |
| `api.py` | FastAPI REST endpoints |
| `mcp_server.py` | MCP protocol for AI agents |

### Storage Layers

**Vault (filesystem)** — the source of truth. Markdown files organized in folders. Every note has YAML frontmatter (title, type, tags, created, source, confidence).

**LightRAG (knowledge graph)** — entities, relations, and chunks extracted from notes. Powers semantic search, multi-hop queries, and wiki-link suggestions.

**Qdrant (vector DB)** — stores embeddings for similarity search. Used by LightRAG internally. Can be replaced with NanoVectorDB for local development.

**Git** — version history and sync mechanism. The daemon auto-commits processed notes. Obsidian Git plugin and VPS cron handle sync.

## Design Principles

See [engine/docs/PRINCIPLES.md](../engine/docs/PRINCIPLES.md) for the full list. Key rules:

1. **One meaning = one place** — no duplicate knowledge. Similar notes merge.
2. **Closed vocabulary** — tags and folders only from existing vault. No auto-creation.
3. **Daemon never guesses** — low confidence → inbox with `needs_review: true`.
4. **Everything is reversible** — reindex rebuilds the graph from vault files.
5. **Single data flow** — inbox → daemon → vault → graph. No side branches.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/add` | Add note to inbox |
| POST | `/search` | Semantic search |
| POST | `/ask` | RAG question answering |
| POST | `/voice` | Upload audio → structured note |
| POST | `/sync` | Sync graph with vault |
| GET | `/stats` | Vault statistics |
| GET | `/graph` | Knowledge graph subgraph |

## Docker Services

| Service | Image | Port | Purpose |
|---------|-------|------|---------|
| `secondbrain-daemon` | Custom (Python 3.11) | 8789 | Processing + API |
| `secondbrain-qdrant` | qdrant/qdrant | — | Vector storage |
| `secondbrain-webui` | ghcr.io/hkuds/lightrag | 9621 | Graph visualization |
