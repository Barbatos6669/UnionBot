# Commands

This is a map of UnionBot's slash-command surface by feature area. Discord
permission checks still apply; many officer and admin commands require
`Manage Guild` or bot-maintainer access.

Use `/help` in Discord for the user-facing command browser.

## General Help

- `/help`: list commands visible to the current user.
- `/workflow`: show recipe-card help for common situations.

## Admin And Setup

General officer setup and guild maintenance commands:

- `/setup-roles`
- `/setup-registration`
- `/setup-shotcaller-sop`
- `/setup-timer-claims`
- `/setup-officer-lifecycle-guide`
- `/set-channel`
- `/show-channels`
- `/set-milestone-threshold`
- `/set-roles`
- `/add-guild`
- `/deregister`
- `/register-for`
- `/set-lifecycle-role`
- `/relink-character`
- `/set-tu-history`
- `/tu-history`
- `/set-inactivity-days`
- `/set-lifecycle-thresholds`
- `/assign-role`
- `/sync-now`
- `/auto-sync`
- `/reconcile-now`
- `/config-list`
- `/health`
- `/db-backup`
- `/setup-regear-policy`
- `/ping-shotcallers`
- `/officer-cheatsheet`

## Applications And Verification

- `/apply setup`
- `/apply set-review-channel`
- `/apply set-wait`
- `/apply requirements`
- `/apply set-home-guild`
- `/apply check-now`
- `/apply pending`
- `/verify guild`

## User Profiles

- `/view`
- `/balance`
- `/timezone`
- `/sync`
- `/me`
- `/whois`

## LFG And Attendance

- `/lfg post-board`
- `/lfg set-post-channel`
- `/lfg set-reminder-lead`
- `/lfg my-events`
- `/lfg stats`
- `/lfg set-comp`
- `/lfg show-config`
- `/lfg auto-config`
- `/lfg set-type-role`
- `/lfg set-type-channel`
- `/lfg unset-type-channel`
- `/lfg unset-type-role`
- `/lfg scan-guild`
- `/lfg dump-channel`
- `/lfg apply-staff-perms`
- `/lfg reset-staff-perms`
- `/lfg propose-layout`
- `/lfg apply-layout`
- `/lfg cleanup-duplicates`
- `/lfg pin-intros`
- `/lfg mark-attended`
- `/lfg mark-all-attended`
- `/lfg recap`
- `/lfg readycheck`

## Schedule And Primetime

- `/schedule add`
- `/schedule remove`
- `/schedule toggle`
- `/schedule view`
- `/schedule post`
- `/schedule generate`
- `/primetime heatmap`
- `/primetime weekday`
- `/primetime events`
- `/primetime claims`
- `/primetime track-claims`
- `/primetime untrack-claims`

## Points And Leaderboards

- `/points show`
- `/points leaderboard`
- `/points rank`
- `/points config-show`
- `/points config-set`
- `/points config-reset`
- `/points add`
- `/points subtract`
- `/points reset`
- `/leaderboard`
- `/streak-leaderboard`
- `/voice-leaderboard`

## Bounties And Shopping

- `/bounty post`
- `/bounty board`
- `/bounty view`
- `/bounty claim`
- `/bounty unclaim`
- `/bounty submit`
- `/bounty approve`
- `/bounty reject`
- `/bounty cancel`
- `/bounty queue`
- `/bounty mine`
- `/bounty top`
- `/bounty config set-channel`
- `/bounty config set-review-channel`
- `/bounty config set-flex-channel`
- `/shopping summary`
- `/shopping remove`

## Loot, Regear, And Stockpile

- `/loot split`
- `/loot quick-split`
- `/loot history`
- `/loot recent`
- `/regear post-board`
- `/regear set-review-channel`
- `/regear list`
- `/setup-regear-policy`
- `/chest add`
- `/chest remove`
- `/chest stock`
- `/chest missing`
- `/chest log`

## Staff, Duties, And LOA

- `/apply`
- `/withdraw`
- `/slots`
- `/applications`
- `/repost-pending`
- `/approve`
- `/deny`
- `/config`
- `/record-grant`
- `/tenure`
- `/rebalance-toggle`
- `/rebalance`
- `/setup-board`
- `/refresh-board`
- `/board`
- `/done`
- `/add`
- `/remove`
- `/mine`
- `/seed-defaults`
- `/loa start`
- `/loa end`
- `/loa status`
- `/loa list`
- `/loa set`

Some commands in this area are top-level because they come from staff and duty
cogs. Discord command permissions and descriptions are the source of truth.

## Comps And Content

- `/comp create`
- `/comp list`
- `/comp view`
- `/comp add-slot`
- `/comp remove-slot`
- `/comp set-swaps`
- `/comp duplicate`
- `/comp archive`
- `/comp delete`
- `/comp refresh-items`
- `/content suggest`
- `/content pool`
- `/content show`
- `/content open`
- `/content close`
- `/content config`
- `/content clear-pool`
- `/content post-board`
- `/content nextvote`
- `/content nextvote-close`
- `/content-roles set-channel`
- `/content-roles post-panel`
- `/content-roles show-config`

## Market And Economy

- `/market scan`
- `/market arbitrage`
- `/market watch list`
- `/market watch add`
- `/market watch remove`
- `/market set-channel`
- `/market post-now`

## Graphs And Dashboards

- `/graph player`
- `/graph kd`
- `/graph guild`
- `/graph track-player`
- `/graph track-guild`
- `/graph untrack-player`
- `/graph untrack-guild`
- `/graph activity`
- `/graph track-activity`
- `/graph untrack-activity`
- `/graph roster`
- `/graph content-mix`
- `/graph staff-funnel`
- `/graph movers`
- `/graph heatmap`
- `/graph dashboard`
- `/graph track-dashboard`
- `/graph untrack-dashboard`
- `/graph cohort`
- `/graph standing`
- `/graph attendance`
- `/graph attendance-trend`
- `/graph recruitment-funnel`
- `/graph set-digest-channel`
- `/dashboard`
- `/guild-health`

## Announcements, Tickets, And Moderation

- `/announce post`
- `/announce config set-crest`
- `/announce config set-color`
- `/announce config set-footer`
- `/announce config show`
- `/help-ticket list`
- `/help-ticket mine`
- `/help-ticket view`
- `/help-ticket take`
- `/help-ticket solve`
- `/help-ticket cancel`
- `/help-ticket config set-channel`
- `/help-ticket config set-review-channel`
- `/blacklist add`
- `/blacklist remove`
- `/blacklist list`
- `/blacklist check`
- `/vibe status`
- `/vibe enable`
- `/vibe disable`
- `/vibe set-timeout`
- `/vibe set-category-name`
- `/vibe pardon`

## Recruitment

- `/recruit add`
- `/recruit update`
- `/recruit status`
- `/recruit followup`
- `/recruit leaderboard`
- `/recruit pin-leaderboard`

## Reminders And Temporary Access

- `/reminder add`
- `/reminder list`
- `/reminder cancel`
- `/temp-access status`
- `/temp-access set-role`
- `/temp-access grant`
- `/temp-access scan`
- `/temp-access clear`

## Sysadmin And Emergency

- `/sysadmin audit-config`
- `/sysadmin backup-now`
- `/sysadmin telemetry`
- `/sysadmin reload`
- `/sysadmin reload-all`
- `/emergency shutdown`

Use these carefully. They affect the running bot process or live operational
state.
