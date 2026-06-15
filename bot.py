"""UnionBot — main entry point.

Responsibilities:
    - Load environment + config
    - Configure intents and the Bot instance
    - Auto-discover and load cogs from the ``cogs/`` folder
    - Sync slash commands (guild-scoped during dev, global in prod)
    - Run the bot

Add new features as cogs in ``cogs/``; keep this file thin.
"""

import os
import asyncio
import signal
import time
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands
from dotenv import load_dotenv

from debug import (
    error_log, info_log, warning_log, critical_log, connection_log,
)
from config import guild_discord_id
from sql_database import Database

# ---------------------------------------------------------------------------
# Environment / configuration
# ---------------------------------------------------------------------------
load_dotenv()

TOKEN: str = os.getenv("DISCORD_TOKEN") or os.getenv("token") or ""
if not TOKEN:
    critical_log("No Discord token found. Set DISCORD_TOKEN in your .env file.")
    raise SystemExit(1)

# Guild ID — when set, slash commands sync instantly to that guild (dev mode).
# Leave unset / None to do a global sync (can take up to ~1 hour).
GUILD_DISCORD_ID: Optional[int] = None
_raw_guild = os.getenv("GUILD_DISCORD_ID") or guild_discord_id
if _raw_guild:
    try:
        GUILD_DISCORD_ID = int(_raw_guild)
        info_log(f"Dev guild ID: {GUILD_DISCORD_ID}")
    except (TypeError, ValueError):
        error_log(f"Invalid GUILD_DISCORD_ID: {_raw_guild!r}; falling back to global sync.")

COGS_DIR = Path(__file__).parent / "cogs"


# ---------------------------------------------------------------------------
# Bot subclass
# ---------------------------------------------------------------------------
class UnionBot(commands.Bot):
    """Custom Bot that auto-loads cogs and syncs in ``setup_hook``."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.guilds = True
        intents.reactions = True

        super().__init__(command_prefix="/", intents=intents)
        self.dev_guild = (
            discord.Object(id=GUILD_DISCORD_ID) if GUILD_DISCORD_ID else None
        )
        self.db = Database("data/database.db")
        self.db.initialize_all_tables()
        # Track gateway outages so we can log how long we were disconnected.
        self._disconnected_at: Optional[float] = None
        self._first_connect_logged = False
        # Wire the slash-command error handler onto the tree (Bot doesn't auto-dispatch this).
        self.tree.on_error = self.on_app_command_error

    async def setup_hook(self) -> None:
        """Runs once before the gateway connects. Load cogs + sync commands."""
        await self._load_cogs()

        if self.dev_guild is not None:
            try:
                # Clear stale guild commands, then re-register current ones.
                self.tree.clear_commands(guild=self.dev_guild)
                self.tree.copy_global_to(guild=self.dev_guild)
                synced = await self.tree.sync(guild=self.dev_guild)
                info_log(f"Synced {len(synced)} command(s) to guild {GUILD_DISCORD_ID}")
                return
            except discord.Forbidden:
                warning_log(
                    f"Bot has no access to dev guild {GUILD_DISCORD_ID}. "
                    "Re-invite with applications.commands scope, or unset GUILD_DISCORD_ID. "
                    "Falling back to global sync."
                )
            except discord.HTTPException as exc:
                warning_log(f"Guild sync failed ({exc}); falling back to global sync.")

        synced = await self.tree.sync()
        info_log(f"Synced {len(synced)} command(s) globally (may take up to 1h)")

    # Cogs whose absence makes the bot unsafe to run. A failure here aborts
    # startup so we don't ship a half-broken instance to production.
    _REQUIRED_COGS: tuple[str, ...] = (
        "cogs.admin",
        "cogs.applications",
        "cogs.events",
        "cogs.sysadmin",
        "cogs.users_profile",
    )

    async def _load_cogs(self) -> None:
        """Auto-discover and load every ``cogs/*.py`` extension."""
        if not COGS_DIR.is_dir():
            warning_log(f"No cogs directory at {COGS_DIR}")
            return

        failed: list[tuple[str, str]] = []
        for path in sorted(COGS_DIR.glob("*.py")):
            if path.name.startswith("_"):
                continue
            ext = f"cogs.{path.stem}"
            try:
                await self.load_extension(ext)
                info_log(f"Loaded cog: {ext}")
            except Exception as exc:  # noqa: BLE001
                failed.append((ext, repr(exc)))
                error_log(f"Failed to load {ext}: {exc}")

        if failed:
            failed_names = {name for name, _ in failed}
            required_missing = sorted(failed_names & set(self._REQUIRED_COGS))
            if required_missing:
                critical_log(
                    "Aborting startup — required cog(s) failed to load: "
                    + ", ".join(required_missing)
                )
                raise SystemExit(1)
            warning_log(
                "Some optional cogs failed to load: "
                + ", ".join(sorted(failed_names))
            )

    async def on_ready(self) -> None:
        user_id = self.user.id if self.user else "?"
        info_log(f"Logged in as {self.user} (ID: {user_id})")
        connection_log("READY", f"logged in as {self.user} ({user_id})")
        try:
            self._validate_config_on_startup()
        except Exception as exc:  # noqa: BLE001
            error_log(f"Config validation crashed: {exc!r}")

    def _validate_config_on_startup(self) -> None:
        """Warn (don't fail) when expected guild_config keys are missing.

        Helps catch silent feature-disabling on fresh deployments — the bot
        boots fine, but features that depend on unset channel/role IDs were
        previously dead-quiet. Each missing key prints a one-liner WARNING.
        """
        # (config key, human label, severity)
        expected: list[tuple[str, str, str]] = [
            ("home_guild_name",                       "Home Albion guild name",                       "warn"),
            ("unverified_role_id",                    "Unverified role",                              "warn"),
            ("verified_role_id",                      "Verified role",                                "warn"),
            ("automation_officer_channel_id",         "Officer-tasks channel",                        "warn"),
            ("automation_announcements_channel_id",   "Announcements channel (anniversaries)",        "info"),
            ("automation_hall_of_fame_channel_id",    "Hall-of-fame channel (fame milestones)",       "info"),
            ("automation_topic_channel_id",           "Topic channel (vital signs)",                  "info"),
            ("welcome_channel_id",                    "Welcome channel",                              "info"),
            ("points_channel_id",                     "Points/activity feed channel",                 "info"),
            ("officer_review_channel_id",             "Registration officer-review channel",          "info"),
        ]
        missing_warn: list[str] = []
        missing_info: list[str] = []
        for key, label, sev in expected:
            val = self.db.get_config(key)
            if val:
                continue
            (missing_warn if sev == "warn" else missing_info).append(f"{label} (`{key}`)")
        if missing_warn:
            warning_log(
                "Config: missing critical keys → "
                + "; ".join(missing_warn)
                + ". Some features will be disabled until these are set."
            )
        if missing_info:
            info_log(
                "Config: optional keys not set → "
                + "; ".join(missing_info)
            )
        if not missing_warn and not missing_info:
            info_log("Config validation: all expected keys present. ✅")

    async def on_connect(self) -> None:
        if self._first_connect_logged:
            connection_log("CONNECT", "gateway socket connected")
        else:
            connection_log("CONNECT", "initial gateway connection")
            self._first_connect_logged = True

    async def on_disconnect(self) -> None:
        # Fires on every gateway disconnect (network blip, Discord-side reset).
        # discord.py auto-reconnects because bot.run/start uses reconnect=True.
        self._disconnected_at = time.monotonic()
        connection_log("DISCONNECT", "gateway disconnected; awaiting auto-reconnect")
        warning_log("Gateway disconnected; awaiting auto-reconnect...")

    async def on_resumed(self) -> None:
        if self._disconnected_at is not None:
            outage_s = time.monotonic() - self._disconnected_at
            self._disconnected_at = None
            connection_log("RESUMED", f"session resumed after {outage_s:.1f}s outage")
            info_log(f"Gateway session resumed after {outage_s:.1f}s outage.")
        else:
            connection_log("RESUMED", "session resumed")
            info_log("Gateway session resumed.")

    async def close(self) -> None:
        # Make sure we close the SQLite connection cleanly on shutdown so the
        # WAL is flushed and we don't leave a stale lockfile behind.
        connection_log("SHUTDOWN", "close() called; flushing DB and disconnecting gateway")
        try:
            self.db.close()
            info_log("Database connection closed.")
        except Exception as exc:  # noqa: BLE001
            error_log(f"Error closing database on shutdown: {exc}")
        await super().close()

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: discord.app_commands.AppCommandError
    ) -> None:
        """Friendly fallback for slash-command errors. Logs everything; surfaces a
        short, ephemeral message to the user instead of a silent failure."""
        from discord import app_commands as _ac

        # Unwrap to the original exception when wrapped by CommandInvokeError
        original = getattr(error, "original", error)

        if isinstance(error, _ac.CheckFailure):
            msg = str(error) or "You don't have permission to use this command."
            payload = {"content": f"❌ {msg}", "ephemeral": True}
        elif isinstance(error, _ac.CommandOnCooldown):
            payload = {"content": f"⏳ Try again in {error.retry_after:.1f}s.", "ephemeral": True}
        else:
            error_log(f"Unhandled app-command error in {interaction.command}: {original!r}")
            payload = {
                "content": "❌ Something went wrong running that command. The error has been logged.",
                "ephemeral": True,
            }

        try:
            if interaction.response.is_done():
                await interaction.followup.send(**payload)
            else:
                await interaction.response.send_message(**payload)
        except discord.HTTPException:
            pass

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """Verbose log of every component (button/select/modal) interaction so we
        can trace 'This interaction failed' messages back to a specific custom_id."""
        try:
            itype = interaction.type
            # Slash commands have their own logging path; only trace components/modals here.
            if itype in (discord.InteractionType.component, discord.InteractionType.modal_submit):
                data = interaction.data or {}
                cid = data.get("custom_id", "?")
                user = f"{interaction.user} ({interaction.user.id})" if interaction.user else "?"
                info_log(f"Interaction recv: type={itype.name} custom_id={cid!r} user={user}")
        except Exception:  # noqa: BLE001
            pass

    async def on_error(self, event_method: str, *args, **kwargs) -> None:
        """Catch-all for unhandled exceptions in any event listener (including
        component callbacks). Logs full traceback so we can pinpoint failures."""
        import traceback
        tb = traceback.format_exc()
        error_log(f"Unhandled error in event {event_method}:\n{tb}")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
async def _run() -> None:
    """Async entrypoint: starts the bot and wires SIGTERM/SIGINT for clean shutdown."""
    bot = UnionBot()
    loop = asyncio.get_running_loop()

    def _request_shutdown(signame: str) -> None:
        info_log(f"Received {signame}; shutting down gracefully.")
        # Schedule bot.close() on the loop; cannot be awaited directly from a signal handler.
        loop.create_task(bot.close())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_shutdown, sig.name)
        except NotImplementedError:
            # add_signal_handler isn't supported on Windows; bot.run handles Ctrl+C there.
            pass

    # bot.start() returns when bot.close() is called or the gateway dies fatally.
    # reconnect=True (default) means transient network drops are auto-recovered.
    async with bot:
        await bot.start(TOKEN, reconnect=True)


def main() -> None:
    """Top-level retry loop.

    discord.py already auto-reconnects on transient gateway disconnects. This
    outer loop is a safety net for *fatal* errors that bubble out of
    ``bot.start`` (e.g. DNS resolution failed at boot before the network is
    up). On a Pi that boots while Wi-Fi/Ethernet is still negotiating, this
    keeps retrying with backoff until the network is alive.

    For long-term resilience (process death, host reboot) run this script
    under systemd; see ``barbatosbot.service`` in the repo.
    """
    # Conservative defaults: a tight retry loop is the fastest way to get
    # Discord to slap us with a session-start rate limit (HTTP 429 / code 40062),
    # which then *extends* every time we retry. So:
    #   - start with a 30s floor (well above discord.py's internal reconnect)
    #   - exponential backoff up to 15 minutes
    #   - any 429 forces at least a 10-minute cooldown
    #   - after MAX_CONSECUTIVE_FAILURES, exit and let systemd take over
    backoff = 30
    consecutive_failures = 0
    MAX_BACKOFF = 900            # 15 min ceiling
    RATE_LIMIT_FLOOR = 600       # 10 min minimum after 429
    MAX_CONSECUTIVE_FAILURES = 5 # then give up and exit (systemd will restart)
    while True:
        try:
            connection_log("START", "process starting bot.run loop")
            asyncio.run(_run())
            info_log("Bot exited cleanly.")
            connection_log("EXIT", "bot exited cleanly")
            return
        except KeyboardInterrupt:
            info_log("Shutdown requested by user.")
            connection_log("EXIT", "KeyboardInterrupt")
            return
        except (discord.LoginFailure, SystemExit):
            critical_log("Fatal: invalid Discord token. Not retrying.")
            connection_log("FATAL", "LoginFailure / SystemExit; not retrying")
            raise
        except Exception as exc:  # noqa: BLE001
            consecutive_failures += 1

            # Special-case Discord's session-start rate limit. Retrying inside
            # the cooldown window only makes Discord push the window further
            # out, so we apply a hard floor regardless of our own backoff.
            wait = backoff
            is_rate_limit = (
                isinstance(exc, discord.HTTPException) and getattr(exc, "status", None) == 429
            ) or "40062" in repr(exc) or "rate limited" in repr(exc).lower()
            if is_rate_limit:
                wait = max(wait, RATE_LIMIT_FLOOR)
                warning_log(
                    f"Discord rate-limited the gateway login (40062). "
                    f"Sleeping {wait}s before next attempt."
                )

            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                critical_log(
                    f"Bot failed {consecutive_failures} times in a row "
                    f"(last: {exc!r}). Exiting; systemd should restart with RestartSec backoff."
                )
                connection_log("FATAL", f"{consecutive_failures} consecutive failures; exiting")
                raise SystemExit(1)

            error_log(f"Bot crashed at top level: {exc!r}. Restarting in {wait}s.")
            connection_log("CRASH", f"{exc!r}; retrying in {wait}s")
            try:
                import time
                time.sleep(wait)
            except KeyboardInterrupt:
                return
            backoff = min(backoff * 2, MAX_BACKOFF)


if __name__ == "__main__":
    main()

