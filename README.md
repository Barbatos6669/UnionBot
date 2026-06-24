# UnionBot

[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![discord.py](https://img.shields.io/badge/discord.py-2.x-5865F2)](https://discordpy.readthedocs.io/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

UnionBot is a self-hosted Discord operations bot for Albion Online guilds.
It helps guilds organize content, verify members, run LFG posts, track
attendance, manage bounties, handle regear workflows, and turn Discord activity
into useful officer analytics.

It is built for guilds that want less last-minute chaos and more repeatable
systems: members sign up, callers get cleaner rosters, officers get better
data, and the boring admin work moves into the bot.

## Who It Is For

UnionBot is a good fit if your guild wants to:

- verify Discord users against Albion characters
- organize LFG content with signups, reminders, and temporary voice rooms
- reserve prime-time UTC windows for planned content
- track event attendance and post-event scorecards
- manage bounties, resource missions, loot splits, and regear review
- give members clear content-role pings and guide channels
- run dashboards for recruitment, activity, fame, voice, and guild health
- optionally use an AI helper as a quiet stand-in officer for common questions

UnionBot is not a hosted SaaS product. You run it on your own machine, VPS, or
server, and you keep your Discord token, database, and member data private.

## Feature Overview

| Area | What UnionBot Helps With |
| --- | --- |
| Registration | Albion character linking, lifecycle roles, home guild/alliance/guest states |
| LFG and events | Event board, prime timers, signups, reminders, event voice channels, recaps |
| Attendance | Signup tracking, voice proof, event scorecards, activity history |
| Regear and loot | Regear review tasks, death summaries, loot split tools, event loot input |
| Bounties | Public bounty board, claims, proof submission, officer approval, SSO routes |
| Guild ops | Staff applications, duties, LOA, surveys, dashboards, inactivity tooling |
| Economy | Market/arbitrage helpers, stockpile/chest tracking, shopping bounties |
| Content planning | Comps, guide posts, content-role pings, timer claim boards |
| Optional AI | OpenAI or Ollama helper for registration, LFG, server navigation, and Albion basics |

See [Features](docs/FEATURES.md) for a more detailed walkthrough.

## Quick Start

```bash
git clone https://github.com/your-org/UnionBot.git
cd UnionBot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env
mkdir -p data data/backups
```

Edit `.env`:

```bash
DISCORD_TOKEN=your-token
GUILD_DISCORD_ID=your-discord-server-id
HOME_GUILD_NAME=YourAlbionGuildName
HOME_GUILD_ROLE_NAME=YourGuildDiscordRole
HOME_GUILD_NICK_TAG=TAG
HOME_ALLIANCE_TAG=ALLY
```

Start the bot:

```bash
.venv/bin/python bot.py
```

Then run the bootstrap commands in Discord:

```text
/admin setup-roles
/admin add-guild
/apply set-home-guild
/setup-registration
/lfg post-board
/lfg auto-config
/setup-timer-claims
/staff setup
/health
```

For the full setup guide, use [Setup](docs/SETUP.md). For Linux/systemd,
use [Deployment](docs/DEPLOYMENT.md).

## What You Need

- Python 3.11 or newer
- A Discord application and bot token
- Server Members Intent enabled
- Message Content Intent enabled if you use AI, moderation, or message-based helpers
- A Discord server where you can manage roles/channels
- An Albion Online guild name for home-guild registration checks

Recommended bot permissions:

- Manage Roles
- Manage Channels
- Manage Messages
- Send Messages
- Embed Links
- Attach Files
- Read Message History
- Use Slash Commands
- Move Members

The bot role must sit above any Discord roles it needs to assign.

## Data And Privacy

This public repository does not include live server data. Your production bot
will create local runtime files under `data/`, including the SQLite database,
logs, backups, and generated reports. Keep those files private.

Never publish:

- `.env`
- `data/database.db`
- `data/backups/`
- `data/*.log`
- guild scans, channel dumps, screenshots, or exports containing member data

Run the safety check before publishing forks or releases:

```bash
python3 scripts/public_safety_check.py
```

Read [Security](docs/SECURITY.md) before inviting UnionBot into a live guild.

## Documentation

- [First Run Checklist](docs/FIRST_RUN.md)
- [Features](docs/FEATURES.md)
- [Setup](docs/SETUP.md)
- [Configuration](docs/CONFIGURATION.md)
- [Deployment](docs/DEPLOYMENT.md)
- [Operations](docs/OPERATIONS.md)
- [Security](docs/SECURITY.md)
- [Commands](docs/COMMANDS.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Support](SUPPORT.md)

## Optional AI Helper

The AI helper is optional and disabled unless configured.

- OpenAI mode uses `OPENAI_API_KEY`.
- Ollama mode can run locally without API spend.
- Public response modes should stay conservative so the bot does not interrupt
  normal guild chat.

The AI helper is meant to answer common server and Albion questions when staff
are busy. It should not approve applications, make officer decisions, issue
payouts, or enforce policy on its own.

## Development And Tests

```bash
.venv/bin/python -m pytest
python3 scripts/public_safety_check.py
```

The codebase uses public cogs for Discord command surfaces and private helper
modules for views, formatting, parsing, and analytics. See
[Architecture](docs/ARCHITECTURE.md) before making larger changes.

## Project Status

UnionBot is actively developed from real guild operations. Expect frequent
improvements around LFG, analytics, bounties, AI knowledge, and setup polish.
If you self-host it, keep backups and test updates before deploying them to
your live server.

## License

UnionBot is released under the MIT License. See [LICENSE](LICENSE).
