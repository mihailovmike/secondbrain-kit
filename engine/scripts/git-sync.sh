#!/bin/bash
# Git sync for SecondBrain vault on VPS
# Pulls new notes from GitHub, pushes daemon-created notes back.
#
# Usage:
#   chmod +x scripts/git-sync.sh
#   # Add to cron (every 5 minutes):
#   crontab -e
#   */5 * * * * /path/to/secondbrain-kit/engine/scripts/git-sync.sh >> /tmp/sb-git-sync.log 2>&1

VAULT_DIR="${VAULT_PATH:-$HOME/SecondBrain}"

cd "$VAULT_DIR" || { echo "Vault not found: $VAULT_DIR"; exit 1; }

# Pull new notes from remote (via GitHub)
git pull --rebase --autostash origin main 2>&1

# Push daemon-created notes back to remote
git add -A

if ! git diff --cached --quiet; then
  git commit -m "auto: daemon $(date +%Y-%m-%d_%H:%M)" 2>&1
  git push origin main 2>&1
fi
