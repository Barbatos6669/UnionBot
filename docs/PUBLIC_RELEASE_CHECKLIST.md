# Public Release Checklist

Use this before pushing UnionBot to a public GitHub repository.

## Clean History

- [ ] This is a fresh repo, not the private production repo.
- [ ] `git log --all -- .env data '*.db' '*.log'` shows no secrets or databases.
- [ ] No live `.env` exists in the repo.
- [ ] No live database exists in the repo.
- [ ] No logs, screenshots, guild scans, or channel dumps exist in the repo.

## Scan

```bash
.venv/bin/python scripts/public_safety_check.py
git status --short
```

## Rotate Secrets

If the private repo ever committed secrets or databases:

- [ ] Rotate Discord bot token.
- [ ] Rotate OpenAI API key.
- [ ] Recreate any exposed webhooks.
- [ ] Treat exposed database/member data as compromised.

## Public Docs

- [ ] README explains what the bot does.
- [ ] `.env.example` has placeholders only.
- [ ] Setup docs explain Discord Developer Portal, intents, and invite scopes.
- [ ] Deployment docs do not reference a private username/path.
- [ ] Security docs warn against publishing runtime data.

## Release

- [ ] Choose a license.
- [ ] Push the fresh repo.
- [ ] Create an initial tagged release.
- [ ] Keep production `.env` and `data/database.db` private.
