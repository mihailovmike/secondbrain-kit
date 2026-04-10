# Detailed Setup Guide

## Prerequisites

### 1. Docker

Download and install Docker Desktop:
- **Mac**: https://docs.docker.com/desktop/install/mac-install/
- **Windows**: https://docs.docker.com/desktop/install/windows-install/
- **Linux**: https://docs.docker.com/engine/install/

After installation, open Docker Desktop and make sure it's running. You should see the Docker icon in your system tray.

Verify in terminal:
```bash
docker --version
# Docker version 27.x.x
```

### 2. Gemini API Key

1. Go to https://aistudio.google.com/apikey
2. Sign in with your Google account
3. Click "Create API Key"
4. Copy the key (starts with `AI...`)

The free tier includes 60 requests per minute — plenty for personal use.

### 3. Obsidian

Download from https://obsidian.md — it's free for personal use.

### 4. Git

Git is needed for vault sync between devices.

```bash
git --version
# If not installed:
# Mac: xcode-select --install
# Ubuntu: sudo apt install git
# Windows: https://git-scm.com/download/win
```

## Installation

### Clone the repository

```bash
git clone https://github.com/pashkapo/secondbrain-kit.git
cd secondbrain-kit
```

### Run the setup wizard

```bash
chmod +x setup.sh
./setup.sh
```

The wizard will ask you:

**Step 1 — Gemini API Key**: Paste the key from aistudio.google.com.

**Step 2 — Vault Location**: Where your notes will live. Default is `~/SecondBrain`. This is the folder you'll open in Obsidian.

**Step 3 — Telegram (optional)**: If you want notifications when notes are processed, renamed, or merged. You need a Telegram bot token (get one from @BotFather) and your chat ID (get from @userinfobot).

**Step 4 — WebUI Password**: Password for the knowledge graph web interface at port 9621.

### What the wizard does

1. Creates your vault from the template (folders, templates, Obsidian config)
2. Initializes git in the vault
3. Generates `.env` with your settings and random API keys
4. Starts 3 Docker containers: Qdrant, Daemon, WebUI

## Open in Obsidian

1. Open Obsidian
2. Click "Open folder as vault"
3. Navigate to your vault path (e.g., `~/SecondBrain`)
4. Click "Open"

### Install Obsidian Git plugin

For syncing notes between devices:

1. Settings (gear icon) → Community plugins → Turn on community plugins
2. Browse → Search "Obsidian Git" → Install → Enable
3. The plugin auto-commits and pushes every 5 minutes by default

### Create a GitHub repo for your vault

```bash
cd ~/SecondBrain  # or your vault path
gh repo create my-secondbrain --private --source=. --push
```

Or create manually on github.com and:
```bash
cd ~/SecondBrain
git remote add origin https://github.com/you/my-secondbrain.git
git push -u origin main
```

## Test It

### Add a note via file

Create a file in `_inbox/`:
```bash
cat > ~/SecondBrain/_inbox/test-note.md << 'EOF'
The Eisenhower Matrix helps prioritize tasks by urgency and importance.
Urgent and important tasks should be done immediately.
Important but not urgent tasks should be scheduled.
Urgent but not important tasks should be delegated.
Neither urgent nor important tasks should be eliminated.
EOF
```

The daemon detects the file within seconds, processes it, and moves it to the appropriate folder.

### Add a note via API

```bash
curl -X POST http://localhost:8789/add \
  -H "X-Api-Key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"text": "Second-order thinking means considering the consequences of consequences. Before making a decision, ask: and then what?", "source": "manual"}'
```

### Search

```bash
curl -X POST http://localhost:8789/search \
  -H "X-Api-Key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "decision making"}'
```

### Check status

```bash
curl http://localhost:8789/stats -H "X-Api-Key: YOUR_API_KEY"
```

## Managing the System

### View logs

```bash
docker compose logs -f secondbrain-daemon
```

### Stop

```bash
docker compose down
```

### Start again

```bash
docker compose up -d
```

### Update to latest version

```bash
git pull
docker compose up -d --build
```

## Configuration Reference

All settings are in `.env`. Key options:

| Variable | Default | Description |
|----------|---------|-------------|
| `GEMINI_API_KEY` | (required) | Google Gemini API key |
| `SECONDBRAIN_API_KEY` | (generated) | API authentication key |
| `HOST_VAULT_PATH` | `~/SecondBrain` | Vault location on your machine |
| `LLM_MODEL` | `gemini-2.5-pro` | LLM for analysis and enrichment |
| `MERGE_THRESHOLD` | `0.85` | Similarity threshold for merging notes |
| `TELEGRAM_BOT_TOKEN` | (optional) | Telegram notifications |
| `TELEGRAM_DM_CHAT_ID` | (optional) | Your Telegram chat ID |
