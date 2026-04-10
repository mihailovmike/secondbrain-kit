# VPS Deployment Guide

Deploy SecondBrain on a remote server (Ubuntu/Debian) for 24/7 operation.

## Requirements

- VPS with 1+ GB RAM (2 GB recommended)
- Ubuntu 22.04+ or Debian 12+
- Domain name (optional, for HTTPS)
- SSH access

## Step 1: Server Preparation

```bash
# Connect to your server
ssh user@your-server-ip

# Update system
sudo apt update && sudo apt upgrade -y

# Install Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER

# Install Git
sudo apt install -y git

# Re-login to apply Docker group
exit
ssh user@your-server-ip
```

## Step 2: Clone and Setup

```bash
# Clone the kit
git clone https://github.com/pashkapo/secondbrain-kit.git
cd secondbrain-kit

# Run setup wizard
chmod +x setup.sh
./setup.sh
```

For VPS, set the vault path to something like `/home/user/SecondBrain`.

## Step 3: Git Sync (Vault)

The vault needs to sync with your local machine via GitHub.

### On the VPS

```bash
cd ~/SecondBrain

# Add your GitHub repo as remote
git remote add origin https://github.com/you/my-secondbrain.git

# If using SSH keys (recommended):
git remote add origin git@github.com:you/my-secondbrain.git

# Initial pull
git pull origin main
```

### Set up automatic sync (cron)

```bash
# Make the sync script executable
chmod +x ~/secondbrain-kit/engine/scripts/git-sync.sh

# Set VAULT_PATH for the script
echo 'export VAULT_PATH="$HOME/SecondBrain"' >> ~/.bashrc

# Add to cron (every 5 minutes)
crontab -e
```

Add this line:
```
*/5 * * * * VAULT_PATH=$HOME/SecondBrain $HOME/secondbrain-kit/engine/scripts/git-sync.sh >> /tmp/sb-git-sync.log 2>&1
```

### How sync works

```
Mac (Obsidian Git plugin) → GitHub → VPS (cron) → Daemon processes → GitHub → Mac
```

1. You write/edit notes in Obsidian on your Mac
2. Obsidian Git plugin pushes to GitHub every 5 minutes
3. VPS cron pulls from GitHub every 5 minutes
4. Daemon processes new inbox notes
5. VPS cron pushes processed notes back to GitHub
6. Obsidian Git pulls the processed notes

## Step 4: Expose API (Optional)

### With Nginx reverse proxy

```bash
sudo apt install -y nginx

sudo tee /etc/nginx/sites-available/secondbrain << 'NGINX'
server {
    listen 80;
    server_name memory.yourdomain.com;

    location / {
        proxy_pass http://127.0.0.1:8789;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location /webui/ {
        proxy_pass http://127.0.0.1:9621/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
NGINX

sudo ln -s /etc/nginx/sites-available/secondbrain /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

### Add HTTPS with Certbot

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d memory.yourdomain.com
```

## Step 5: Firewall

```bash
# Allow SSH, HTTP, HTTPS
sudo ufw allow 22
sudo ufw allow 80
sudo ufw allow 443
sudo ufw enable

# API and WebUI are behind Nginx, no need to expose ports directly
```

## Monitoring

### Check daemon status

```bash
docker compose logs -f secondbrain-daemon
```

### Check git sync

```bash
tail -f /tmp/sb-git-sync.log
```

### Check disk usage

```bash
du -sh ~/SecondBrain
docker system df
```

## Updating

```bash
cd ~/secondbrain-kit
git pull
docker compose up -d --build
```

## Troubleshooting

### Daemon not processing notes

```bash
# Check logs
docker compose logs secondbrain-daemon --tail 50

# Restart
docker compose restart secondbrain-daemon
```

### Git sync conflicts

```bash
cd ~/SecondBrain
git status
# If conflicts:
git stash
git pull --rebase origin main
git stash pop
```

### Qdrant out of memory

Edit `docker-compose.yml` and add memory limits:
```yaml
secondbrain-qdrant:
  deploy:
    resources:
      limits:
        memory: 512M
```

### Gemini API rate limits

The free tier allows 60 requests/minute. If you're batch-importing many notes, add them gradually or use the reindex script with `--limit`:

```bash
cd ~/secondbrain-kit
python engine/scripts/reindex_lightrag.py --limit 10
```
