# Contributing

Thanks for improving UnionBot. The project is built from real Albion guild
operations, so practical fixes and clear documentation matter a lot.

## Development Setup

```bash
git clone https://github.com/your-org/UnionBot.git
cd UnionBot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env
mkdir -p data data/backups
```

Run tests:

```bash
.venv/bin/python -m pytest
python3 scripts/public_safety_check.py
```

## Code Style

- Keep public cogs focused on Discord commands and orchestration.
- Put views, buttons, and modals in private helper modules when they grow.
- Put pure parsing, formatting, and calculations in testable helper modules.
- Avoid broad refactors mixed with behavior changes.
- Add tests for bug fixes and edge cases.
- Keep private guild names, Discord IDs, logs, databases, screenshots, and
  channel dumps out of the public repo.

## Pull Request Checklist

- [ ] Tests pass.
- [ ] Public safety check passes.
- [ ] No secrets or runtime data are included.
- [ ] Docs are updated for user-facing behavior changes.
- [ ] The change is scoped enough to review.

## Architecture Notes

Read [Architecture](docs/ARCHITECTURE.md) before moving large pieces around.
The preferred pattern is:

- public cog for commands and scheduled tasks
- private helper module for UI and formatting
- small database/helper module when a feature needs isolated persistence logic
- tests around parsing, dates, permissions, and state transitions
