# Features

UnionBot is a modular Discord bot for Albion Online guild operations. You can
run only the pieces your guild needs and leave optional systems disabled until
you are ready for them.

## Registration And Member State

UnionBot links Discord members to Albion characters and uses that information
to help staff understand who is in the guild, alliance, guest pool, inactive
group, or alumni group.

Common uses:

- verify that a Discord user has an Albion character
- tag home-guild members with a nickname prefix
- distinguish home guild, alliance, guest, inactive, and alumni roles
- spot members whose Albion guild changed
- support manual officer review when the Albion API is missing data

## LFG, Signups, And Voice

The event board gives members and shotcallers a structured way to create
content instead of relying on last-minute pings.

Common uses:

- create General LFG events
- reserve prime-time UTC timer windows
- collect signups and withdrawals
- post reminders before content starts
- create temporary event voice channels
- restrict event voice access to signed-up members
- reconcile attendance after the event

## Event Analytics

UnionBot turns event activity into officer-readable reports. The reports are
best-effort analytics, not automatic judgment.

Scorecards can include:

- signups vs voice-confirmed attendance
- voice attendance flow over the event window
- PvP, PvE, gathering, crafting, and death fame movement
- AlbionBB battle context when available
- top attendee movement
- loot input and estimated net results
- regear review rows for officer follow-up

## Prime Timer Claims

Prime timer claims help guilds avoid overlap and give members predictable
content windows.

Common uses:

- show the next seven Albion timer days
- display UTC windows with local Discord timestamps
- mark claimed and open slots
- link claimed slots back to the LFG post
- keep content organized around recurring windows

## Bounties And Resource Missions

The bounty board lets officers or members create tasks with rewards and a clean
claim/proof/approval flow.

Common uses:

- resource gathering missions
- scouting tasks
- SSO route submissions
- energy core delivery
- public bounty board
- officer review queue
- confirm-paid cleanup for silver payouts

## Regear, Loot, And Stockpile

UnionBot can help staff keep economy workflows consistent.

Common uses:

- regear request board
- officer review tasks
- loot split calculators
- event loot input
- chest/stockpile tracking
- shopping bounty summaries

## Staff, Duties, Surveys, And Guild Health

The bot includes tools for guild administration and member feedback.

Common uses:

- staff applications
- duty boards
- leave-of-absence tracking
- optional surveys
- activity dashboards
- lifecycle and inactivity tooling
- officer briefings

## Guides, Comps, And Content Roles

UnionBot can post and maintain structured guides, comp lists, and content-role
panels.

Common uses:

- content ping roles
- comp storage and views
- guides and tutorial posts
- shotcaller SOPs
- faction, roads, dungeon, and ZvZ planning material

## Optional AI Helper

The AI helper can answer common questions when staff are busy. It is optional
and should be kept conservative in public channels.

Good AI use cases:

- registration help
- LFG/event board help
- server navigation
- content-role explanations
- Albion beginner questions
- quick reminders about guild workflows

The AI should not approve applications, issue payouts, ban members, or make
leadership decisions.

## What To Enable First

For a new install, start small:

1. Registration
2. LFG event board
3. Content roles
4. Prime timer claims
5. Regear or bounty boards
6. Dashboards and automation
7. Optional AI helper

The safest path is to get one workflow working, announce it, let members use
it, then add the next workflow.
