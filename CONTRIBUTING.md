# Contributing & Sync

SecondBrain Kit is a public distribution mirror of the private `secondbrain-engine` repo.

## Sync Rule

After any significant change to `secondbrain-engine` (`src/`, `hooks/`, `docker-compose.yml`, `.env.example`):

1. Copy changed files to kit mirror paths:
   - `engine/src/` ← `src/`
   - `hooks/` ← `hooks/`
   - `docker-compose.yml`, `.env.example` at root
2. **Never copy**: `.env`, `taskboard/`, `docs/tasks/`, vault notes, personal data
3. Commit kit with the same change description

## What Lives Here

| Path | Content |
|------|---------|
| `engine/` | Core daemon code (mirror of engine `src/`) |
| `hooks/` | Claude Code session hooks |
| `vault-template/` | Starter vault structure — no personal notes |
| `docs/` | Setup, architecture, FAQ |
| `docker-compose.yml` | Production deployment config |

## What Does NOT Live Here

- Personal vault notes (SecondBrain repo is private)
- API keys, `.env` secrets
- Task tracking (`taskboard/`, `docs/tasks/`)
- Engine-specific configs tied to private VPS

## Check sync is current

```bash
diff engine/src/linker.py ~/coding/secondbrain-engine/src/linker.py
diff hooks/secondbrain-session-start.py ~/coding/secondbrain-engine/hooks/secondbrain-session-start.py
```
