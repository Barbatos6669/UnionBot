"""Help cog — dynamically generated command catalog.

Provides ``/help`` as a top-level slash command. Walks ``bot.tree`` at runtime
so the listing is always in sync with whatever is registered (no static text
to keep updated).

Filtering:
- Commands whose ``default_permissions`` require perms the invoking user lacks
  are hidden, so members don't see admin/audit/staff-only commands.
- Optional ``category`` Choice narrows to a single top-level group.
- Optional ``search`` substring matches against name/description.
"""

from __future__ import annotations

from cogs._typing import Bot
import discord
from discord import app_commands
from discord.ext import commands

from debug import info_log


# Friendly emoji per top-level group. Anything not listed falls back to 📁.
_GROUP_EMOJI: dict[str, str] = {
    "profile":      "👤",
    "leaderboard":  "🏆",
    "graph":        "📈",
    "duty":         "📋",
    "regear":       "🛡️",
    "staff":        "🎖️",
    "automation":   "⚙️",
    "admin":        "🛠️",
    "audit":        "🔍",
}

# Top-level (non-group) commands all bucket into "general".
_GENERAL_BUCKET = "general"
_GENERAL_EMOJI = "✨"
_EMBED_TOTAL_LIMIT = 6000
_EMBED_SAFE_LIMIT = 5600
_FIELD_CAP = 25


def _embed_text_size(embed: discord.Embed) -> int:
    """Approximate Discord's 6000-character embed text budget."""
    total = len(embed.title or "") + len(embed.description or "")
    total += len((embed.footer.text if embed.footer else "") or "")
    total += len((embed.author.name if embed.author else "") or "")
    for field in embed.fields:
        total += len(str(field.name or "")) + len(str(field.value or ""))
    return total


def _can_add_field(embed: discord.Embed, *, name: str, value: str) -> bool:
    if len(embed.fields) >= _FIELD_CAP:
        return False
    return _embed_text_size(embed) + len(name) + len(value) <= _EMBED_SAFE_LIMIT


def _user_has_perms(member: discord.abc.User, required: discord.Permissions | None) -> bool:
    """Return True if the invoking user satisfies a command's default_permissions."""
    if required is None:
        return True
    if not isinstance(member, discord.Member):
        return False
    perms = member.guild_permissions
    # Administrator implicitly grants every permission (Discord rule).
    if perms.administrator:
        return True
    return perms >= required


def _required_perms(cmd: app_commands.Command | app_commands.Group) -> discord.Permissions | None:
    """Resolve the effective default_permissions, walking up parents."""
    perms = cmd.default_permissions
    parent = cmd.parent
    while parent is not None:
        if parent.default_permissions is not None:
            if perms is None:
                perms = parent.default_permissions
            else:
                # Combine — user must satisfy both.
                perms = discord.Permissions(perms.value | parent.default_permissions.value)
        parent = parent.parent
    return perms


class Help(commands.Cog):
    """Dynamic /help command index."""

    def __init__(self, bot: Bot):
        self.bot: Bot = bot
        info_log(f"Initialized {self.__class__.__name__} cog.")

    # ── helpers ──────────────────────────────────────────────────────────────

    def _collect_commands(
        self,
        invoker: discord.abc.User,
    ) -> dict[str, list[tuple[str, str]]]:
        """Return {bucket_name: [(qualified_name, description), ...]} sorted."""
        buckets: dict[str, list[tuple[str, str]]] = {}
        seen: set[str] = set()

        for cmd in self.bot.tree.walk_commands():
            # Only leaf commands (skip the group wrapper itself).
            if isinstance(cmd, app_commands.Group):
                continue
            qname = cmd.qualified_name
            if qname in seen:
                continue
            seen.add(qname)

            # Permission filter.
            if not _user_has_perms(invoker, _required_perms(cmd)):
                continue

            # Bucket = root group name, or "general" for top-level commands.
            root = cmd
            while root.parent is not None:
                root = root.parent
            bucket = root.name if isinstance(root, app_commands.Group) else _GENERAL_BUCKET

            desc = (cmd.description or "").strip() or "—"
            buckets.setdefault(bucket, []).append((qname, desc))

        for entries in buckets.values():
            entries.sort(key=lambda t: t[0])
        return buckets

    @staticmethod
    def _format_bucket(entries: list[tuple[str, str]]) -> list[str]:
        """Turn entries into one or more ≤1024-char field bodies."""
        lines = [f"`/{name}` — {desc}" for name, desc in entries]
        # Pack lines into chunks under the embed-field limit (1024).
        chunks: list[str] = []
        buf: list[str] = []
        size = 0
        for ln in lines:
            add = len(ln) + 1  # newline
            if size + add > 1000 and buf:
                chunks.append("\n".join(buf))
                buf, size = [], 0
            buf.append(ln)
            size += add
        if buf:
            chunks.append("\n".join(buf))
        return chunks

    # ── /help ────────────────────────────────────────────────────────────────

    @app_commands.command(name="help", description="List the commands you can use.")
    @app_commands.describe(
        category="Limit to one category (default: all)",
        search="Filter to commands whose name or description matches this text",
    )
    @app_commands.choices(category=[
        app_commands.Choice(name="All",          value="all"),
        app_commands.Choice(name="General",      value=_GENERAL_BUCKET),
        app_commands.Choice(name="Profile",      value="profile"),
        app_commands.Choice(name="Leaderboard",  value="leaderboard"),
        app_commands.Choice(name="Graph",        value="graph"),
        app_commands.Choice(name="Duty",         value="duty"),
        app_commands.Choice(name="Regear",       value="regear"),
        app_commands.Choice(name="Staff",        value="staff"),
        app_commands.Choice(name="Automation",   value="automation"),
        app_commands.Choice(name="Admin",        value="admin"),
        app_commands.Choice(name="Audit",        value="audit"),
    ])
    async def help_cmd(
        self,
        interaction: discord.Interaction,
        category: app_commands.Choice[str] | None = None,
        search: app_commands.Range[str, 1, 64] | None = None,
    ) -> None:
        cat = category.value if category else "all"
        needle = (search or "").strip().lower()

        buckets = self._collect_commands(interaction.user)

        if cat != "all":
            buckets = {cat: buckets.get(cat, [])}

        if needle:
            filtered: dict[str, list[tuple[str, str]]] = {}
            for k, entries in buckets.items():
                hits = [
                    (n, d) for n, d in entries
                    if needle in n.lower() or needle in d.lower()
                ]
                if hits:
                    filtered[k] = hits
            buckets = filtered

        total_cmds = sum(len(v) for v in buckets.values())
        if total_cmds == 0:
            await interaction.response.send_message(
                "No commands match that filter (or you don't have permission to see any).",
                ephemeral=True,
            )
            return

        title_bits = ["📖 Command Help"]
        if cat != "all":
            title_bits.append(f"· {cat}")
        if needle:
            title_bits.append(f"· “{needle}”")

        embed = discord.Embed(
            title=" ".join(title_bits),
            description=f"{total_cmds} command(s) available to you.",
            color=discord.Color.blurple(),
        )

        # Stable ordering: known groups first (in _GROUP_EMOJI order), then general,
        # then anything else alphabetically.
        ordering = list(_GROUP_EMOJI.keys()) + [_GENERAL_BUCKET]
        ordered_keys = [k for k in ordering if k in buckets] + sorted(
            k for k in buckets if k not in ordering
        )

        # Discord caps at 25 fields and 6000 total embed characters. We may emit
        # multiple fields per bucket if entries don't fit; truncate gracefully if
        # we hit either cap.
        truncated = False
        for key in ordered_keys:
            entries = buckets[key]
            if not entries:
                continue
            emoji = _GROUP_EMOJI.get(key, _GENERAL_EMOJI if key == _GENERAL_BUCKET else "📁")
            chunks = self._format_bucket(entries)
            for i, body in enumerate(chunks):
                suffix = f" ({i + 1}/{len(chunks)})" if len(chunks) > 1 else ""
                field_name = f"{emoji} {key}{suffix}"
                if not _can_add_field(embed, name=field_name, value=body):
                    truncated = True
                    break
                embed.add_field(
                    name=field_name,
                    value=body,
                    inline=False,
                )
            if truncated:
                break

        if truncated:
            embed.set_footer(text="Listing truncated — narrow with `category` or `search`.")
        else:
            embed.set_footer(text="Tip: use `category:` or `search:` to narrow this list.")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /help workflow ───────────────────────────────────────────────────────
    #
    # Task-oriented help: instead of "what commands exist", answers "what do I
    # run when X happens". Curated lists (not auto-generated) so officers get
    # a recipe-card view of recurring jobs. Add a new workflow by appending an
    # entry to ``_WORKFLOWS`` and a Choice to the decorator.

    _WORKFLOWS: dict[str, dict] = {
        "officer-daily": {
            "title": "🛂 Officer — daily check-in",
            "intro": "Run these once a day, in order. Most are ephemeral.",
            "steps": [
                ("/automation dashboard",          "One-glance overview of every pending workload bucket."),
                ("/automation snoozes",            "See if any alerts are currently muted."),
                ("/audit silver-ledger",           "Spot members with unpaid silver."),
                ("/staff board",                   "Confirm staff coverage and rotation."),
                ("/admin health",                  "Verify bot/DB/API are green."),
            ],
        },
        "officer-weekly": {
            "title": "📅 Officer — weekly review",
            "intro": "Quick weekly cadence to keep the guild data clean.",
            "steps": [
                ("/graph dashboard",               "Composite analytics for the past week."),
                ("/graph movers stat:kill_fame",   "Who's grinding hardest right now."),
                ("/audit inactivity",              "Confirm the auto-sweep matches reality."),
                ("/automation inactivity-preview", "Dry-run the inactivity sweep before it posts."),
                ("/admin auto-sync",               "Re-sync any nicknames that drifted from Albion."),
            ],
        },
        "new-member": {
            "title": "👋 When a new member joins",
            "intro": "Mostly automatic — these are the manual escape hatches.",
            "steps": [
                ("/profile register",              "Member self-registers (button on welcome message)."),
                ("/admin setup-roles",             "Re-audit role assignments if something looks off."),
                ("/admin set-lifecycle-role",      "Manually override their lifecycle bucket if needed."),
                ("/admin deregister",              "Wipe a registration so the member can start over."),
            ],
        },
        "first-time-setup": {
            "title": "🧰 First-time server setup",
            "intro": "Run these once, in order, after inviting the bot.",
            "steps": [
                ("/admin setup-roles",                "Create every required role at the right colour/hoist."),
                ("/admin set-roles",                  "Tell the bot which roles map to Unverified/Verified."),
                ("/admin set-channel",                "Repeat for: officer, welcome, goodbye, points, hof, announcements, sso-routes."),
                ("/automation set-channel",           "Repeat for: officer-tasks, announcements, hall-of-fame, topic."),
                ("/automation set-voice-channel",     "Voice channel where event auto-attendance is tracked."),
                ("/admin add-guild",                  "Register your home guild + any tracked rivals."),
                ("/apply set-home-guild",             "Tell applicants which in-game guild to join."),
                ("/admin setup-registration",         "Post the registration button in the welcome channel."),
            ],
        },
        "events": {
            "title": "🎯 Running an event",
            "intro": "From planning a comp to post-event reconciliation.",
            "steps": [
                ("/lfg create",                       "Open a signup post for an event."),
                ("/lfg list",                         "Browse upcoming events."),
                ("/lfg signup-here",                  "Re-post the signup embed (e.g. after a Discord blip)."),
                ("/automation set-voice-channel",     "Configure where attendance is auto-tracked."),
                ("/duty assign",                      "Assign shotcaller/loot-master/etc. for the event."),
                ("/lfg recap",                        "Review attendance after an event."),
            ],
        },
        "silver": {
            "title": "🪙 Silver / regear / bounties",
            "intro": "Treasury and the silver ledger.",
            "steps": [
                ("/regear submit",                    "Member files a regear claim."),
                ("/regear approve  ·  /regear reject","Officer adjudicates a pending claim."),
                ("/bounty post",                      "Propose a new bounty (officer approves)."),
                ("/bounty board  ·  /bounty mine",    "Browse open bounties / yours."),
                ("/audit silver-ledger",              "Who is owed / who owes."),
                ("/automation run-now scope:unpaid_silver", "Force-fire the unpaid-silver reminder."),
            ],
        },
    }

    @app_commands.command(
        name="workflow",
        description="Recipe-card help: what to run when a specific situation comes up.",
    )
    @app_commands.describe(
        topic="Which workflow to look up.",
    )
    @app_commands.choices(topic=[
        app_commands.Choice(name="🛂 Officer daily check-in",     value="officer-daily"),
        app_commands.Choice(name="📅 Officer weekly review",      value="officer-weekly"),
        app_commands.Choice(name="👋 New member joins",           value="new-member"),
        app_commands.Choice(name="🧰 First-time server setup",    value="first-time-setup"),
        app_commands.Choice(name="🎯 Running an event",           value="events"),
        app_commands.Choice(name="🪙 Silver / regear / bounties", value="silver"),
    ])
    async def workflow(
        self,
        interaction: discord.Interaction,
        topic: app_commands.Choice[str],
    ) -> None:
        spec = self._WORKFLOWS[topic.value]
        embed = discord.Embed(
            title=spec["title"],
            description=spec["intro"],
            color=discord.Color.teal(),
        )
        body = "\n".join(
            f"**`{cmd}`**\n   {desc}" for cmd, desc in spec["steps"]
        )
        # Discord field cap is 1024 — these are small enough to fit, but split if not.
        if len(body) <= 1024:
            embed.add_field(name="Steps", value=body, inline=False)
        else:
            chunk: list[str] = []
            size = 0
            n = 1
            for cmd, desc in spec["steps"]:
                line = f"**`{cmd}`**\n   {desc}\n"
                if size + len(line) > 1000 and chunk:
                    embed.add_field(name=f"Steps ({n})", value="\n".join(chunk), inline=False)
                    chunk, size, n = [], 0, n + 1
                chunk.append(line)
                size += len(line)
            if chunk:
                embed.add_field(name=f"Steps ({n})", value="\n".join(chunk), inline=False)
        embed.set_footer(text="Tip: use `/help` for the full alphabetical command list.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: Bot):
    await bot.add_cog(Help(bot))
