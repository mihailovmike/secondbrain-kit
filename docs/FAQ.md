# FAQ

## General

### What is SecondBrain Kit?

A self-hosted system that turns your notes into a searchable knowledge graph. You write notes (or dictate them), drop them into a folder, and an AI daemon processes, deduplicates, enriches, and indexes them automatically.

### Do I need to know how to code?

No. The setup wizard handles everything. You need to be comfortable running a few terminal commands (copy-paste from the guide) and using Obsidian.

### How much does it cost?

The software is free (MIT license). You pay for:
- **Gemini API**: free tier (60 req/min) is enough for personal use
- **VPS** (optional): $5-10/month if you want 24/7 operation
- **Obsidian**: free for personal use

### Can I use a different LLM instead of Gemini?

LightRAG supports other providers. You'd need to modify `engine/src/lightrag_engine.py` and `engine/src/linker.py` to use a different model. OpenAI, Anthropic, and local models (via Ollama) are supported by LightRAG.

## Setup

### The setup wizard fails with "Docker is not running"

Start Docker Desktop (or the Docker daemon on Linux) and try again.

### Can I use this without Docker?

Yes, for development. Install Python 3.11+, then:
```bash
cd engine
pip install -r requirements.txt
cp ../.env.example ../.env
# Edit .env: set LIGHTRAG_VECTOR_STORAGE=NanoVectorDBStorage
python -m src.main
```

This uses NanoVectorDB (in-memory) instead of Qdrant — no Docker needed, but data doesn't persist between restarts.

### Can I change the vault location after setup?

Yes. Stop the containers, update `HOST_VAULT_PATH` in `.env`, and restart:
```bash
docker compose down
# Edit .env
docker compose up -d
```

## Usage

### How do I add notes?

Three ways:
1. **File**: create a `.md` file in `_inbox/` folder
2. **API**: `POST /add` with text content
3. **Voice**: `POST /voice` with audio file (ogg, mp3, m4a, wav)

### The daemon rejected my note — why?

Check `rejected.log` in your vault root. Common reasons:
- Too short (<20 words)
- Too long (>5000 words)
- Looks like code or logs (>50% code patterns)
- LLM decided it's not long-term knowledge (shopping lists, temporary reminders)
- Duplicate of an existing note

### How do I create new folders?

Manually in Obsidian or filesystem. The daemon discovers folders automatically but never creates new ones. This is by design — you decide the structure.

### Can I edit processed notes?

Yes, directly in Obsidian. The daemon watches for title changes and updates wiki-links across the vault. Content changes are tracked by the frontmatter cache.

### How does deduplication work?

Two layers:
1. **Title dedup** (L5): if a note with the same filename slug exists, skip it
2. **Semantic dedup** (L6): if content is >85% similar to an existing note, merge the new information into the existing note instead of creating a duplicate

## VPS / Sync

### How does sync work between my Mac and VPS?

```
Mac (Obsidian Git, every 5 min) → GitHub → VPS (cron, every 5 min)
```

The VPS daemon processes notes and pushes results back to GitHub. Your Mac pulls them on the next sync cycle.

### Can I use without a VPS?

Yes. Run everything locally with Docker. The daemon runs on your machine. Sync isn't needed if you're the only user.

### I'm getting git merge conflicts

This usually happens if both the daemon and Obsidian modify the same file simultaneously. The cron sync script uses `--rebase --autostash` to handle most cases. For persistent conflicts:

```bash
cd ~/SecondBrain
git stash
git pull --rebase origin main
git stash pop
# Resolve any remaining conflicts manually
```

## Troubleshooting

### Daemon is not processing new files

1. Check if the daemon is running: `docker compose ps`
2. Check logs: `docker compose logs secondbrain-daemon --tail 20`
3. Make sure the file is in `_inbox/` and ends with `.md`
4. Make sure the file has >20 words of actual content

### API returns 401

Your API key doesn't match. Check `SECONDBRAIN_API_KEY` in `.env` and use it in the `X-Api-Key` header.

### Knowledge graph is empty after adding notes

The graph builds over time. After adding notes, check:
```bash
curl http://localhost:8789/stats -H "X-Api-Key: YOUR_KEY"
```

If entities=0, the notes may have been rejected. Check `docker compose logs secondbrain-daemon`.

### How do I rebuild the index?

If the graph gets corrupted or you want a fresh start:
```bash
# Delete graph data
rm -rf ~/SecondBrain/.lightrag

# Restart daemon (it will reinitialize)
docker compose restart secondbrain-daemon

# Reindex all vault notes
docker compose exec secondbrain-daemon python scripts/reindex_lightrag.py
```
