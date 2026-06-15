# Configuration

UnionBot uses two configuration layers:

1. `.env` for secrets and boot-time defaults.
2. The SQLite `guild_config` table, usually managed through slash commands.

Do not commit `.env` or `data/database.db`.

## Required `.env`

```bash
DISCORD_TOKEN=
```

## Recommended `.env`

```bash
GUILD_DISCORD_ID=
HOME_GUILD_NAME=HomeGuild
HOME_GUILD_ROLE_NAME=HomeGuild
HOME_GUILD_NICK_TAG=HG
HOME_ALLIANCE_TAG=ALLY
```

`HOME_GUILD_NAME` should match the Albion Online guild name.

`HOME_GUILD_ROLE_NAME` is the Discord role UnionBot treats as the home-guild
role. Many servers set it to their guild name or guild tag.

`HOME_GUILD_NICK_TAG` is the nickname prefix for home-guild members, for
example `[HG] PlayerName`.

`HOME_ALLIANCE_TAG` is used as the default alliance nickname tag before the
Albion API or guild config provides a better value.

## Optional AI

```bash
OPENAI_API_KEY=
OPENAI_BASE_URL=https://api.openai.com/v1
OLLAMA_URL=http://127.0.0.1:11434
OLLAMA_MODEL=llama3.2:3b
```

OpenAI is optional. If no key is set, keep AI disabled or use Ollama.

Public AI modes should stay conservative:

- `off`: no public AI replies.
- `onboarding`: registration/help style support only.
- `standin`: answers when users are stuck and no staff respond.
- `mentions`: replies only when mentioned.

## Runtime Config

Most channel and role bindings are stored in the bot database. Use:

```text
/set-channel
/show-channels
/lfg show-config
/lfg auto-config
/apply set-home-guild
/staff config
/health
```

The database is private operational state. Back it up, but do not publish it.

## Branding

This public version uses generic names:

- UnionBot
- HomeGuild
- ALLY
- HG

Change these through `.env`, setup commands, and Discord role/channel names.
Avoid editing code for normal branding changes unless you are maintaining a
fork.
