# Architecture

UnionBot is a Python `discord.py` application. The entry point is `bot.py`,
which loads environment variables, creates the custom bot class, initializes the
SQLite database, auto-loads cogs from `cogs/`, and syncs slash commands.

## Project Layout

- `bot.py`: process entry point, cog loading, command sync, gateway lifecycle,
  top-level retry/backoff, and graceful shutdown.
- `config.py`: defaults, role/rank configuration, and small pure helpers.
- `sql_database.py`: SQLite schema creation, migrations, and feature data
  access methods.
- `debug.py`: file and console logging helpers.
- `albion_api.py`: Albion Online API client helpers.
- `market_api.py`: Albion market price helpers.
- `cogs/`: Discord slash commands, buttons, views, scheduled tasks, and feature
  orchestration.
- `tests/`: pytest coverage for pure helpers and selected bot workflows.
- `data/`: live SQLite databases, logs, generated exports, and backups.
- `scripts/`: one-off maintenance/seed scripts.

## Startup Flow

1. `.env` is loaded.
2. `DISCORD_TOKEN` is required.
3. `GUILD_DISCORD_ID` is optional and controls guild-scoped command sync.
4. `UnionBot` initializes Discord intents and the SQLite database.
5. All public cogs in `cogs/*.py` are loaded.
6. Slash commands sync to the dev guild or globally.
7. The bot connects to Discord and runs until stopped.

Some cogs are required for safe production startup. If one of those fails to
load, startup aborts instead of running a partially broken bot.

## Data Model

The bot uses SQLite at `data/database.db`. `sql_database.py` creates tables with
`CREATE TABLE IF NOT EXISTS` and performs simple migrations with `ALTER TABLE`
where needed.

Operational notes:

- WAL mode is enabled for better reader/writer behavior.
- The database connection is closed during bot shutdown.
- Backups are stored under `data/backups/`.
- Database and log files are ignored by git.

## Cog Pattern

Each public cog is a `cogs/*.py` file that exposes `async def setup(bot)`.
Private helper modules use a leading underscore, for example `_lfg_views.py` or
`_automation_helpers.py`, and are not auto-loaded as Discord extensions.

Good feature boundaries:

- Command definitions stay in the public cog.
- Views/buttons/modals can live in private helper modules.
- Pure formatting, parsing, and calculation helpers should be testable without
  Discord.
- Database access should be kept behind `Database` methods or a small feature
  data-access module.

## Testing

Run:

```bash
.venv/bin/python -m pytest
```

The current tests focus on pure helpers and selected workflows. Good future test
targets are:

- database methods with temporary SQLite files
- command helper functions before Discord responses are sent
- attendance, points, bounty, and staff edge cases
- migration behavior for old database files

## Linting

Run:

```bash
.venv/bin/python -m ruff check .
```

The ruff configuration lives in `pyproject.toml`.

## Refactor Targets

The highest-value cleanup target is `sql_database.py`, because it contains many
feature areas in one large file. A safe migration path is:

1. Move one feature's SQL methods into a small module.
2. Keep the public method names stable.
3. Add tests around the moved behavior.
4. Repeat feature by feature.

Large cogs such as `admin.py`, `automation.py`, `lfg.py`, `bounties.py`,
`events.py`, and `graphs.py` are also good candidates for gradual extraction.
The existing `_lfg_*`, `_automation_*`, `_graphs_*`, and `_content_*` modules are
the right pattern to keep following.

## Live Bot Caution

Avoid broad refactors while the bot is actively needed. Prefer small changes
that can be tested and restarted cleanly. For live-safe work, documentation,
tests, and pure helper cleanup are the lowest-risk places to start.
