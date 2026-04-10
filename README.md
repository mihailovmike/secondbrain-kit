# SecondBrain Kit

Self-hosted AI-powered knowledge management system. Drop notes into a folder — the daemon processes, deduplicates, enriches, and indexes them into a searchable knowledge graph.

Built on [Obsidian](https://obsidian.md) + [LightRAG](https://github.com/HKUDS/LightRAG) + [Gemini](https://aistudio.google.com).

## What It Does

```
You write a note (text, voice, API)
    → drop it into _inbox/
        → Daemon detects the new file
            → Quality gates (too short? code dump? duplicate?)
            → LLM evaluates: is this worth keeping long-term?
            → Semantic dedup: merge if similar note exists (>0.85)
            → LLM enrichment: title, tags, type, wiki-links
            → Sort into the right folder
            → Index in knowledge graph
            → Git commit
```

You get: a clean, interlinked vault of atomic notes you can search, query, and browse in Obsidian.

## Features

- **6-layer quality pipeline** — hash dedup, size gate, content quality, LLM value gate, title dedup, semantic dedup
- **Knowledge graph** — entities, relations, multi-hop RAG queries via LightRAG
- **Auto wiki-links** — notes link to related existing notes automatically
- **Path sync** — rename a note in Obsidian, wiki-links update across the vault
- **Voice notes** — send audio, Gemini transcribes and structures it
- **REST API** — `/add`, `/search`, `/ask`, `/stats`, `/voice`
- **MCP server** — use your brain from Claude Code or any MCP-compatible agent
- **Web UI** — browse the knowledge graph visually (LightRAG WebUI)

## Quick Start

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) installed and running
- [Gemini API key](https://aistudio.google.com/apikey) (free tier works)
- [Obsidian](https://obsidian.md) (free, for viewing/editing notes)

### Setup (2 minutes)

```bash
git clone https://github.com/pashkapo/secondbrain-kit.git
cd secondbrain-kit
chmod +x setup.sh
./setup.sh
```

The wizard asks 4 questions:
1. Your Gemini API key
2. Where to put the vault (default: `~/SecondBrain`)
3. Telegram notifications (optional)
4. WebUI password

Then it creates the vault, writes `.env`, and starts Docker containers.

### Open in Obsidian

1. Open Obsidian → "Open folder as vault" → select your vault path
2. Install community plugin: **Obsidian Git** (for sync)
3. Start writing notes or drop files into `_inbox/`

## Architecture

```
┌──────────────┐     ┌──────────────────────┐     ┌──────────┐
│   Obsidian   │     │   SecondBrain Daemon  │     │  Qdrant  │
│   (editor)   │◄───►│  (Python + FastAPI)   │◄───►│(vectors) │
│              │     │                      │     │          │
│  ~/SecondBrain/    │  - file watcher      │     └──────────┘
│   ├─ _inbox/ │────►│  - quality gates     │
│   ├─ knowledge/    │  - LLM enrichment    │     ┌──────────┐
│   ├─ goals/  │     │  - LightRAG graph    │◄───►│  Gemini  │
│   └─ ...     │     │  - path sync         │     │  (LLM)   │
└──────────────┘     │  - REST API (:8789)  │     └──────────┘
                     └──────────────────────┘
                              │
                     ┌──────────────────┐
                     │  LightRAG WebUI  │
                     │   (:9621)        │
                     └──────────────────┘
```

## API

```bash
API_KEY="your-key-from-setup"

# Add a note
curl -X POST http://localhost:8789/add \
  -H "X-Api-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"text": "The Pareto principle states that 80% of results come from 20% of effort.", "source": "manual"}'

# Search knowledge graph
curl -X POST http://localhost:8789/search \
  -H "X-Api-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "productivity principles"}'

# Ask a question (RAG)
curl -X POST http://localhost:8789/ask \
  -H "X-Api-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"question": "What do I know about decision-making?"}'

# Vault stats
curl http://localhost:8789/stats -H "X-Api-Key: $API_KEY"
```

## MCP Server (for Claude Code)

Connect your brain to Claude Code:

```bash
claude mcp add --global secondbrain -- \
  python /path/to/secondbrain-kit/engine/src/mcp_server.py
```

Set environment variables:
```bash
export SECONDBRAIN_API_URL=http://localhost:8789
export SECONDBRAIN_API_KEY=your-key
```

Tools: `remember`, `recall`, `ask`, `brain_stats`.

## Vault Structure

```
SecondBrain/
├── _inbox/              # Drop notes here — daemon processes them
├── knowledge/
│   └── definitions/     # Terms, concepts, glossary
├── goals/               # Objectives, OKRs
├── templates/           # Note templates (6 types)
├── .obsidian/           # Obsidian config
├── .lightrag/           # Knowledge graph data (auto-managed)
└── CLAUDE.md            # Agent instructions
```

The daemon discovers folders dynamically. Create new folders yourself — the daemon sorts notes into existing ones but never creates new folders.

## Docs

- [Detailed Setup Guide](docs/SETUP.md) — step-by-step with screenshots
- [VPS Deployment](docs/VPS-DEPLOY.md) — deploy on Ubuntu/Debian server
- [Architecture](docs/ARCHITECTURE.md) — how the pipeline works
- [FAQ](docs/FAQ.md) — common questions

## Stack

| Component | Technology |
|-----------|-----------|
| Editor | Obsidian |
| Daemon | Python 3.11, FastAPI, watchdog |
| Knowledge Graph | LightRAG |
| Vector DB | Qdrant |
| LLM | Google Gemini |
| Embeddings | Gemini Embedding 001 (3072d) |
| Sync | Git + Obsidian Git plugin |
| Deploy | Docker Compose |

## License

MIT
