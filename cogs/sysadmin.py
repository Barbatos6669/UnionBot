"""System-administration helpers: config audit, backups, telemetry, error hook.

This cog has no domain logic — it's the bot's own self-maintenance layer.

* ``/admin audit-config`` — One-shot validator for every config key the
  bot expects. Reports which ones are missing or unresolvable, plus the
  fix command for each.
* Nightly DB backup task — copies ``data/database.db`` to
  ``data/backups/database-YYYY-MM-DD.db`` and prunes anything older than
  30 days. WAL is great until you ``rm`` the wrong thing.
* Command telemetry — every slash invocation is logged to a
  ``command_log`` table (cog, command, user, success flag, latency_ms).
  Lets you see what's actually used.
* Error-to-officer-channel hook — installs a wrapper on
  ``bot.tree.on_error`` that, on top of the existing log, posts a
  redacted embed to the officer channel so officers see failures in
  real time instead of digging through journalctl.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
import traceback
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs._typing import Bot
from debug import error_log, info_log, warning_log
from utils import error_embed, info_embed, is_officer, success_embed
from time_utils import utc_now_naive


# ── config-audit catalog ─────────────────────────────────────────────────────
# Tier governs how loudly we complain.
#   "critical" — feature is broken without it
#   "important" — feature is degraded but partially works
#   "optional"  — nice-to-have, silent if missing

_AUDIT_CATALOG: tuple[tuple[str, str, str, str], ...] = (
    # (key, label, fix_hint, tier)
    ("home_guild_name", "Home Albion guild", "/applications set-home-guild", "critical"),
    ("application_review_channel_id", "Application review channel",
     "/apply set-review-channel", "critical"),
    ("application_channel_id", "Application Apply-button channel",
     "/apply setup (in target channel)", "important"),
    ("officer_channel_id", "Officer-only channel",
     "/admin set-channels", "important"),
    ("regear_review_channel_id", "Regear review channel",
     "/regear set-review-channel", "important"),
    ("regear_board_channel_id", "Regear board channel",
     "/regear set-board-channel", "important"),
    ("chest_alert_channel_id", "Chest low-stock alert channel",
     "/chest set-alert-channel (or falls back to officer_channel_id)", "optional"),
    ("chest_low_stock_threshold", "Chest low-stock threshold (default 3)",
     "DB only — set via config table", "optional"),
    ("lfg_post_channel_id", "LFG post channel",
     "/lfg setup-channel", "important"),
    ("lfg_board_channel_id", "LFG control board channel",
     "/lfg setup-board (in target channel)", "important"),
    ("automation_officer_channel_id", "Automation officer-tasks channel",
     "/automation set-officer-channel", "important"),
    ("automation_announcements_channel_id",
     "Automation announcements channel", "/automation set-announcements-channel", "optional"),
    ("welcome_channel_id", "Welcome channel",
     "/admin set-channel purpose:welcome", "optional"),
    ("goodbye_channel_id", "Goodbye channel",
     "/admin set-channel purpose:goodbye", "optional"),
    ("points_announce_channel_id", "Points announcement channel",
     "/points config set-announce-channel", "optional"),
    ("help_channel_id", "Help-tickets entry channel",
     "/help-ticket config set-channel", "optional"),
    ("help_review_channel_id", "Help-tickets review channel",
     "/help-ticket config set-review-channel", "optional"),
    ("probationary_days", "Probationary tenure threshold",
     "DB only — set via config table", "important"),
    ("member_days", "Member tenure threshold",
     "DB only — set via config table", "important"),
    ("error_alert_channel_id", "Error-alert channel (errors get posted here)",
     "DB only — set via config table", "optional"),
)


# ── permission helper ────────────────────────────────────────────────────────


def _is_admin(user) -> bool:
    """True for guild owners or members with Administrator permission.

    Stricter than ``is_officer`` because hot-reload and reload-all execute
    arbitrary Python in the bot process — only trust real admins.
    """
    if not isinstance(user, discord.Member):
        return False
    if user.guild and user.guild.owner_id == user.id:
        return True
    perms = getattr(user, "guild_permissions", None)
    return bool(perms and perms.administrator)


# ── backup & telemetry table setup ───────────────────────────────────────────


def _ensure_command_log_table(db) -> None:
    try:
        if not db.connection:
            db.connect()
        db.execute(
            "CREATE TABLE IF NOT EXISTS command_log ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  cog TEXT,"
            "  command TEXT NOT NULL,"
            "  user_id TEXT NOT NULL,"
            "  username TEXT,"
            "  guild_id TEXT,"
            "  success INTEGER NOT NULL DEFAULT 1,"
            "  error TEXT,"
            "  latency_ms INTEGER,"
            "  created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)"
            ")"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS ix_command_log_created "
            "ON command_log(created_at DESC)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS ix_command_log_command "
            "ON command_log(command, created_at DESC)"
        )
    except sqlite3.Error as exc:
        error_log(f"_ensure_command_log_table failed: {exc!r}")


def _log_command(
    db, *, cog: str | None, command: str, user_id: str, username: str | None,
    guild_id: str | None, success: bool, error: str | None,
    latency_ms: int | None,
) -> None:
    try:
        if not db.connection:
            db.connect()
        db.cursor.execute(
            "INSERT INTO command_log "
            "(cog, command, user_id, username, guild_id, success, error, latency_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (cog, command, str(user_id), username, guild_id,
             1 if success else 0, error, latency_ms),
        )
        db.connection.commit()
    except sqlite3.Error as exc:
        # Don't spam logs on a write failure; telemetry is best-effort.
        warning_log(f"_log_command failed: {exc!r}")


# ── cog ──────────────────────────────────────────────────────────────────────


class SysAdminCog(commands.Cog):
    """Self-maintenance: config audit, backups, telemetry, error hook."""

    BACKUP_DIR = Path("data/backups")
    BACKUP_RETENTION_DAYS = 30

    def __init__(self, bot: Bot) -> None:
        self.bot: Bot = bot
        _ensure_command_log_table(self.bot.db)
        # Track installed hooks so cog_unload can fully detach on reload —
        # otherwise we'd stack duplicate listeners / on_error wrappers.
        self._telemetry_listener = None  # type: ignore[var-annotated]
        self._prev_tree_on_error = None  # type: ignore[var-annotated]
        self._installed_tree_on_error = None  # type: ignore[var-annotated]
        self._install_error_hook()
        self._install_telemetry_hook()
        self.nightly_backup.start()

    def cog_unload(self) -> None:
        self.nightly_backup.cancel()
        # Detach telemetry listener so reloads don't stack duplicates.
        if self._telemetry_listener is not None:
            try:
                self.bot.remove_listener(
                    self._telemetry_listener, "on_app_command_completion"
                )
            except Exception:  # noqa: BLE001
                pass
            self._telemetry_listener = None
        # Restore the previous tree.on_error only if our wrapper is still the
        # active one (avoid stomping a newer reload's handler).
        try:
            if (
                self._installed_tree_on_error is not None
                and getattr(self.bot.tree, "on_error", None) is self._installed_tree_on_error
            ):
                self.bot.tree.on_error = self._prev_tree_on_error  # type: ignore[assignment]
        except Exception:  # noqa: BLE001
            pass
        self._installed_tree_on_error = None
        self._prev_tree_on_error = None

    # ── /admin audit-config ─────────────────────────────────────────────────

    admin_group = app_commands.Group(
        name="sysadmin",
        description="Bot self-maintenance commands.",
    )

    @admin_group.command(
        name="audit-config",
        description="Audit every config key the bot expects; report what's missing.",
    )
    async def audit_config(self, interaction: discord.Interaction) -> None:
        if not is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Officers only", "This audit is for officers."),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        db = self.bot.db

        ok: list[str] = []
        critical: list[tuple[str, str, str]] = []
        important: list[tuple[str, str, str]] = []
        optional: list[tuple[str, str, str]] = []
        unresolved: list[tuple[str, str, str]] = []

        for key, label, fix, tier in _AUDIT_CATALOG:
            val = db.get_config(key)
            if not val:
                bucket = {
                    "critical": critical,
                    "important": important,
                    "optional": optional,
                }[tier]
                bucket.append((key, label, fix))
                continue
            # If the key is a channel ID, verify it actually resolves.
            if key.endswith("_channel_id"):
                resolved = self.bot.get_channel(int(val)) if val.isdigit() else None
                if resolved is None:
                    try:
                        resolved = await self.bot.fetch_channel(int(val))
                    except (discord.NotFound, discord.Forbidden, ValueError):
                        resolved = None
                if resolved is None:
                    unresolved.append((key, label, fix))
                    continue
            ok.append(f"`{key}`")

        embed = discord.Embed(
            title="Config Audit",
            description=(
                f"**{len(ok)}** keys configured and resolvable.\n"
                f"**{len(critical)}** critical • **{len(important)}** important • "
                f"**{len(optional)}** optional missing • "
                f"**{len(unresolved)}** unresolvable."
            ),
            color=(
                discord.Color.red() if critical or unresolved
                else discord.Color.orange() if important
                else discord.Color.green()
            ),
            timestamp=utc_now_naive(),
        )

        def _fmt(rows: list[tuple[str, str, str]]) -> str:
            return "\n".join(f"• **{lbl}** — `{key}`\n   _fix:_ `{fix}`"
                             for key, lbl, fix in rows[:8])

        if critical:
            embed.add_field(
                name="🔴 Critical (features broken)",
                value=_fmt(critical),
                inline=False,
            )
        if unresolved:
            embed.add_field(
                name="⚠️ Set but channel/role doesn't exist anymore",
                value=_fmt(unresolved),
                inline=False,
            )
        if important:
            embed.add_field(
                name="🟠 Important (degraded)",
                value=_fmt(important),
                inline=False,
            )
        if optional:
            embed.add_field(
                name="⚪ Optional (silent miss)",
                value=_fmt(optional)[:1024],
                inline=False,
            )

        await interaction.followup.send(embed=embed, ephemeral=True)
        info_log(
            f"{interaction.user} ran /sysadmin audit-config: "
            f"{len(ok)} ok, {len(critical)} crit, {len(important)} imp, "
            f"{len(optional)} opt, {len(unresolved)} unresolved."
        )

    # ── /sysadmin backup-now ────────────────────────────────────────────────

    @admin_group.command(
        name="backup-now",
        description="Run an immediate DB backup (officers only).",
    )
    async def backup_now(self, interaction: discord.Interaction) -> None:
        if not is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Officers only", "This is for officers."),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        path, size = self._do_backup()
        if path is None:
            await interaction.followup.send(
                embed=error_embed("Backup failed", "Check logs for details."),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=success_embed(
                "Backup complete",
                f"Wrote `{path.name}` ({size // 1024} KiB) to `{self.BACKUP_DIR}/`.",
            ),
            ephemeral=True,
        )

    # ── /sysadmin telemetry ─────────────────────────────────────────────────

    @admin_group.command(
        name="telemetry",
        description="Show command usage telemetry for the last 7 days.",
    )
    @app_commands.describe(days="Look-back window in days (default 7, max 90).")
    async def telemetry(
        self, interaction: discord.Interaction, days: int = 7,
    ) -> None:
        if not is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Officers only", "This is for officers."),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        days = max(1, min(90, int(days)))
        db = self.bot.db
        try:
            if not db.connection:
                db.connect()
            db.cursor.execute(
                "SELECT command, COUNT(*) AS calls, "
                "SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS errors, "
                "AVG(latency_ms) AS avg_ms "
                "FROM command_log "
                "WHERE julianday('now') - julianday(created_at) <= ? "
                "GROUP BY command ORDER BY calls DESC LIMIT 25",
                (int(days),),
            )
            rows = [dict(r) for r in db.cursor.fetchall()]
        except sqlite3.Error as exc:
            await interaction.followup.send(
                embed=error_embed("Telemetry query failed", repr(exc)),
                ephemeral=True,
            )
            return
        if not rows:
            await interaction.followup.send(
                embed=info_embed("No data",
                                 f"No commands logged in the last {days} day(s)."),
                ephemeral=True,
            )
            return
        lines = []
        for r in rows:
            avg = int(r.get("avg_ms") or 0)
            errs = int(r.get("errors") or 0)
            badge = " ⚠️" if errs else ""
            lines.append(
                f"`{r['command']:<24}` **{int(r['calls']):>4}** call(s) "
                f"• {errs} err{badge} • {avg}ms avg"
            )
        embed = info_embed(
            f"Command telemetry — last {days} day(s)",
            "\n".join(lines)[:4000],
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /sysadmin reload ────────────────────────────────────────────────────

    async def _cog_autocomplete(
        self,
        interaction: discord.Interaction,  # noqa: ARG002
        current: str,
    ) -> list[app_commands.Choice[str]]:
        names = sorted(
            ext.removeprefix("cogs.")
            for ext in self.bot.extensions
            if ext.startswith("cogs.")
        )
        needle = (current or "").lower()
        picks = [n for n in names if needle in n.lower()][:25]
        return [app_commands.Choice(name=n, value=n) for n in picks]

    @admin_group.command(
        name="reload",
        description="Hot-reload a single cog (no service restart). Admins only.",
    )
    @app_commands.describe(cog="Cog to reload, e.g. 'lfg' or 'regear'.")
    @app_commands.autocomplete(cog=_cog_autocomplete)
    async def reload(
        self, interaction: discord.Interaction, cog: str,
    ) -> None:
        if not _is_admin(interaction.user):
            await interaction.response.send_message(
                embed=error_embed(
                    "Admins only",
                    "Hot-reloading code is restricted to server admins.",
                ),
                ephemeral=True,
            )
            return
        ext = cog if cog.startswith("cogs.") else f"cogs.{cog}"
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.bot.reload_extension(ext)
        except commands.ExtensionNotLoaded:
            try:
                await self.bot.load_extension(ext)
            except Exception as exc:  # noqa: BLE001
                await interaction.followup.send(
                    embed=error_embed("Load failed", f"`{ext}` — `{exc!r}`"),
                    ephemeral=True,
                )
                return
        except commands.ExtensionNotFound:
            await interaction.followup.send(
                embed=error_embed(
                    "Cog not found",
                    f"No extension named `{ext}`.",
                ),
                ephemeral=True,
            )
            return
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(
                embed=error_embed(
                    "Reload failed",
                    f"`{ext}` — `{exc!r}`\n"
                    "_The previous version is still loaded._",
                ),
                ephemeral=True,
            )
            error_log(f"reload {ext} failed: {exc!r}")
            return
        try:
            synced = await self.bot.tree.sync()
            sync_note = f" • {len(synced)} command(s) synced"
        except discord.HTTPException as exc:
            sync_note = f" • sync failed: {exc!r}"
        await interaction.followup.send(
            embed=success_embed(
                "Reloaded",
                f"`{ext}` reloaded successfully.{sync_note}",
            ),
            ephemeral=True,
        )
        info_log(f"{interaction.user} reloaded {ext}.{sync_note}")

    @admin_group.command(
        name="reload-all",
        description="Reload every loaded cog. Admins only.",
    )
    async def reload_all(self, interaction: discord.Interaction) -> None:
        if not _is_admin(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Admins only", "Restricted to server admins."),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok: list[str] = []
        failed: list[tuple[str, str]] = []
        # snapshot first — reloading mutates self.bot.extensions
        targets = sorted(e for e in self.bot.extensions if e.startswith("cogs."))
        for ext in targets:
            try:
                await self.bot.reload_extension(ext)
                ok.append(ext.removeprefix("cogs."))
            except Exception as exc:  # noqa: BLE001
                failed.append((ext.removeprefix("cogs."), repr(exc)))
                error_log(f"reload-all: {ext} failed: {exc!r}")
        try:
            synced = await self.bot.tree.sync()
            sync_note = f"{len(synced)} command(s) synced."
        except discord.HTTPException as exc:
            sync_note = f"sync failed: {exc!r}"
        body = (
            f"**Reloaded {len(ok)}/{len(targets)}** • {sync_note}\n"
        )
        if failed:
            body += "\n**Failed:**\n" + "\n".join(
                f"• `{name}` — `{err}`" for name, err in failed[:10]
            )
        await interaction.followup.send(
            embed=(
                info_embed("Reload complete", body)
                if not failed else
                error_embed("Reload partial", body)
            ),
            ephemeral=True,
        )
        info_log(f"{interaction.user} ran reload-all: {len(ok)} ok, {len(failed)} failed.")

    # ── nightly backup loop ─────────────────────────────────────────────────

    @tasks.loop(hours=24)
    async def nightly_backup(self) -> None:
        try:
            self._do_backup()
            self._prune_old_backups()
        except Exception as exc:  # noqa: BLE001
            error_log(f"nightly_backup crashed: {exc!r}")

    @nightly_backup.before_loop
    async def _before_backup(self) -> None:
        await self.bot.wait_until_ready()

    def _do_backup(self) -> tuple[Optional[Path], int]:
        """Copy the live SQLite DB to data/backups/. Uses sqlite3's online
        backup API so a snapshot is taken safely even while the bot writes."""
        try:
            self.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            stamp = utc_now_naive().strftime("%Y-%m-%d_%H%M%S")
            dest = self.BACKUP_DIR / f"database-{stamp}.db"
            src_path = Path("data/database.db")
            if not src_path.exists():
                error_log(f"backup: source not found at {src_path}")
                return None, 0
            # Use SQLite's online backup so WAL state is consistent.
            src = sqlite3.connect(str(src_path))
            try:
                dst = sqlite3.connect(str(dest))
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
            size = dest.stat().st_size
            info_log(f"DB backup OK: {dest.name} ({size // 1024} KiB)")
            return dest, size
        except Exception as exc:  # noqa: BLE001
            error_log(f"_do_backup failed: {exc!r}")
            return None, 0

    def _prune_old_backups(self) -> None:
        if not self.BACKUP_DIR.exists():
            return
        cutoff = utc_now_naive() - _dt.timedelta(days=self.BACKUP_RETENTION_DAYS)
        pruned = 0
        for f in self.BACKUP_DIR.glob("database-*.db"):
            try:
                mtime = _dt.datetime.fromtimestamp(
                    f.stat().st_mtime,
                    _dt.UTC,
                ).replace(tzinfo=None)
                if mtime < cutoff:
                    f.unlink()
                    pruned += 1
            except OSError as exc:
                warning_log(f"backup prune: couldn't remove {f.name}: {exc!r}")
        if pruned:
            info_log(f"backup prune: removed {pruned} old file(s).")

    # ── telemetry & error hooks ─────────────────────────────────────────────

    def _install_telemetry_hook(self) -> None:
        """Listen to ``on_app_command_completion`` for successful runs and
        wrap ``tree.on_error`` so failures are logged + posted. We don't
        replace the existing on_error — we chain through it."""
        bot = self.bot
        cog = self

        async def _on_completion(
            interaction: discord.Interaction,
            command: discord.app_commands.Command,
        ) -> None:
            try:
                _log_command(
                    bot.db,
                    cog=(
                        command.binding.__class__.__name__
                        if getattr(command, "binding", None) else None
                    ),
                    command=command.qualified_name,
                    user_id=str(interaction.user.id),
                    username=str(interaction.user),
                    guild_id=str(interaction.guild_id) if interaction.guild_id else None,
                    success=True,
                    error=None,
                    latency_ms=None,
                )
            except Exception as exc:  # noqa: BLE001
                warning_log(f"telemetry on_completion failed: {exc!r}")

        bot.add_listener(_on_completion, "on_app_command_completion")
        self._telemetry_listener = _on_completion

        # Wrap on_error for failure logging + officer-channel alert.
        prev_handler = bot.tree.on_error
        self._prev_tree_on_error = prev_handler

        async def _on_error(
            interaction: discord.Interaction,
            error: discord.app_commands.AppCommandError,
        ) -> None:
            try:
                command = interaction.command
                if command is not None:
                    _log_command(
                        bot.db,
                        cog=command.binding.__class__.__name__
                             if getattr(command, "binding", None) else None,
                        command=command.qualified_name,
                        user_id=str(interaction.user.id),
                        username=str(interaction.user),
                        guild_id=str(interaction.guild_id) if interaction.guild_id else None,
                        success=False,
                        error=repr(error)[:500],
                        latency_ms=None,
                    )
            except Exception as exc:  # noqa: BLE001
                warning_log(f"telemetry on_error failed: {exc!r}")
            # Post to officer channel
            try:
                await cog._post_error_alert(interaction, error)
            except Exception as exc:  # noqa: BLE001
                warning_log(f"error-alert post failed: {exc!r}")
            # Chain to whatever handler was there (bot.py's default reply).
            if prev_handler is not None:
                await prev_handler(interaction, error)

        bot.tree.on_error = _on_error  # type: ignore[assignment]
        self._installed_tree_on_error = _on_error

    def _install_error_hook(self) -> None:
        """No-op marker — the actual hook is installed alongside telemetry."""
        # Kept as a separate method so the docstring at top of file is honest.
        return

    async def _post_error_alert(
        self, interaction: discord.Interaction,
        error: discord.app_commands.AppCommandError,
    ) -> None:
        """Best-effort: post a redacted error embed to the officer channel.

        We deliberately skip CheckFailure / CommandOnCooldown — those are
        expected user-facing errors, not bugs.
        """
        from discord import app_commands as _ac
        if isinstance(error, (_ac.CheckFailure, _ac.CommandOnCooldown)):
            return
        db = self.bot.db
        chan_id = (
            db.get_config("error_alert_channel_id")
            or db.get_config("officer_channel_id")
        )
        if not chan_id:
            return
        try:
            chan = self.bot.get_channel(int(chan_id))
            if chan is None:
                chan = await self.bot.fetch_channel(int(chan_id))
        except (discord.NotFound, discord.Forbidden, ValueError):
            return
        if not isinstance(chan, discord.TextChannel):
            return
        original = getattr(error, "original", error)
        tb = "".join(
            traceback.format_exception(
                type(original), original, original.__traceback__,
            )
        )[-1500:]
        command_name = (
            interaction.command.qualified_name
            if interaction.command else "?"
        )
        embed = error_embed(
            f"Command error in `/{command_name}`",
            f"**User:** {interaction.user.mention}\n"
            f"**Error:** `{type(original).__name__}: {original}`\n"
            f"```py\n{tb}\n```",
        )
        try:
            await chan.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            pass


async def setup(bot: Bot) -> None:
    await bot.add_cog(SysAdminCog(bot))
    info_log("Initialized SysAdmin cog.")
