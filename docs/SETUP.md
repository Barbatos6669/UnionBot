# Setup

This guide takes a new guild from a blank server to a working UnionBot install.

## 1. Create The Discord App

In the Discord Developer Portal:

1. Create a new application.
2. Open the Bot page and create a bot user.
3. Copy the bot token into `.env` as `DISCORD_TOKEN`.
4. Enable privileged intents:
   - Server Members Intent
   - Message Content Intent
5. Save changes.

Invite the bot with these scopes:

- `bot`
- `applications.commands`

Recommended permissions:

- Manage Roles
- Manage Channels
- Manage Messages
- Send Messages
- Embed Links
- Attach Files
- Read Message History
- Use Slash Commands
- Move Members

The bot's Discord role must be above any role it will assign.

## 2. Install Locally

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
HOME_GUILD_ROLE_NAME=YourGuildRoleName
HOME_GUILD_NICK_TAG=YG
HOME_ALLIANCE_TAG=ALLY
```

`GUILD_DISCORD_ID` is recommended during setup because commands sync to that
server quickly. If you remove it later, Discord global command sync can take up
to an hour.

## 3. Start The Bot

```bash
.venv/bin/python bot.py
```

Wait until the bot logs that slash commands synced.

## 4. Bootstrap Discord

Run these commands in your server as an administrator:

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

Then configure feature channels:

```text
/set-channel
/show-channels
/health
```

Feature-specific setup commands can create boards, policy posts, and channel
bindings for registration, staff applications, LFG, bounties, regear, guides,
market posts, and automation.

## 5. Test Core Workflows

Before announcing the bot:

1. Register a test Albion character.
2. Create one General LFG.
3. Sign up and withdraw from that LFG.
4. Create a temporary voice channel from a join-to-create trigger.
5. Run `/help`.
6. Run `/health`.
7. Confirm the bot can assign and remove its managed roles.

## 6. Production

For a production host, use systemd or another process manager. See
[Deployment](DEPLOYMENT.md).
