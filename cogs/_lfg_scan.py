"""Guild-inventory scan dump used by /lfg scan-guild and the hourly sync.

Walks the live guild for channels/roles/categories and produces a
human-readable text snapshot, optionally writing it to ``data/`` for
diffing across syncs. Pure functions — no Cog state.

Sibling module of cogs/lfg.py. The ``_`` prefix means the cog
auto-loader skips this file; cogs/lfg.py imports the public names
back at module load.
"""
from __future__ import annotations

import datetime

import discord

from debug import error_log

from cogs._lfg_config import (
    CFG_BOARD_CHANNEL,
    CFG_CHAN_PREFIX,
    CFG_LFG_CHANNEL,
    CFG_ROLE_PREFIX,
    EVENT_TYPES,
    NOTABLE_PERM_FLAGS,
)


# ── Guild scan dump (used by /lfg scan-guild AND the hourly inventory sync) ─

# NOTABLE_PERM_FLAGS is imported from :mod:`cogs._lfg_config`.


def _notable_permissions(perms: discord.Permissions) -> list[str]:
    """Return a sorted list of elevated permission names this role has.

    Excludes ``administrator`` (handled by the caller) and chat basics like
    ``read_messages`` / ``send_messages`` which everyone holds. Output is
    suitable for direct rendering in the scan file.
    """
    return [flag for flag in NOTABLE_PERM_FLAGS if getattr(perms, flag, False)]


def build_guild_scan_text(guild: discord.Guild, db) -> str:
    """Render a human-readable snapshot of a guild's channels, roles, and the
    bot's current LFG configuration. Pure / no I/O — safe to call anywhere.
    """
    lines: list[str] = []
    lines.append(f"# Guild scan: {guild.name} (id={guild.id})")
    lines.append(f"# Generated: {datetime.datetime.now(datetime.timezone.utc).isoformat()}")
    lines.append("")

    lines.append("## Channels")
    by_cat: dict[str, list[discord.abc.GuildChannel]] = {}
    for ch in guild.channels:
        cat_name = ch.category.name if getattr(ch, "category", None) else "(no category)"
        by_cat.setdefault(cat_name, []).append(ch)
    for cat_name in sorted(by_cat):
        lines.append(f"\n### {cat_name}")
        for ch in sorted(by_cat[cat_name], key=lambda c: c.position):
            kind = type(ch).__name__.replace("Channel", "")
            lines.append(f"- [{kind}] #{ch.name}  (id={ch.id})")
            # Per-channel permission overwrites. We render allow/deny lists
            # with friendly flag names so it's easy to spot, e.g., "this
            # role has send_messages denied here". Skip channels with no
            # overwrites to keep the file compact.
            overwrites = getattr(ch, "overwrites", {}) or {}
            for target, ow in overwrites.items():
                allow_perms, deny_perms = ow.pair()
                allow_names = [n for n, v in allow_perms if v]
                deny_names = [n for n, v in deny_perms if v]
                if not allow_names and not deny_names:
                    continue
                kind_tag = "role" if isinstance(target, discord.Role) else "member"
                tname = getattr(target, "name", str(target.id))
                parts = []
                if allow_names:
                    parts.append(f"allow: {', '.join(allow_names)}")
                if deny_names:
                    parts.append(f"deny: {', '.join(deny_names)}")
                lines.append(
                    f"    overwrite ({kind_tag}) {tname}: " + " | ".join(parts)
                )

    lines.append("\n## Roles (top → bottom)")
    for role in sorted(guild.roles, key=lambda r: r.position, reverse=True):
        tags = []
        if role.is_default():
            tags.append("@everyone")
        if role.managed:
            tags.append("managed")
        if role.hoist:
            tags.append("hoisted")
        if role.mentionable:
            tags.append("mentionable")
        tag_s = f"  [{', '.join(tags)}]" if tags else ""
        lines.append(f"- {role.name}  (id={role.id}){tag_s}  members={len(role.members)}")
        # Permission summary. We show admin/elevated perms first so the file
        # is scannable for "who has the keys" — full bitmask is also dumped
        # in case more granular analysis is needed later.
        perms = role.permissions
        notable = _notable_permissions(perms)
        if perms.administrator:
            lines.append(f"    perms: ADMINISTRATOR (bypasses all checks) [bits={perms.value}]")
        elif notable:
            lines.append(f"    perms: {', '.join(notable)} [bits={perms.value}]")
        # else: no notable perms — skip the line to keep output compact.

    # ── Role memberships ─────────────────────────────────────────────────
    # Show, for every non-managed non-@everyone role that has at least one
    # member, who's in it. Useful for spotting "who's actually a Shotcaller?"
    # questions and lets the assistant cross-reference identities by role.
    lines.append("\n## Role memberships")
    for role in sorted(guild.roles, key=lambda r: r.position, reverse=True):
        if role.is_default() or role.managed:
            continue
        if not role.members:
            continue
        names = ", ".join(
            sorted((m.display_name for m in role.members), key=str.lower)
        )
        lines.append(f"- **{role.name}** ({len(role.members)}): {names}")

    lines.append("\n## Current LFG config")
    for label, key in (("Board channel", CFG_BOARD_CHANNEL), ("Default post channel", CFG_LFG_CHANNEL)):
        lines.append(f"- {label}: {db.get_config(key) or '(unset)'}")
    for t in EVENT_TYPES:
        lines.append(
            f"- {t.label}: role={db.get_config(CFG_ROLE_PREFIX + t.key) or '(unset)'} "
            f"channel={db.get_config(CFG_CHAN_PREFIX + t.key) or '(unset)'}"
        )

    # ── Diagnostics sections ────────────────────────────────────────────
    # Each block is wrapped in try/except so one bad lookup can't kill the
    # whole scan — diagnostics are best-effort.

    # ## Bot runtime — quick triage info (versions, db size, latency).
    try:
        import os as _os
        import sqlite3 as _sqlite3
        lines.append("\n## Bot runtime")
        try:
            import discord as _d
            lines.append(f"- discord.py: {_d.__version__}")
        except Exception:  # noqa: BLE001
            pass
        lines.append(f"- sqlite3: {_sqlite3.sqlite_version}")
        try:
            db_path = getattr(db, "db_path", None) or "data/database.db"
            if _os.path.exists(db_path):
                size_mb = _os.path.getsize(db_path) / (1024 * 1024)
                lines.append(f"- DB file: {db_path} ({size_mb:.2f} MB)")
        except Exception:  # noqa: BLE001
            pass
        bot_member = guild.me
        if bot_member is not None:
            lines.append(f"- Bot user: {bot_member} (id={bot_member.id})")
            top_role = bot_member.top_role.name if bot_member.top_role else "—"
            lines.append(f"- Bot top role: {top_role}")
    except Exception as _exc:  # noqa: BLE001
        lines.append(f"\n## Bot runtime\n- (failed: {_exc!r})")

    # ## Configured channels — resolve every *_channel_id in guild_config
    # against the live guild and verify the bot can send/embed there.
    try:
        rows = db.cursor.execute(
            "SELECT key, value FROM guild_config "
            "WHERE key LIKE '%channel_id' ORDER BY key"
        ).fetchall()
        lines.append("\n## Configured channels")
        if not rows:
            lines.append("- (no *_channel_id keys set)")
        bot_member = guild.me
        for r in rows:
            key = r["key"]
            val = r["value"]
            if not val:
                lines.append(f"- {key}: (unset)")
                continue
            try:
                ch = guild.get_channel(int(val))
            except (TypeError, ValueError):
                ch = None
            if ch is None:
                lines.append(f"- {key}={val}: ❌ NOT FOUND (deleted or wrong guild)")
                continue
            perms = ch.permissions_for(bot_member) if bot_member else None
            flags: list[str] = []
            if perms is not None:
                if not perms.view_channel:
                    flags.append("can't view")
                if isinstance(ch, discord.TextChannel):
                    if not perms.send_messages:
                        flags.append("can't send")
                    if not perms.embed_links:
                        flags.append("no embeds")
                if isinstance(ch, discord.VoiceChannel):
                    if not perms.connect:
                        flags.append("can't connect")
            tag = f" ⚠️ {', '.join(flags)}" if flags else " ✅"
            lines.append(f"- {key}: #{ch.name} (id={ch.id}){tag}")
    except Exception as _exc:  # noqa: BLE001
        lines.append(f"\n## Configured channels\n- (failed: {_exc!r})")

    # ## Lifecycle distribution — counts from user_profiles.
    try:
        rows = db.cursor.execute(
            "SELECT lifecycle_role, COUNT(*) AS n FROM user_profiles "
            "GROUP BY lifecycle_role ORDER BY n DESC"
        ).fetchall()
        total = db.cursor.execute(
            "SELECT COUNT(*) AS n FROM user_profiles"
        ).fetchone()
        registered = db.cursor.execute(
            "SELECT COUNT(*) AS n FROM user_profiles WHERE albion_player_id IS NOT NULL AND albion_player_id != ''"
        ).fetchone()
        in_home = db.cursor.execute(
            "SELECT COUNT(*) AS n FROM user_profiles WHERE was_in_home_guild = 1"
        ).fetchone()
        lines.append("\n## Lifecycle distribution (user_profiles)")
        lines.append(f"- Total profiles: {total['n'] if total else 0}")
        lines.append(f"- Registered (Albion linked): {registered['n'] if registered else 0}")
        lines.append(f"- Currently flagged in home guild: {in_home['n'] if in_home else 0}")
        for r in rows:
            role = r["lifecycle_role"] or "(none)"
            lines.append(f"- {role}: {r['n']}")
    except Exception as _exc:  # noqa: BLE001
        lines.append(f"\n## Lifecycle distribution\n- (failed: {_exc!r})")

    # ## Tracked-data stats — row counts for the most active tables.
    try:
        lines.append("\n## Tracked-data stats")
        # Generic row counts.
        for table in (
            "guilds", "guild_stats_history", "player_stats_history",
            "lfg_events", "lfg_signups",
            "bounties", "regear_requests", "staff_applications",
            "guild_applications", "policy_snapshots",
            "event_voice_snapshots", "event_voice_reconciled",
            "silver_ledger", "guild_treasury_history",
        ):
            try:
                row = db.cursor.execute(
                    f"SELECT COUNT(*) AS n FROM {table}"
                ).fetchone()
                if row:
                    lines.append(f"- {table}: {row['n']}")
            except Exception:  # noqa: BLE001
                continue
        # Status breakdowns where applicable.
        for table in ("bounties", "regear_requests", "lfg_events", "guild_applications", "staff_applications"):
            try:
                rs = db.cursor.execute(
                    f"SELECT status, COUNT(*) AS n FROM {table} GROUP BY status ORDER BY n DESC"
                ).fetchall()
                if rs:
                    parts = ", ".join(f"{r['status'] or '(none)'}={r['n']}" for r in rs)
                    lines.append(f"  · {table} by status: {parts}")
            except Exception:  # noqa: BLE001
                continue
        # Silver-ledger debt summary.
        try:
            owe_to_member = db.cursor.execute(
                "SELECT COALESCE(SUM(silver_balance), 0) AS s FROM user_profiles WHERE silver_balance > 0"
            ).fetchone()
            owe_to_guild = db.cursor.execute(
                "SELECT COALESCE(SUM(silver_balance), 0) AS s FROM user_profiles WHERE silver_balance < 0"
            ).fetchone()
            non_zero = db.cursor.execute(
                "SELECT COUNT(*) AS n FROM user_profiles WHERE silver_balance != 0"
            ).fetchone()
            lines.append(
                f"  · silver: guild owes members {owe_to_member['s']:,} · "
                f"members owe guild {abs(owe_to_guild['s']):,} · "
                f"{non_zero['n']} non-zero balances"
            )
        except Exception:  # noqa: BLE001
            pass
        # Latest treasury snapshot.
        try:
            row = db.cursor.execute(
                "SELECT date, balance FROM guild_treasury_history ORDER BY date DESC LIMIT 1"
            ).fetchone()
            if row:
                lines.append(f"  · latest treasury: {row['balance']:,} silver on {row['date']}")
        except Exception:  # noqa: BLE001
            pass
    except Exception as _exc:  # noqa: BLE001
        lines.append(f"\n## Tracked-data stats\n- (failed: {_exc!r})")

    # ## Loop / task heartbeats — anything stamped into guild_config that
    # tracks "last_*_at" timestamps. This is how we tell at a glance if a
    # background task has stalled.
    try:
        rows = db.cursor.execute(
            "SELECT key, value FROM guild_config "
            "WHERE key LIKE 'last_%' ORDER BY key"
        ).fetchall()
        lines.append("\n## Task heartbeats (last_* timestamps)")
        if not rows:
            lines.append("- (none recorded yet)")
        for r in rows:
            lines.append(f"- {r['key']}: {r['value']}")
    except Exception as _exc:  # noqa: BLE001
        lines.append(f"\n## Task heartbeats\n- (failed: {_exc!r})")

    # ## Full guild_config dump — every key/value, sorted. Most important
    # diagnostic block: tells us at a glance which knobs are set and which
    # are unset.
    try:
        rows = db.cursor.execute(
            "SELECT key, value FROM guild_config ORDER BY key"
        ).fetchall()
        lines.append(f"\n## guild_config (full dump · {len(rows)} keys)")
        # Lightly mask anything that looks like a token/secret. We don't
        # currently store tokens here, but be defensive in case a future
        # feature does.
        SENSITIVE = ("token", "secret", "password", "api_key")
        for r in rows:
            k, v = r["key"], r["value"]
            if any(s in k.lower() for s in SENSITIVE) and v:
                v = f"<redacted len={len(str(v))}>"
            lines.append(f"- {k} = {v if v not in (None, '') else '(empty)'}")
    except Exception as _exc:  # noqa: BLE001
        lines.append(f"\n## guild_config (full dump)\n- (failed: {_exc!r})")

    # Final timestamp footer so it's easy to tell how stale the file is.
    lines.append(
        f"\n# End of scan · written at "
        f"{datetime.datetime.now(datetime.timezone.utc).isoformat()}"
    )

    return "\n".join(lines)


def write_guild_scan_file(guild: discord.Guild, db, directory: str = "data") -> str:
    """Write the guild scan to ``data/guild-scan-<guild_id>.txt`` (gitignored)
    so it can be inspected from disk between bot restarts. Returns the path.

    Best-effort: failures are logged but never raised — this runs on the
    hourly sync tick and must not break that loop.
    """
    import os
    try:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"guild-scan-{guild.id}.txt")
        text = build_guild_scan_text(guild, db)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path
    except Exception as exc:  # noqa: BLE001
        error_log(f"write_guild_scan_file failed for {guild.name}: {exc!r}")
        return ""

