# UnionBot

UnionBot is an open-source Discord operations bot for Albion Online guilds.
It helps a guild run registration, Albion API profile sync, LFG events,
attendance, prime-time timer claims, bounties, regear, staff applications,
loot splits, market tools, surveys, dashboards, guides, voice rooms, moderation,
and an optional AI helper.

This public repository intentionally does not include any live server data.
You bring your own Discord bot token, guild/server ID, Albion guild name, and
private SQLite database.

## What It Does

- Verifies Albion characters and assigns lifecycle roles.
- Tracks home-guild, alliance, guest, inactive, and alumni states.
- Creates LFG/event posts with signups, reminders, access roles, and voice rooms.
- Supports prime-time timer claiming for organized content.
- Runs bounty boards, resource missions, regear, loot splits, raffles, and points.
- Builds guild dashboards and activity analytics from synced data.
- Provides guide/knowledge-base answers through optional OpenAI or Ollama AI.
- Offers setup commands to create roles, channels, boards, and policies.

## Public Safety

Never commit these files:

- `.env`
- `data/database.db`
- `data/backups/`
- `data/*.log`
- `data/guild-scan-*.txt`
- screenshots, exports, or channel dumps with member data

Use `.env.example` as the template and keep real secrets private.

## Quick Start

```bash
git clone https://github.com/YOUR-ORG/unionbot.git
cd unionbot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env
mkdir -p data data/backups
```

Edit `.env`, then run:

```bash
.venv/bin/python bot.py
```

For a production Linux service, see [Deployment](docs/DEPLOYMENT.md).

## Discord Setup

1. Create an app in the Discord Developer Portal.
2. Add a bot user.
3. Enable these privileged gateway intents:
   - Server Members Intent
   - Message Content Intent
4. Invite the bot with `bot` and `applications.commands` scopes.
5. Give the bot permissions needed for your enabled features:
   - Manage Roles
   - Manage Channels
   - Manage Messages
   - Send Messages
   - Embed Links
   - Attach Files
   - Read Message History
   - Use Slash Commands
   - Move Members
6. Put the bot role above any roles it needs to assign.

## First Server Bootstrap

After the bot is online in your Discord:

```text
/admin setup-roles
/admin add-guild
/apply set-home-guild
/setup-registration
/lfg post-board
/lfg auto-config
/setup-timer-claims
/staff setup
```

Then use `/set-channel` or feature-specific setup commands to point features at
your real channels. Run `/show-channels` and `/health` to verify configuration.

## Documentation

- [Setup](docs/SETUP.md)
- [Configuration](docs/CONFIGURATION.md)
- [Deployment](docs/DEPLOYMENT.md)
- [Operations](docs/OPERATIONS.md)
- [Security](docs/SECURITY.md)
- [Commands](docs/COMMANDS.md)
- [Architecture](docs/ARCHITECTURE.md)

## AI Helper

The AI helper is optional.

- OpenAI mode uses `OPENAI_API_KEY`.
- Ollama mode can run locally without API spend.
- Public mode defaults should be conservative so the bot does not interrupt
  normal guild chat.

See [Configuration](docs/CONFIGURATION.md) before enabling public AI responses.

## Tests

```bash
.venv/bin/python -m pytest
```

## License

This project is released under the MIT License. See [LICENSE](LICENSE).
