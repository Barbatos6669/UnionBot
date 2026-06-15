# Security

UnionBot can manage roles, channels, event access, moderation, and private guild
operations. Treat it like infrastructure.

## Never Publish

- `.env`
- Discord bot token
- OpenAI API key
- SQLite databases
- database backups
- logs
- guild scans
- channel dumps
- screenshots containing private Discord/member data

## Before Making A Repo Public

Run:

```bash
.venv/bin/python scripts/public_safety_check.py
git status --short
git log --all --oneline -- .env data '*.db' '*.log'
```

If `.env`, databases, logs, or member exports appear in git history, do not
publish that repository. Create a fresh repository with clean history.

## Token Rotation

Rotate the Discord token if it is ever pasted into:

- Discord
- GitHub
- logs
- screenshots
- a support ticket
- any public/private place you do not control

After rotation:

1. Update `.env`.
2. Restart the bot.
3. Confirm it logs in.
4. Delete old leaked copies where possible.

## Discord Permissions

Use least privilege where practical. The bot needs elevated permissions only for
features you enable.

The bot role must be above managed roles, but should not be above owner/admin
roles unless you fully trust the deployment.

## AI Safety

If AI is enabled:

- Keep public response mode conservative.
- Rate-limit users.
- Do not let AI expose private channel names to normal members.
- Do not feed private officer chat into public AI answers.
- Keep API keys private and set monthly billing limits.

## Data Retention

Guilds should decide how long to keep message memory, logs, voice activity,
applications, and exit survey data. Shorter retention is safer.
