# Deployment

This guide assumes a Linux host with Python 3.11+ and systemd.

## Install

```bash
sudo useradd --system --create-home --home-dir /opt/unionbot unionbot
sudo mkdir -p /opt/unionbot
sudo chown unionbot:unionbot /opt/unionbot
sudo -u unionbot git clone https://github.com/YOUR-ORG/unionbot.git /opt/unionbot
cd /opt/unionbot
sudo -u unionbot python3 -m venv .venv
sudo -u unionbot .venv/bin/pip install -r requirements.txt
sudo -u unionbot cp .env.example .env
sudo -u unionbot mkdir -p data data/backups
```

Edit `/opt/unionbot/.env` with your real token and guild settings.

## Service

```bash
sudo cp /opt/unionbot/unionbot.service /etc/systemd/system/unionbot.service
sudo systemctl daemon-reload
sudo systemctl enable unionbot
sudo systemctl start unionbot
sudo systemctl status unionbot
```

Logs:

```bash
sudo journalctl -u unionbot -f
tail -f /opt/unionbot/data/bot.log
```

Restart after code/config changes:

```bash
sudo systemctl restart unionbot
```

## Updates

```bash
cd /opt/unionbot
sudo -u unionbot git pull
sudo -u unionbot .venv/bin/pip install -r requirements.txt
sudo -u unionbot .venv/bin/python -m pytest
sudo systemctl restart unionbot
```

## Backups

The live database is `data/database.db`.

```bash
cd /opt/unionbot
sudo -u unionbot cp data/database.db "data/backups/manual-$(date +%Y%m%d-%H%M%S).db"
```

Keep database backups private. They can contain Discord IDs, Albion names,
event history, officer decisions, and guild configuration.
