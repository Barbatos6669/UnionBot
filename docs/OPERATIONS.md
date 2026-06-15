# Operations

This guide is for running UnionBot safely while the Discord bot is live.

## Safety Rule

Code and documentation edits on disk do not change the running bot until the
process is restarted or a cog is hot-reloaded. For live changes, prefer this
flow:

```bash
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
sudo systemctl restart unionbot
sudo journalctl -u unionbot -f
```

If the change only touches documentation, tests, or developer tooling, no restart
is needed.

## Service Commands

```bash
sudo systemctl status unionbot
sudo systemctl restart unionbot
sudo systemctl stop unionbot
sudo systemctl start unionbot
sudo journalctl -u unionbot -f
```

The service file is `unionbot.service`. It runs:

```bash
/opt/unionbot/.venv/bin/python /opt/unionbot/bot.py
```

## Logs

The bot writes its own logs under `data/`:

- `data/bot.log`: general bot log
- `data/connection.log`: gateway lifecycle events

systemd also captures stdout/stderr:

```bash
sudo journalctl -u unionbot --since "1 hour ago"
```

## Token Rotation

Rotate `DISCORD_TOKEN` in the Discord Developer Portal if it is ever exposed in
chat, screenshots, logs, public repositories, shared backups, or copied into a
ticket.

After rotating:

1. Update `.env`.
2. Run tests.
3. Restart the service.
4. Watch `journalctl` until the bot logs in successfully.

## Backups

The SQLite database lives at `data/database.db`. Backup files are generated in
`data/backups/`.

Before risky maintenance:

```bash
cp data/database.db "data/backups/manual-$(date +%Y%m%d-%H%M%S).db"
```

Keep an eye on disk usage:

```bash
du -sh data data/backups
find data/backups -maxdepth 1 -type f | wc -l
```

If backups grow too large, prune old files only after confirming you have a
recent known-good backup.

## Restart Checklist

Use this before restarting the live bot:

```bash
git status --short
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
sudo systemctl restart unionbot
sudo systemctl status unionbot
```

Then watch logs for a minute:

```bash
sudo journalctl -u unionbot -f
```

Healthy startup should include cog loading, command sync, gateway connect, and a
ready event.

## Hot Reloading

The `sysadmin` cog exposes hot-reload commands for maintainers:

- `/sysadmin reload`
- `/sysadmin reload-all`

Hot reloads are useful for isolated cog changes, but a full service restart is
cleaner after dependency, database, startup, or shared-helper changes.

## Common Incidents

Missing slash commands:

- If `GUILD_DISCORD_ID` is set, commands sync quickly to that guild.
- If it is unset, global sync can take up to an hour.
- Check startup logs for sync errors.

Bot offline after reboot:

- Check `sudo systemctl status unionbot`.
- Check `sudo journalctl -u unionbot --since "30 min ago"`.
- Confirm `.env` still contains a valid token.
- Confirm the network is online.

SQLite/database issues:

- Stop the bot before copying or replacing `data/database.db`.
- Keep `data/database.db`, `data/database.db-wal`, and `data/database.db-shm`
  together if copying a live WAL database.
- Prefer the bot's backup commands when available.
