# First Run Checklist

Use this after UnionBot starts successfully for the first time.

## 1. Confirm The Bot Is Online

In Discord, run:

```text
/health
/help
```

If slash commands do not appear, check:

- `GUILD_DISCORD_ID` is set to your server ID during setup.
- The bot was invited with the `applications.commands` scope.
- The bot logs say commands synced.

## 2. Check Role Position

In Discord server settings, move the bot's role above any role it needs to
assign or remove.

This matters for registration, lifecycle roles, event access roles, content
roles, and staff tools.

## 3. Configure Home Guild Identity

Set your home Albion guild and Discord role:

```text
/apply set-home-guild
/admin add-guild
/admin setup-roles
```

Then confirm the role and nickname tag behavior with one test user before
announcing registration server-wide.

## 4. Set Up Registration

```text
/setup-registration
```

Test the full flow:

1. Click the registration button.
2. Enter an Albion character.
3. Upload the requested character screenshot if your flow requires it.
4. Confirm the bot assigns the expected roles.
5. Confirm officers know where manual review messages appear.

Registration should prove someone is an Albion player. Guild membership and
alliance membership can still need officer review when the Albion API is stale
or missing data.

## 5. Set Up The Event Board

```text
/lfg post-board
/lfg auto-config
```

Test:

1. Create a General LFG.
2. Sign up.
3. Withdraw.
4. Create or join the event voice room if enabled.
5. Cancel the test event and confirm cleanup behaves as expected.

## 6. Set Up Timer Claims

```text
/setup-timer-claims
/primetime track-claims
```

Review the generated tracker. Make sure UTC timer windows, local timestamps,
and LFG links make sense for your guild.

## 7. Configure Channels

Use:

```text
/set-channel
/show-channels
/config-list
```

At minimum, bind channels for:

- registration
- event board
- looking for group posts
- officer review
- announcements
- regear review if used
- bounty board if used

## 8. Try One Real Event

Before rolling everything out, run one small real event through the bot.

Check:

- members understand how to sign up
- reminders are not too spammy
- event voice access works
- attendance is visible afterward
- recap/report output is useful to officers

## 9. Back Up Before Heavy Use

Once members start registering, your database becomes important.

```bash
cp data/database.db "data/backups/manual-$(date +%Y%m%d-%H%M%S).db"
```

Keep backups private.

## 10. Announce One Workflow At A Time

Members adopt systems faster when the rollout is simple.

Good first announcement:

- where to register
- where the event board is
- how to sign up for content
- why signups and event voice help the guild
- who to ping if something breaks

Avoid enabling every feature publicly on day one. Get registration and LFG
working, then add bounties, regear, dashboards, and AI once officers are
comfortable.
