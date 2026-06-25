# Event Attendance, Analytics, And Regear

Keywords: event attendance analytics scorecard event scorecard reconcile reconsile recap report lfg signup sign up voice vc event voice regear gear loss loot split raffle stat growth fame movement albionbb killboard

UnionBot's LFG system exists so the guild can turn content into useful data.

The simple rule for members:

1. Sign up for the event.
2. Join the event voice channel.
3. Stay with the group.
4. Follow the caller.
5. Use event tools for loot, regear, and attendance.

## Why Signups Matter

Signups are not just RSVP fluff. They tell leadership:

- What content members are interested in.
- Which timers are strongest.
- Whether a caller has enough people before form-up.
- Which roles/comps might be missing.
- Whether the guild should run more of that content later.

If people attend without signing up, the content may still happen, but the data
gets weaker.

## Why Event Voice Matters

Event voice is the strongest attendance proof the bot can track.

Voice attendance helps with:

- Confirming who actually showed up.
- Seeing when members joined or dropped during the run.
- Building event scorecards.
- Running raffles from real attendance later.
- Supporting regear decisions when someone dies during guild content.

If a member says they were at an event but never joined voice, tell them the bot
may not be able to prove attendance cleanly.

## Event Scorecards

Event scorecards are best-effort analytics, not perfect truth.

They may include:

- VC attendance flow over time.
- Signups vs voice-confirmed attendees.
- Fame/stat movement from stored Albion profiles.
- Killboard or AlbionBB battle context when available.
- Kills, deaths, estimated gear loss, and loot value.
- Role/IP context when battle data includes builds.
- Officer-entered loot value and net silver.

The bot can miss data if:

- Members never signed up.
- Members skipped event voice.
- Albion API/killboard data is delayed.
- The event went longer than the planned window.
- Deaths happened outside the event window.
- Loot value was never entered.

If the bot says a report is waiting on data, it usually means it queued a retry
or needs officer input such as loot value.

## Regear Context

Event reconcile posts a consolidated regear review list. It does not create one
individual officer regear card per death, because large CTA wipes can spam the
officer channel. Officers should use the list/continuation embeds to review the
death rows, then handle payouts or ask players for manual regear submissions as
needed.

The cleanest regear proof is:

- The member signed up.
- The member joined event voice.
- The death happened during the event window or active event voice window.
- The death matches the content being run.
- Gear value can be estimated from killboard/death data or entered by staff.

Regear reports are officer review tools. The bot can prepare requests and
estimate value, but officers still decide policy, approval, and payout.

## Loot Splits

Loot split and event loot value are separate from regear.

- Tradeable loot can be split normally.
- Untradeable silver bags or non-tradeable value may need to be entered as a
  separate silver split.
- Some members may opt out of a split.
- Officers should record the final split so the ledger and event report stay
  useful.

If someone asks why the bot cares about this, explain:

"The world runs on data for a reason. Signups, voice attendance, loot, and
regear data tell us what content works, who shows up, and how to improve the
guild without guessing."
