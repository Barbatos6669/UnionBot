# Support

UnionBot is self-hosted software. The public repository provides code and
documentation; it does not include a hosted bot instance.

## Before Asking For Help

Please check:

- [README](README.md)
- [First Run Checklist](docs/FIRST_RUN.md)
- [Setup](docs/SETUP.md)
- [Configuration](docs/CONFIGURATION.md)
- [Security](docs/SECURITY.md)
- [Operations](docs/OPERATIONS.md)

Run:

```bash
python3 scripts/public_safety_check.py
.venv/bin/python -m pytest
```

## What To Include In A Support Request

Include:

- Python version
- operating system
- whether you run manually or with systemd
- the command that failed
- the relevant error from logs
- whether slash commands synced
- which feature you were setting up

Do not include:

- Discord bot token
- OpenAI API key
- `.env`
- `data/database.db`
- database backups
- screenshots with private member data

## Common Problems

Slash commands do not show:

- Set `GUILD_DISCORD_ID` during setup.
- Reinvite the bot with `applications.commands`.
- Restart the bot and watch startup logs.

Bot cannot assign roles:

- Move the bot role above the roles it manages.
- Check it has Manage Roles.

Buttons do nothing after restart:

- Confirm the relevant cog loaded.
- Check logs for startup errors.
- Make sure the button belongs to the current running bot, not an old test bot.

AI answers too much:

- Keep AI disabled or in conservative onboarding/mention-only mode.
- Disable message memory unless you intentionally need it.

## Security Reports

If you find a security issue, do not post secrets, live databases, or member
exports in a public issue. Open a minimal report describing the affected
feature and the risk.
