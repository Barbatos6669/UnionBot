# Bot Commands And Workflows

Keywords: bot unionbot commands slash help profile dashboard graph lfg event registration application regear loot bounty market arbitrage sso routes survey role content ping officer admin staff

Use `/help` in Discord for the full command browser. Many setup, admin, and
staff commands are restricted by Discord permissions or officer roles.

Member-safe commands:

- `/help` shows commands visible to the user.
- `/workflow` shows recipe-card help for common situations.
- `/me`, `/view`, `/whois`, `/balance`, `/timezone`, and `/sync` are profile
  or account helpers.
- `/lfg my-events` shows events the user is signed up for.
- `/points show`, `/points leaderboard`, `/leaderboard`, `/streak-leaderboard`,
  and `/voice-leaderboard` show activity/points information when enabled.
- `/bounty mine`, `/bounty view`, `/bounty top`, `/bounty claim`, `/bounty
  unclaim`, and `/bounty submit` are bounty participation commands.

Registration and onboarding:

- Normal members should register from the configured registration channel by
  clicking the registration button.
- Officers may use `/register-for`, `/relink-character`, `/deregister`,
  `/verify guild`, or application review commands when helping someone manually.
- Staff applications and guild applications use the configured application
  channels and review flows, not random DMs.

LFG and events:

- Create LFGs from the event board buttons, not from SSO routes.
- Officers/shotcallers can use `/lfg post-board` to repost the event board.
- Event attendance commands include `/lfg mark-attended`,
  `/lfg mark-all-attended`, `/lfg recap`, and `/lfg readycheck`.
- Event configuration commands include `/lfg set-post-channel`, `/lfg
  set-type-role`, `/lfg set-type-channel`, `/lfg set-comp`, and `/lfg
  show-config`.

Prime time and schedule:

- `/primetime claims` shows claimed prime timer windows.
- `/primetime track-claims` starts a tracked weekly claims board.
- `/schedule view` and `/schedule post` are for scheduled guild plans.

Bounties, regear, and loot:

- `/bounty board` posts or refreshes the bounty board.
- `/bounty post` creates a bounty.
- `/bounty approve`, `/bounty reject`, `/bounty cancel`, and payment
  confirmation flows are officer-side review tools.
- `/regear post-board`, `/regear list`, and `/setup-regear-policy` support
  regear requests and policy display.
- `/loot split`, `/loot quick-split`, `/loot history`, and `/loot recent`
  support loot tracking.

Market and economy:

- `/market post-now` posts the current market/arbitrage suggestions.
- `/market arbitrage` and `/market scan` are officer/economy tools for finding
  opportunities.
- Market posts are suggestions for buy orders and sell orders. They are not a
  promise that instant buy/sell prices will exist when a player arrives.

Graphs and dashboard:

- `/dashboard` and `/guild-health` show high-level guild analytics.
- `/graph dashboard`, `/graph roster`, `/graph content-mix`, `/graph
  staff-funnel`, `/graph attendance`, and related commands create analytics
  views for officers.

SSO routes:

- SSO route commands and buttons are for route scouting reports only.
- Do not send members to the SSO route board when they ask how to create an
  LFG, join an event, register, or apply.

AI helper:

- `/ai ask` gives a private immediate answer.
- Public AI help is a fallback. In stand-in mode, direct `@UnionBot` mentions
  answer immediately. Normal unanswered member questions wait for the
  configured delay and answer only if another human has not replied.
- Officers can configure the helper with `/ai status`, `/ai set-provider`,
  `/ai set-public-mode`, `/ai set-fallback-delay`, `/ai set-model`, and
  `/ai set-cooldown`.

AI moderation:

- `/mod-ai status` shows the OpenAI moderation setup.
- `/mod-ai enable` and `/mod-ai disable` turn moderation scanning on or off.
- `/mod-ai set-review-channel` chooses where moderation alerts go.
- `/mod-ai set-scan-mode` controls whether eligible public messages are scanned
  broadly or only when they contain obvious risk hints.
- `/mod-ai test` privately checks sample text against the moderation endpoint.
- AI moderation is alert-only by default. It does not delete messages, timeout
  members, or punish anyone automatically.
