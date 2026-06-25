import sqlite3
import datetime as _datetime
import re
from collections import defaultdict
from pathlib import Path

from cogs._db_lfg import LfgDatabaseMixin
from cogs._db_loadout_chest import LoadoutChestDatabaseMixin

import debug


# ---------------------------------------------------------------------------
# Database — add tables and methods here as we build each feature
# ---------------------------------------------------------------------------
class Database(LfgDatabaseMixin, LoadoutChestDatabaseMixin):
    def fetch_pending_home_guild_grace(self) -> list[dict]:
        """Return all users currently in the 72h home guild grace period (pending_home_guild_until in the future)."""
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute('''
                SELECT discord_id, username, albion_name, guild_name, pending_home_guild_until
                FROM user_profiles
                WHERE pending_home_guild_until IS NOT NULL
                  AND pending_home_guild_until > CURRENT_TIMESTAMP
                  AND albion_player_id IS NOT NULL
            ''')
            return [dict(row) for row in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"Error fetching pending home guild grace users: {e}")
            return []
    def __init__(self, database_path):
        self.database_path = database_path
        self.connection = None
        self.cursor = None

    def connect(self):
        try:
            # Make sure the parent directory exists before SQLite tries to
            # create the file there. First-boot scenarios can otherwise fail
            # silently with "unable to open database file".
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
            # 30s timeout is the per-call wait for an open lock; busy_timeout
            # below adds a backoff inside the engine itself. WAL lets readers
            # and writers coexist without blocking each other, which matters
            # because we have a long-running async bot with many cogs hitting
            # the same connection.
            self.connection = sqlite3.connect(self.database_path, timeout=30)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA busy_timeout=5000")
            self.connection.execute("PRAGMA synchronous=NORMAL")
            self.cursor = self.connection.cursor()
            debug.info_log("Connected to the database.")
        except sqlite3.Error as e:
            debug.error_log(f"Error connecting to the database: {e}")

    def close(self):
        try:
            if self.connection:
                self.connection.close()
        except sqlite3.Error as e:
            debug.error_log(f"Error closing the database connection: {e}")
        finally:
            self.connection = None
            self.cursor = None

    def execute(self, query, params=None, quiet: bool = False):
        try:
            if not self.connection:
                self.connect()
            if params is not None:
                self.cursor.execute(query, params)
            else:
                self.cursor.execute(query)
            self.connection.commit()
        except sqlite3.Error as e:
            if not quiet:
                debug.error_log(f"Error executing query: {e}")

    def initialize_user_profiles_table(self):
        self.execute('''
            CREATE TABLE IF NOT EXISTS user_profiles (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,

                -- Discord
                discord_id  TEXT UNIQUE NOT NULL,
                username    TEXT NOT NULL,
                join_date   TEXT,

                -- Albion Identity
                albion_player_id  TEXT UNIQUE,
                albion_name       TEXT,
                guild_id          TEXT,
                guild_name        TEXT,
                alliance_id       TEXT,
                alliance_name     TEXT,
                alliance_tag      TEXT,

                -- Combat
                kill_fame         INTEGER DEFAULT 0,
                death_fame        INTEGER DEFAULT 0,
                fame_ratio        REAL DEFAULT 0.0,
                average_item_power REAL DEFAULT 0.0,

                -- PvE
                pve_total         INTEGER DEFAULT 0,
                pve_royal         INTEGER DEFAULT 0,
                pve_outlands      INTEGER DEFAULT 0,
                pve_avalon        INTEGER DEFAULT 0,
                pve_hellgate      INTEGER DEFAULT 0,
                pve_corrupted     INTEGER DEFAULT 0,
                pve_mists         INTEGER DEFAULT 0,

                -- Gathering
                gather_fiber      INTEGER DEFAULT 0,
                gather_hide       INTEGER DEFAULT 0,
                gather_ore        INTEGER DEFAULT 0,
                gather_rock       INTEGER DEFAULT 0,
                gather_wood       INTEGER DEFAULT 0,
                gather_all        INTEGER DEFAULT 0,

                -- Other
                crafting_fame     INTEGER DEFAULT 0,
                crystal_league    INTEGER DEFAULT 0,
                fishing_fame      INTEGER DEFAULT 0,
                farming_fame      INTEGER DEFAULT 0,

                -- Metadata
                last_updated      TEXT,
                screenshot_url    TEXT,

                -- Guild Management
                activity_points   INTEGER DEFAULT 0,
                strikes           INTEGER DEFAULT 0,
                notes             TEXT,

                -- Activity points (rolling windows; reset on schedule)
                points_weekly     INTEGER DEFAULT 0,
                points_monthly    INTEGER DEFAULT 0,
                points_season     INTEGER DEFAULT 0,

                -- Registration state
                pending_verification INTEGER DEFAULT 0,

                -- Lifecycle
                lifecycle_role    TEXT,
                verified_date     TEXT,
                last_activity_date TEXT,
                was_in_home_guild INTEGER DEFAULT 0,

                -- Silver ledger (positive = guild owes member; negative = member owes guild)
                silver_balance    INTEGER DEFAULT 0
            )
        ''')
        # Migration: add columns to existing databases that predate these fields
        for col, definition in [
            ("pending_verification", "INTEGER DEFAULT 0"),
            ("lifecycle_role",       "TEXT"),
            ("verified_date",        "TEXT"),
            ("last_activity_date",   "TEXT"),
            ("was_in_home_guild",    "INTEGER DEFAULT 0"),
            ("points_weekly",        "INTEGER DEFAULT 0"),
            ("points_monthly",       "INTEGER DEFAULT 0"),
            ("points_season",        "INTEGER DEFAULT 0"),
            ("silver_balance",       "INTEGER DEFAULT 0"),
            ("activity_streak_days",      "INTEGER DEFAULT 0"),
            ("activity_streak_last_date", "TEXT"),
            ("activity_streak_best",      "INTEGER DEFAULT 0"),
            ("activity_streak_freeze_used_date", "TEXT"),
            ("inactivity_nudge_sent_date", "TEXT"),
            ("unverified_nudge_sent_date", "TEXT"),
            ("unverified_nudge_count", "INTEGER DEFAULT 0"),
            ("loa_until",  "TEXT"),
            ("loa_reason", "TEXT"),
            ("timezone",   "TEXT"),
            ("pb_kill_delta",   "INTEGER DEFAULT 0"),
            ("pb_pve_delta",    "INTEGER DEFAULT 0"),
            ("pb_gather_delta", "INTEGER DEFAULT 0"),
            ("pb_craft_delta",  "INTEGER DEFAULT 0"),
            ("pb_fish_delta",   "INTEGER DEFAULT 0"),
        ]:
            try:
                if not self.connection:
                    self.connect()
                self.cursor.execute(f"ALTER TABLE user_profiles ADD COLUMN {col} {definition}")
                self.connection.commit()
            except sqlite3.OperationalError:
                pass  # Column already exists
        # Audit trail for every silver_balance change. Append-only; never updated.
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute('''
                CREATE TABLE IF NOT EXISTS silver_ledger (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    discord_id  TEXT NOT NULL,
                    delta       INTEGER NOT NULL,
                    reason      TEXT,
                    ref_type    TEXT,
                    ref_id      TEXT,
                    actor_id    TEXT,
                    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            self.cursor.execute(
                'CREATE INDEX IF NOT EXISTS idx_silver_ledger_user '
                'ON silver_ledger (discord_id, created_at DESC)'
            )
            self.connection.commit()
        except sqlite3.OperationalError as exc:
            debug.error_log(f"silver_ledger init failed: {exc!r}")
        debug.info_log("Initialized user_profiles table.")   

    def set_lifecycle_role(self, discord_id: str, role_name: str):
        self.execute(
            'UPDATE user_profiles SET lifecycle_role = ? WHERE discord_id = ?',
            (role_name, discord_id)
        )

    # Milestone day-counts that surface in the activity feed. Matches the
    # checkpoints we want public callouts for (3, 7, 14, 30, 60, 90, 180, 365).
    _STREAK_MILESTONES = (3, 7, 14, 30, 60, 90, 180, 365)
    # One free missed-day per 30-day rolling window. Burns silently if a
    # member skips exactly one day; resets the streak otherwise.
    _STREAK_FREEZE_WINDOW_DAYS = 30

    def update_activity_streak(self, discord_id: str, today_iso: str) -> dict:
        """Record fame activity for ``today_iso`` (YYYY-MM-DD) and return:

            {
                "streak":        int,     # current consecutive-day count
                "best":          int,     # all-time best for this profile
                "milestone":     int|None,  # set when ``streak`` hits a milestone
                "extended":      bool,    # True when streak grew this call
                "started":       bool,    # True when a new streak just started (was reset)
                "freeze_used":   bool,    # True when the grace day was burned this call
            }

        Same UTC day → no change. Yesterday → streak += 1. Two days gap and a
        freeze hasn't been used in the last 30 days → burn the freeze, treat
        as +1 (no break). Three+ day gap, or freeze unavailable, → reset to 1.
        Caller passes the date string explicitly so the timezone semantics are
        controlled by the caller (we always use UTC).
        """
        result = {
            "streak": 0, "best": 0, "milestone": None,
            "extended": False, "started": False, "freeze_used": False,
        }
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                'SELECT activity_streak_days, activity_streak_last_date, '
                'activity_streak_best, activity_streak_freeze_used_date '
                'FROM user_profiles WHERE discord_id = ?',
                (discord_id,),
            )
            row = self.cursor.fetchone()
            if row is None:
                return result
            cur = int(row["activity_streak_days"] or 0)
            last = row["activity_streak_last_date"] or ""
            best = int(row["activity_streak_best"] or 0)
            freeze_last = row["activity_streak_freeze_used_date"] or ""

            if last == today_iso:
                # Already counted today; no-op but report current state.
                result.update(streak=cur, best=best)
                return result

            import datetime as _dt
            try:
                today_dt = _dt.date.fromisoformat(today_iso)
            except ValueError:
                today_dt = None

            new_streak = cur
            burned_freeze = False
            if last and today_dt:
                try:
                    last_dt = _dt.date.fromisoformat(last)
                    delta_days = (today_dt - last_dt).days
                except ValueError:
                    delta_days = 0
                if delta_days == 1:
                    new_streak = cur + 1
                    result["extended"] = True
                elif delta_days == 2 and cur > 0:
                    # One-day gap: spend the freeze if available.
                    freeze_available = True
                    if freeze_last:
                        try:
                            freeze_last_dt = _dt.date.fromisoformat(freeze_last)
                            freeze_age = (today_dt - freeze_last_dt).days
                            if freeze_age < self._STREAK_FREEZE_WINDOW_DAYS:
                                freeze_available = False
                        except ValueError:
                            pass
                    if freeze_available:
                        new_streak = cur + 1
                        result["extended"] = True
                        result["freeze_used"] = True
                        burned_freeze = True
                    else:
                        new_streak = 1
                        result["started"] = True
                else:
                    new_streak = 1
                    result["started"] = (cur > 0)
            else:
                new_streak = 1
                result["started"] = False  # never had a streak — not a "reset"

            new_best = max(best, new_streak)
            milestone = new_streak if new_streak in self._STREAK_MILESTONES else None
            new_freeze_used = today_iso if burned_freeze else freeze_last

            self.cursor.execute(
                'UPDATE user_profiles SET activity_streak_days = ?, '
                'activity_streak_last_date = ?, activity_streak_best = ?, '
                'activity_streak_freeze_used_date = ? '
                'WHERE discord_id = ?',
                (new_streak, today_iso, new_best, new_freeze_used, discord_id),
            )
            self.connection.commit()
            result.update(streak=new_streak, best=new_best, milestone=milestone)
            return result
        except sqlite3.Error as exc:
            debug.error_log(f"update_activity_streak failed for {discord_id}: {exc!r}")
            return result

    # Map delta-kwarg → column name in user_profiles. Used by check_personal_bests.
    _PB_COLUMNS = {
        "kill":   "pb_kill_delta",
        "pve":    "pb_pve_delta",
        "gather": "pb_gather_delta",
        "craft":  "pb_craft_delta",
        "fish":   "pb_fish_delta",
    }
    # Floor on what counts as a "personal best" worth announcing.
    # Below this, the noise from sub-thousand deltas drowns the feed.
    _PB_MIN_DELTA = 10_000

    def check_personal_bests(self, discord_id: str, deltas: dict) -> list[dict]:
        """Compare ``deltas`` against the player's stored bests and return a
        list of NEW records that beat the prior PB.

        ``deltas`` keys: ``kill``, ``pve``, ``gather``, ``craft``, ``fish``.
        Each value is a positive int.

        Returns ``[{metric, prior, current}, ...]``. The DB is updated in
        place with the new bests. Skips:
          * deltas below ``_PB_MIN_DELTA`` (noise floor)
          * the very first PB per metric (prior = 0) — new players shouldn't
            get a hall-of-fame embed on their first sync.
        """
        new_bests: list[dict] = []
        if not deltas:
            return new_bests
        try:
            if not self.connection:
                self.connect()
            cols = ", ".join(self._PB_COLUMNS.values())
            self.cursor.execute(
                f"SELECT {cols} FROM user_profiles WHERE discord_id = ?",
                (discord_id,),
            )
            row = self.cursor.fetchone()
            if row is None:
                return new_bests

            updates: list[tuple[str, int]] = []
            for key, col in self._PB_COLUMNS.items():
                cur_delta = int(deltas.get(key, 0) or 0)
                if cur_delta < self._PB_MIN_DELTA:
                    continue
                prior = int(row[col] or 0)
                if cur_delta > prior:
                    updates.append((col, cur_delta))
                    if prior > 0:
                        new_bests.append(
                            {"metric": key, "prior": prior, "current": cur_delta}
                        )
            if updates:
                set_sql = ", ".join(f"{col} = ?" for col, _ in updates)
                params = [v for _, v in updates] + [discord_id]
                self.cursor.execute(
                    f"UPDATE user_profiles SET {set_sql} WHERE discord_id = ?",
                    params,
                )
                self.connection.commit()
            return new_bests
        except sqlite3.Error as exc:
            debug.error_log(f"check_personal_bests failed for {discord_id}: {exc!r}")
            return new_bests

    def top_streaks(self, by: str = "current", limit: int = 10,
                    home_guild: str | None = None) -> list:
        """Return the top N profiles by activity streak.

        ``by``: ``"current"`` (active streak) or ``"best"`` (all-time best).
        If ``home_guild`` is supplied, only profiles whose current
        ``guild_name`` matches (case-insensitive) are included.
        """
        column = "activity_streak_days" if by == "current" else "activity_streak_best"
        try:
            if not self.connection:
                self.connect()
            params: list = []
            guild_clause = ""
            if home_guild:
                guild_clause = " AND LOWER(guild_name) = LOWER(?)"
                params.append(home_guild)
            params.append(int(limit))
            self.cursor.execute(f'''
                SELECT discord_id, albion_name, username,
                       activity_streak_days  AS current_streak,
                       activity_streak_best  AS best_streak,
                       activity_streak_last_date AS last_date
                FROM user_profiles
                WHERE {column} > 0
                  AND albion_player_id IS NOT NULL
                  AND albion_name IS NOT NULL
                  AND TRIM(albion_name) != ''{guild_clause}
                ORDER BY {column} DESC, albion_name ASC
                LIMIT ?
            ''', tuple(params))
            return [dict(row) for row in self.cursor.fetchall()]
        except sqlite3.Error as exc:
            debug.error_log(f"top_streaks failed: {exc!r}")
            return []

    def find_broken_streaks(self, today_iso: str, min_streak: int = 7) -> list:
        """Return profiles whose ``activity_streak_days >= min_streak`` but
        whose ``activity_streak_last_date`` is older than two days ago — i.e.
        the streak is unrecoverable even with a freeze (which only covers a
        single missed day). The caller announces and calls :meth:`clear_streak`.
        Members who missed exactly yesterday still have today UTC to come
        back and burn their freeze.
        """
        try:
            today = _datetime.datetime.strptime(today_iso, "%Y-%m-%d").date()
            cutoff = (today - _datetime.timedelta(days=2)).isoformat()
            if not self.connection:
                self.connect()
            self.cursor.execute('''
                SELECT discord_id, albion_name, username,
                       activity_streak_days AS streak,
                       activity_streak_last_date AS last_date
                FROM user_profiles
                WHERE activity_streak_days >= ?
                  AND activity_streak_last_date IS NOT NULL
                  AND activity_streak_last_date < ?
                ORDER BY activity_streak_days DESC
            ''', (int(min_streak), cutoff))
            return [dict(row) for row in self.cursor.fetchall()]
        except sqlite3.Error as exc:
            debug.error_log(f"find_broken_streaks failed: {exc!r}")
            return []

    def clear_streak(self, discord_id: str) -> None:
        """Zero out the active streak (preserves activity_streak_best)."""
        self.execute(
            'UPDATE user_profiles SET activity_streak_days = 0 '
            'WHERE discord_id = ?',
            (discord_id,),
        )

    def set_was_in_home_guild(self, discord_id: str, value: bool = True) -> None:
        """Persist whether this profile has ever been seen in the home in-game guild."""
        self.execute(
            'UPDATE user_profiles SET was_in_home_guild = ? WHERE discord_id = ?',
            (1 if value else 0, discord_id)
        )

    def fetch_tu_history(self):
        """Return profiles flagged as ever-having-been in the home guild.

        Includes current members, Alumni, and anyone manually flagged. Used by
        the admin tracking view.
        """
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute('''
                SELECT discord_id, username, albion_name, guild_name,
                       lifecycle_role, verified_date
                FROM user_profiles
                WHERE was_in_home_guild = 1
                ORDER BY lifecycle_role, albion_name
            ''')
            return [dict(row) for row in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"Error fetching TU history: {e}")
            return []

    def fetch_all_registered_with_verified_date(self):
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute('''
                SELECT discord_id, lifecycle_role, verified_date, last_activity_date,
                       was_in_home_guild, guild_name,
                       kill_fame, pve_total, gather_all
                FROM user_profiles
                WHERE albion_player_id IS NOT NULL
            ''')
            return [dict(row) for row in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"Error fetching profiles for lifecycle check: {e}")
            return []

    def set_pending_verification(self, discord_id: str, pending: bool):
        self.execute(
            'UPDATE user_profiles SET pending_verification = ? WHERE discord_id = ?',
            (1 if pending else 0, discord_id)
        )

    def fetch_pending_verifications(self):
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                'SELECT discord_id FROM user_profiles WHERE pending_verification = 1'
            )
            return [dict(row) for row in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"Error fetching pending verifications: {e}")
            return []

    def set_screenshot_url(self, discord_id: str, url: str) -> None:
        self.execute(
            'UPDATE user_profiles SET screenshot_url = ?, pending_verification = 1 WHERE discord_id = ?',
            (url, discord_id)
        )

    def set_verified_date(self, discord_id: str) -> None:
        self.execute(
            'UPDATE user_profiles SET verified_date = CURRENT_TIMESTAMP WHERE discord_id = ?',
            (discord_id,)
        )

    def insert_user_basic_info(self, discord_id, username, join_date):
        self.execute('''
            INSERT OR IGNORE INTO user_profiles (discord_id, username, join_date)
            VALUES (?, ?, ?)
        ''', (discord_id, username, join_date))

    def clear_user_albion_info(self, discord_id: str):
        """Resets all Albion fields so a user can re-register."""
        self.execute('''
            UPDATE user_profiles
            SET albion_player_id = NULL, albion_name = NULL,
                guild_id = NULL, guild_name = NULL,
                alliance_id = NULL, alliance_name = NULL, alliance_tag = NULL,
                kill_fame = 0, death_fame = 0, fame_ratio = 0.0, average_item_power = 0.0,
                pve_total = 0, pve_royal = 0, pve_outlands = 0, pve_avalon = 0,
                pve_hellgate = 0, pve_corrupted = 0, pve_mists = 0,
                gather_fiber = 0, gather_hide = 0, gather_ore = 0,
                gather_rock = 0, gather_wood = 0, gather_all = 0,
                crafting_fame = 0, crystal_league = 0, fishing_fame = 0, farming_fame = 0,
                screenshot_url = NULL, last_updated = NULL,
                pending_verification = 0, lifecycle_role = NULL, verified_date = NULL,
                last_activity_date = NULL
            WHERE discord_id = ?
        ''', (discord_id,))
        debug.info_log(f"Cleared Albion info for user {discord_id}.")

    def fetch_user_profile(self, discord_id):
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute('''
                SELECT * FROM user_profiles WHERE discord_id = ?
            ''', (discord_id,))
            row = self.cursor.fetchone()
            return dict(row) if row else None
        except sqlite3.Error as e:
            debug.error_log(f"Error fetching user profile for {discord_id}: {e}")
            return None

    def fetch_user_profile_by_player_id(self, albion_player_id):
        """Return the profile (if any) currently linked to a given Albion player_id.

        Used to detect duplicate links during registration/application so two
        Discord accounts can't claim the same in-game character.
        """
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute('''
                SELECT * FROM user_profiles WHERE albion_player_id = ?
            ''', (str(albion_player_id),))
            row = self.cursor.fetchone()
            return dict(row) if row else None
        except sqlite3.Error as e:
            debug.error_log(f"Error fetching user profile by player_id {albion_player_id}: {e}")
            return None
        
    def update_user_albion_info(self, discord_id, albion_player_id, albion_name, stats: dict):
        self.execute('''
            UPDATE user_profiles
            SET albion_player_id = ?, albion_name = ?,
                guild_id = ?, guild_name = ?,
                alliance_id = ?, alliance_name = ?, alliance_tag = ?,
                kill_fame = ?, death_fame = ?, fame_ratio = ?, average_item_power = ?,
                pve_total = ?, pve_royal = ?, pve_outlands = ?, pve_avalon = ?,
                pve_hellgate = ?, pve_corrupted = ?, pve_mists = ?,
                gather_fiber = ?, gather_hide = ?, gather_ore = ?,
                gather_rock = ?, gather_wood = ?, gather_all = ?,
                crafting_fame = ?, crystal_league = ?, fishing_fame = ?, farming_fame = ?,
                last_updated = CURRENT_TIMESTAMP
            WHERE discord_id = ?
        ''', (
            albion_player_id, albion_name,
            stats.get("guild_id"), stats.get("guild_name"),
            stats.get("alliance_id"), stats.get("alliance_name"), stats.get("alliance_tag"),
            stats.get("kill_fame", 0), stats.get("death_fame", 0), stats.get("fame_ratio", 0.0), stats.get("average_item_power", 0.0),
            stats.get("pve_total", 0), stats.get("pve_royal", 0), stats.get("pve_outlands", 0), stats.get("pve_avalon", 0),
            stats.get("pve_hellgate", 0), stats.get("pve_corrupted", 0), stats.get("pve_mists", 0),
            stats.get("gather_fiber", 0), stats.get("gather_hide", 0), stats.get("gather_ore", 0),
            stats.get("gather_rock", 0), stats.get("gather_wood", 0), stats.get("gather_all", 0),
            stats.get("crafting_fame", 0), stats.get("crystal_league", 0), stats.get("fishing_fame", 0), stats.get("farming_fame", 0),
            discord_id
        ))

    def initialize_guilds_table(self):
        self.execute('''
            CREATE TABLE IF NOT EXISTS guilds (
                guild_id        TEXT PRIMARY KEY,
                guild_name      TEXT,
                founder_name    TEXT,
                founded         TEXT,
                kill_fame       INTEGER DEFAULT 0,
                death_fame      INTEGER DEFAULT 0,
                member_count    INTEGER DEFAULT 0,
                alliance_id     TEXT,
                alliance_name   TEXT,
                alliance_tag    TEXT,
                last_updated    TEXT
            )
        ''')
        debug.info_log("Initialized guilds table.")

    def upsert_guild(self, guild_id, guild_name, founder_name, founded,
                     kill_fame, death_fame, member_count,
                     alliance_id, alliance_name, alliance_tag):
        self.execute('''
            INSERT INTO guilds (
                guild_id, guild_name, founder_name, founded,
                kill_fame, death_fame, member_count,
                alliance_id, alliance_name, alliance_tag,
                last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(guild_id) DO UPDATE SET
                guild_name   = excluded.guild_name,
                founder_name = excluded.founder_name,
                founded      = excluded.founded,
                kill_fame    = excluded.kill_fame,
                death_fame   = excluded.death_fame,
                member_count = excluded.member_count,
                alliance_id  = excluded.alliance_id,
                alliance_name = excluded.alliance_name,
                alliance_tag = excluded.alliance_tag,
                last_updated = CURRENT_TIMESTAMP
        ''', (guild_id, guild_name, founder_name, founded,
              kill_fame, death_fame, member_count,
              alliance_id, alliance_name, alliance_tag))

    def fetch_guild(self, guild_id):
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute('SELECT * FROM guilds WHERE guild_id = ?', (guild_id,))
            row = self.cursor.fetchone()
            return dict(row) if row else None
        except sqlite3.Error as e:
            debug.error_log(f"Error fetching guild {guild_id}: {e}")
            return None
        
    def fetch_all_guilds(self):
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute('SELECT * FROM guilds')
            rows = self.cursor.fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as e:
            debug.error_log(f"Error fetching all guilds: {e}")
            return []

    def fetch_all_registered_profiles(self):
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute('''
                SELECT discord_id, albion_player_id, albion_name,
                       guild_id, guild_name,
                       alliance_id, alliance_name, alliance_tag,
                       lifecycle_role, was_in_home_guild,
                       kill_fame, death_fame, pve_total, gather_all,
                       crafting_fame, fishing_fame, last_updated
                FROM user_profiles
                WHERE albion_player_id IS NOT NULL
            ''')
            rows = self.cursor.fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as e:
            debug.error_log(f"Error fetching registered profiles: {e}")
            return []

    def initialize_guild_stats_history_table(self):
        self.execute('''
            CREATE TABLE IF NOT EXISTS guild_stats_history (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id     TEXT NOT NULL,
                kill_fame    INTEGER DEFAULT 0,
                death_fame   INTEGER DEFAULT 0,
                member_count INTEGER DEFAULT 0,
                recorded_at  TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_gsh_guild_time "
            "ON guild_stats_history(guild_id, recorded_at)"
        )
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_gsh_recorded_at "
            "ON guild_stats_history(recorded_at)"
        )
        debug.info_log("Initialized guild_stats_history table.")

    def insert_guild_history(self, guild_id, kill_fame, death_fame, member_count):
        self.execute('''
            INSERT INTO guild_stats_history (guild_id, kill_fame, death_fame, member_count)
            VALUES (?, ?, ?, ?)
        ''', (guild_id, kill_fame, death_fame, member_count))

    def initialize_player_stats_history_table(self):
        self.execute('''
            CREATE TABLE IF NOT EXISTS player_stats_history (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id      TEXT NOT NULL,
                kill_fame       INTEGER DEFAULT 0,
                death_fame      INTEGER DEFAULT 0,
                pve_total       INTEGER DEFAULT 0,
                gather_all      INTEGER DEFAULT 0,
                crafting_fame   INTEGER DEFAULT 0,
                average_item_power REAL DEFAULT 0.0,
                recorded_at     TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Composite index drives every history query (LAG partitions, top movers,
        # hourly buckets). Without it SQLite full-scans this table.
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_psh_player_time "
            "ON player_stats_history(discord_id, recorded_at)"
        )
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_psh_recorded_at "
            "ON player_stats_history(recorded_at)"
        )
        debug.info_log("Initialized player_stats_history table.")

    def insert_player_history(self, discord_id, kill_fame, death_fame, pve_total,
                               gather_all, crafting_fame, average_item_power):
        self.execute('''
            INSERT INTO player_stats_history
                (discord_id, kill_fame, death_fame, pve_total, gather_all, crafting_fame, average_item_power)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (discord_id, kill_fame, death_fame, pve_total, gather_all, crafting_fame, average_item_power))

    def prune_stats_history(self, days: int = 90) -> dict:
        """Delete stats-history rows older than ``days`` days. Keeps the DB small
        and queries fast. Always preserves the most recent row per player/guild
        so 'last seen' baselines aren't lost.

        Returns a dict: {'players': int, 'guilds': int} of rows deleted.
        """
        deleted = {"players": 0, "guilds": 0}
        if days <= 0:
            return deleted
        try:
            if not self.connection:
                self.connect()
            # Player history: keep latest row per discord_id even if older than cutoff.
            self.cursor.execute('''
                DELETE FROM player_stats_history
                WHERE recorded_at < datetime('now', ?)
                  AND id NOT IN (
                      SELECT MAX(id) FROM player_stats_history GROUP BY discord_id
                  )
            ''', (f'-{int(days)} days',))
            deleted["players"] = self.cursor.rowcount or 0
            # Guild history: same idea per guild_id.
            self.cursor.execute('''
                DELETE FROM guild_stats_history
                WHERE recorded_at < datetime('now', ?)
                  AND id NOT IN (
                      SELECT MAX(id) FROM guild_stats_history GROUP BY guild_id
                  )
            ''', (f'-{int(days)} days',))
            deleted["guilds"] = self.cursor.rowcount or 0
            self.connection.commit()
        except sqlite3.Error as e:
            debug.error_log(f"prune_stats_history failed: {e}")
        return deleted

    def purge_zero_stats_history(self) -> int:
        """Delete glitch-baseline rows from player_stats_history.

        A row is treated as a glitch when ``kill_fame = 0 AND death_fame = 0``
        AND the same player has at least one other row with ``kill_fame > 0``
        elsewhere in history. These come from registration glitches / API
        timeouts that wrote zero baselines; they break LAG-based delta queries
        (hourly fame chart, top movers) by inflating the first real sync into
        a phantom "lifetime fame earned in one hour" delta.

        Brand-new low-level players (who legitimately have zero fame in every
        row) are NOT touched — the subquery only matches discord_ids that have
        a non-zero kill_fame somewhere.

        Returns the number of rows deleted.
        """
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute('''
                DELETE FROM player_stats_history
                WHERE kill_fame = 0
                  AND death_fame = 0
                  AND discord_id IN (
                      SELECT discord_id FROM player_stats_history
                      WHERE kill_fame > 0
                  )
            ''')
            n = self.cursor.rowcount or 0
            self.connection.commit()
            return n
        except sqlite3.Error as e:
            debug.error_log(f"purge_zero_stats_history failed: {e}")
            return 0

    def fetch_player_history(self, discord_id: str):
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute('''
                SELECT kill_fame, death_fame, pve_total, gather_all, crafting_fame, average_item_power, recorded_at
                FROM player_stats_history
                WHERE discord_id = ?
                ORDER BY recorded_at ASC
            ''', (discord_id,))
            return [dict(row) for row in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"Error fetching player history for {discord_id}: {e}")
            return []

    def fetch_guild_history(self, guild_id: str):
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute('''
                SELECT kill_fame, death_fame, member_count, recorded_at
                FROM guild_stats_history
                WHERE guild_id = ?
                ORDER BY recorded_at ASC
            ''', (guild_id,))
            return [dict(row) for row in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"Error fetching guild history for {guild_id}: {e}")
            return []

    def fetch_activity_data(self):
        """Return one row per player-hour where at least one stat increased (player was active).
        Uses LAG window function to detect increases between consecutive snapshots per player."""
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute('''
                WITH lagged AS (
                    SELECT
                        recorded_at,
                        CASE WHEN
                            kill_fame  > COALESCE(LAG(kill_fame)  OVER (PARTITION BY discord_id ORDER BY recorded_at), kill_fame)
                         OR pve_total  > COALESCE(LAG(pve_total)  OVER (PARTITION BY discord_id ORDER BY recorded_at), pve_total)
                         OR gather_all > COALESCE(LAG(gather_all) OVER (PARTITION BY discord_id ORDER BY recorded_at), gather_all)
                        THEN 1 ELSE 0 END AS was_active
                    FROM player_stats_history
                )
                SELECT recorded_at
                FROM lagged
                WHERE was_active = 1
                ORDER BY recorded_at
            ''')
            return [dict(row) for row in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"Error fetching activity data: {e}")
            return []

    # Whitelist of metric columns allowed for hourly delta queries.
    _HOURLY_METRICS = {
        "kill_fame", "death_fame", "pve_total", "gather_all",
        "crafting_fame", "average_item_power",
    }

    # Reject deltas that span a snapshot gap longer than this (minutes). A
    # genuine grind ticks every ~hour or two; gaps beyond this almost always
    # mean the bot was offline, the API timed out, or a profile was relinked,
    # in which case the delta isn't real "this hour" activity.
    _MAX_DELTA_GAP_MIN = 120

    def fetch_hourly_deltas(self, metric: str, days: int = 7,
                            mode: str = "sum") -> list[dict]:
        """For the given metric, return [{hour: 0..23, total, days_active}, ...].

        - ``mode='sum'`` (default): ``total`` is the raw summed positive delta
          across the window — useful when you want absolute volume.
        - ``mode='avg_per_day'``: ``total`` is divided by the number of distinct
          days that contributed any positive delta in that hour bucket, so a
          single 9-hour grind no longer dwarfs the rest of the day.

        Robustness:
        * Albion's API sometimes returns a stale (lower) value that bounces
          back next snapshot. We compute deltas against each player's running
          max, so the bounce-back contributes zero.
        * Deltas spanning snapshot gaps > ``_MAX_DELTA_GAP_MIN`` minutes are
          dropped — they're almost always bot-downtime or relink artifacts,
          not real "this hour" activity.
        * Rows where ``{metric} = 0`` are excluded BEFORE the window so a
          glitch-zero snapshot doesn't manufacture a lifetime-sized delta.
        """
        if metric not in self._HOURLY_METRICS:
            debug.error_log(f"fetch_hourly_deltas: invalid metric {metric!r}")
            return []
        if mode not in ("sum", "avg_per_day"):
            debug.error_log(f"fetch_hourly_deltas: invalid mode {mode!r}")
            return []
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(f'''
                WITH cleaned AS (
                    SELECT discord_id, recorded_at, {metric}
                    FROM player_stats_history
                    WHERE recorded_at >= datetime('now', ?)
                      AND {metric} > 0
                ),
                with_prev AS (
                    SELECT
                        recorded_at,
                        {metric} AS val,
                        MAX({metric}) OVER (
                            PARTITION BY discord_id
                            ORDER BY recorded_at
                            ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                        ) AS prev_max,
                        LAG(recorded_at) OVER (
                            PARTITION BY discord_id ORDER BY recorded_at
                        ) AS prev_at
                    FROM cleaned
                )
                SELECT
                    CAST(strftime('%H', recorded_at) AS INTEGER) AS hour,
                    SUM(val - prev_max) AS total,
                    COUNT(DISTINCT date(recorded_at)) AS days_active
                FROM with_prev
                WHERE prev_max IS NOT NULL
                  AND val > prev_max
                  AND prev_at IS NOT NULL
                  AND (julianday(recorded_at) - julianday(prev_at)) * 1440.0 <= ?
                GROUP BY hour
                ORDER BY hour
            ''', (f'-{int(days)} days', float(self._MAX_DELTA_GAP_MIN)))
            rows = [dict(row) for row in self.cursor.fetchall()]
            if mode == "avg_per_day":
                for r in rows:
                    d = max(1, int(r.get("days_active") or 1))
                    r["total"] = float(r["total"] or 0) / d
            return rows
        except sqlite3.Error as e:
            debug.error_log(f"Error fetching hourly deltas for {metric}: {e}")
            return []

    def fetch_top_movers(self, metric: str, since_iso: str, limit: int = 10,
                         home_guild: str | None = None) -> list[dict]:
        """Top N players by positive delta in ``metric`` since ``since_iso``.

        Computes the player's true gain in window as the sum of positive
        running-max increases — robust against API regressions that bounce
        between a stale (lower) value and the true value, which would
        otherwise inflate ``MAX - MIN`` by the regression magnitude.

        If ``home_guild`` is supplied, only players whose current
        ``user_profiles.guild_name`` matches (case-insensitive) are included.
        """
        if metric not in self._HOURLY_METRICS:
            debug.error_log(f"fetch_top_movers: invalid metric {metric!r}")
            return []
        try:
            if not self.connection:
                self.connect()
            params: list = [since_iso]
            guild_clause = ""
            if home_guild:
                guild_clause = " AND LOWER(u.guild_name) = LOWER(?)"
                params.append(home_guild)
            params.append(limit)
            self.cursor.execute(f'''
                WITH cleaned AS (
                    SELECT discord_id, recorded_at, {metric}
                    FROM player_stats_history
                    WHERE recorded_at >= ?
                      AND {metric} > 0
                ),
                with_prev AS (
                    SELECT discord_id,
                           {metric} AS val,
                           MAX({metric}) OVER (
                               PARTITION BY discord_id
                               ORDER BY recorded_at
                               ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                           ) AS prev_max,
                           LAG(recorded_at) OVER (
                               PARTITION BY discord_id ORDER BY recorded_at
                           ) AS prev_at,
                           recorded_at
                    FROM cleaned
                ),
                gained AS (
                    SELECT discord_id, SUM(val - prev_max) AS delta
                    FROM with_prev
                    WHERE prev_max IS NOT NULL
                      AND val > prev_max
                      AND prev_at IS NOT NULL
                      AND (julianday(recorded_at) - julianday(prev_at)) * 1440.0 <= ?
                    GROUP BY discord_id
                )
                SELECT u.albion_name AS name,
                       u.discord_id  AS discord_id,
                       g.delta       AS delta
                FROM gained g
                JOIN user_profiles u ON u.discord_id = g.discord_id
                WHERE g.delta > 0
                  AND u.albion_player_id IS NOT NULL{guild_clause}
                ORDER BY delta DESC
                LIMIT ?
            ''', (since_iso, float(self._MAX_DELTA_GAP_MIN), *params[1:]))
            return [dict(row) for row in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"Error fetching top movers ({metric}): {e}")
            return []

    def fetch_activity_heatmap(self, since_iso: str) -> list[dict]:
        """Player-hours of activity bucketed by (weekday, hour) in UTC.

        Returns rows ``{weekday: 0..6, hour: 0..23, n: count}`` where each
        ``n`` is the number of distinct (player, hour-bucket) pairs in which
        any tracked stat increased. Suitable for a 7×24 heatmap.

        ``weekday`` is SQLite's ``strftime('%w')`` — Sunday=0..Saturday=6.
        """
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute('''
                WITH lagged AS (
                    SELECT
                        recorded_at,
                        CASE WHEN
                            kill_fame  > COALESCE(LAG(kill_fame)  OVER w, kill_fame)
                         OR pve_total  > COALESCE(LAG(pve_total)  OVER w, pve_total)
                         OR gather_all > COALESCE(LAG(gather_all) OVER w, gather_all)
                        THEN 1 ELSE 0 END AS was_active
                    FROM player_stats_history
                    WHERE recorded_at >= ?
                    WINDOW w AS (PARTITION BY discord_id ORDER BY recorded_at)
                )
                SELECT
                    CAST(strftime('%w', recorded_at) AS INTEGER) AS weekday,
                    CAST(strftime('%H', recorded_at) AS INTEGER) AS hour,
                    SUM(was_active) AS n
                FROM lagged
                GROUP BY weekday, hour
            ''', (since_iso,))
            return [dict(row) for row in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"Error fetching activity heatmap: {e}")
            return []

    def initialize_guild_config_table(self):
        self.execute('''
            CREATE TABLE IF NOT EXISTS guild_config (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        debug.info_log("Initialized guild_config table.")

    def get_config(self, key):
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute('SELECT value FROM guild_config WHERE key = ?', (key,))
            row = self.cursor.fetchone()
            return row["value"] if row else None
        except Exception as e:
            debug.error_log(f"Error getting config {key}: {e}")
            return None

    def set_config(self, key, value):
        self.execute('INSERT OR REPLACE INTO guild_config (key, value) VALUES (?, ?)', (key, value))
        debug.info_log(f"Set config {key} = {value}")

    # ── Recent Discord message memory ───────────────────────────────────────

    def initialize_message_archive_table(self):
        self.execute('''
            CREATE TABLE IF NOT EXISTS message_archive (
                guild_id         TEXT NOT NULL,
                channel_id       TEXT NOT NULL,
                message_id       TEXT NOT NULL,
                author_id        TEXT NOT NULL,
                author_name      TEXT,
                channel_name     TEXT,
                category_name    TEXT,
                content          TEXT,
                attachment_count INTEGER NOT NULL DEFAULT 0,
                attachment_names TEXT,
                jump_url         TEXT,
                is_bot           INTEGER NOT NULL DEFAULT 0,
                created_at       TEXT NOT NULL,
                archived_at      TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                PRIMARY KEY (guild_id, channel_id, message_id)
            )
        ''')
        debug.info_log("Initialized message_archive table.")

    def archive_message(
        self,
        *,
        guild_id: str,
        channel_id: str,
        message_id: str,
        author_id: str,
        author_name: str,
        channel_name: str,
        category_name: str | None,
        content: str,
        attachment_count: int,
        attachment_names: str | None,
        jump_url: str,
        is_bot: bool,
        created_at: str,
    ) -> None:
        self.execute(
            '''
            INSERT OR REPLACE INTO message_archive (
                guild_id, channel_id, message_id, author_id, author_name,
                channel_name, category_name, content, attachment_count,
                attachment_names, jump_url, is_bot, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                guild_id,
                channel_id,
                message_id,
                author_id,
                author_name,
                channel_name,
                category_name,
                content,
                int(attachment_count or 0),
                attachment_names,
                jump_url,
                1 if is_bot else 0,
                created_at,
            ),
            quiet=True,
        )

    def fetch_message_context(
        self,
        *,
        channel_id: str,
        limit: int = 25,
        author_id: str | None = None,
        search: str | None = None,
        include_bots: bool = False,
    ) -> list[dict]:
        try:
            if not self.connection:
                self.connect()
            where = ["channel_id = ?"]
            params: list = [str(channel_id)]
            if author_id:
                where.append("author_id = ?")
                params.append(str(author_id))
            if search:
                where.append("(LOWER(content) LIKE ? OR LOWER(attachment_names) LIKE ?)")
                needle = f"%{search.lower()}%"
                params.extend([needle, needle])
            if not include_bots:
                where.append("is_bot = 0")
            params.append(max(1, min(int(limit or 25), 100)))
            self.cursor.execute(
                f'''
                SELECT * FROM (
                    SELECT * FROM message_archive
                    WHERE {' AND '.join(where)}
                    ORDER BY datetime(created_at) DESC
                    LIMIT ?
                )
                ORDER BY datetime(created_at) ASC
                ''',
                tuple(params),
            )
            return [dict(row) for row in self.cursor.fetchall()]
        except (sqlite3.Error, ValueError) as e:
            debug.error_log(f"fetch_message_context error: {e}")
            return []

    def purge_old_message_archive(self, retention_days: int) -> int:
        try:
            if not self.connection:
                self.connect()
            days = max(1, int(retention_days or 30))
            self.cursor.execute(
                "DELETE FROM message_archive WHERE datetime(created_at) < datetime('now', ?)",
                (f"-{days} days",),
            )
            deleted = int(self.cursor.rowcount or 0)
            self.connection.commit()
            return deleted
        except (sqlite3.Error, ValueError) as e:
            debug.error_log(f"purge_old_message_archive error: {e}")
            return 0

    def count_message_archive(self) -> int:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute("SELECT COUNT(*) AS n FROM message_archive")
            row = self.cursor.fetchone()
            return int(row["n"] if row else 0)
        except sqlite3.Error as e:
            debug.error_log(f"count_message_archive error: {e}")
            return 0

    # ── Activity points (weekly / monthly / season) ──────────────────────────

    _POINT_WINDOWS = ("weekly", "monthly", "season")

    def add_points(self, discord_id: str, amount: int) -> None:
        """Add the given amount to all three rolling point windows for a user.

        Silently no-ops if the user has no profile row (unregistered users
        don't earn points). Negative amounts are clamped at 0 per window so a
        bad reduction can't drive a window negative.
        """
        if not amount:
            return
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute('''
                UPDATE user_profiles
                SET points_weekly  = MAX(0, COALESCE(points_weekly,  0) + ?),
                    points_monthly = MAX(0, COALESCE(points_monthly, 0) + ?),
                    points_season  = MAX(0, COALESCE(points_season,  0) + ?)
                WHERE discord_id = ?
            ''', (amount, amount, amount, discord_id))
            self.connection.commit()
        except sqlite3.Error as e:
            debug.error_log(f"Error adding {amount} points to {discord_id}: {e}")

    def get_points(self, discord_id: str) -> dict:
        """Return {'weekly': int, 'monthly': int, 'season': int} for a user."""
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                'SELECT points_weekly, points_monthly, points_season '
                'FROM user_profiles WHERE discord_id = ?',
                (discord_id,)
            )
            row = self.cursor.fetchone()
            if not row:
                return {"weekly": 0, "monthly": 0, "season": 0}
            return {
                "weekly":  int(row["points_weekly"]  or 0),
                "monthly": int(row["points_monthly"] or 0),
                "season":  int(row["points_season"]  or 0),
            }
        except sqlite3.Error as e:
            debug.error_log(f"Error fetching points for {discord_id}: {e}")
            return {"weekly": 0, "monthly": 0, "season": 0}

    # ── Silver ledger ────────────────────────────────────────────────────────
    #
    # silver_balance convention:
    #   positive value → guild owes the member that many silver
    #   negative value → member owes the guild that much silver
    # Bounty payouts and approved regear requests credit the member (+).
    # Officers settling debts in-game post a settlement (-) to zero it out.

    def adjust_silver_balance(
        self,
        discord_id: str,
        delta: int,
        reason: str,
        ref_type: str | None = None,
        ref_id: str | None = None,
        actor_id: str | None = None,
    ) -> int | None:
        """Atomically add `delta` to a member's silver_balance and append a
        ledger row. Returns the new balance, or None if the profile row is
        missing or the write fails. Zero deltas are no-ops.
        """
        if not delta:
            return None
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                'UPDATE user_profiles '
                'SET silver_balance = COALESCE(silver_balance, 0) + ? '
                'WHERE discord_id = ?',
                (int(delta), discord_id),
            )
            if self.cursor.rowcount == 0:
                debug.error_log(
                    f"adjust_silver_balance: no profile for {discord_id}; "
                    f"skipped delta={delta} reason={reason!r}."
                )
                self.connection.rollback()
                return None
            self.cursor.execute(
                'INSERT INTO silver_ledger '
                '(discord_id, delta, reason, ref_type, ref_id, actor_id) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (discord_id, int(delta), reason, ref_type, ref_id, actor_id),
            )
            self.connection.commit()
            row = self.cursor.execute(
                'SELECT silver_balance FROM user_profiles WHERE discord_id = ?',
                (discord_id,),
            ).fetchone()
            new_bal = int(row["silver_balance"]) if row else 0
            debug.info_log(
                f"silver_balance: {discord_id} {delta:+,} → {new_bal:+,} "
                f"({reason}; ref={ref_type}:{ref_id}; actor={actor_id})"
            )
            return new_bal
        except sqlite3.Error as e:
            debug.error_log(f"adjust_silver_balance failed for {discord_id}: {e}")
            try:
                self.connection.rollback()
            except sqlite3.Error:
                pass
            return None

    def fetch_silver_balance(self, discord_id: str) -> int:
        """Return current silver_balance for a member (0 if no profile)."""
        try:
            if not self.connection:
                self.connect()
            row = self.cursor.execute(
                'SELECT silver_balance FROM user_profiles WHERE discord_id = ?',
                (discord_id,),
            ).fetchone()
            return int(row["silver_balance"] or 0) if row else 0
        except sqlite3.Error as e:
            debug.error_log(f"fetch_silver_balance failed for {discord_id}: {e}")
            return 0

    def fetch_profile_by_albion_name(self, albion_name: str) -> dict | None:
        """Look up a registered profile by case-insensitive Albion in-game name.
        Returns the row dict, or None if no match."""
        if not albion_name:
            return None
        try:
            if not self.connection:
                self.connect()
            row = self.cursor.execute(
                'SELECT discord_id, albion_name, username, silver_balance, '
                '       lifecycle_role, guild_name '
                '  FROM user_profiles '
                ' WHERE LOWER(albion_name) = LOWER(?) '
                ' LIMIT 1',
                (albion_name.strip(),),
            ).fetchone()
            return dict(row) if row else None
        except sqlite3.Error as e:
            debug.error_log(f"fetch_profile_by_albion_name error: {e}")
            return None

    def fetch_silver_debts(self) -> list[dict]:
        """Return every profile with non-zero silver_balance, sorted with the
        biggest amounts the *guild owes* first (largest positive), then the
        biggest amounts members *owe the guild* (most negative). Each row is
        a dict with discord_id, albion_name, username, silver_balance.
        """
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute('''
                SELECT discord_id, albion_name, username, silver_balance
                FROM user_profiles
                WHERE COALESCE(silver_balance, 0) != 0
                ORDER BY silver_balance DESC
            ''')
            return [dict(r) for r in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"fetch_silver_debts failed: {e}")
            return []

    def fetch_silver_ledger(self, discord_id: str, limit: int = 20) -> list[dict]:
        """Return the most recent ledger entries for one member."""
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute('''
                SELECT id, delta, reason, ref_type, ref_id, actor_id, created_at
                FROM silver_ledger
                WHERE discord_id = ?
                ORDER BY id DESC
                LIMIT ?
            ''', (discord_id, int(limit)))
            return [dict(r) for r in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"fetch_silver_ledger failed for {discord_id}: {e}")
            return []

    def fetch_unpaid_silver_aged(self, min_age_days: int = 7,
                                  min_amount: int = 1,
                                  home_guild: str | None = None) -> list[dict]:
        """Return members the guild owes silver to whose oldest credit is at
        least ``min_age_days`` old. Each row:
        ``{discord_id, albion_name, username, balance, oldest_credit_at, days_waiting}``.

        ``oldest_credit_at`` is the timestamp of that member's earliest
        positive ledger entry — a conservative "how long have they been
        waiting" anchor. Even if they got partial payments, the date of the
        original debt is what officers should see in a reminder.
        """
        try:
            if not self.connection:
                self.connect()
            params: list = [int(min_amount)]
            guild_clause = ""
            if home_guild:
                guild_clause = " AND LOWER(u.guild_name) = LOWER(?)"
                params.append(home_guild)
            params.append(int(min_age_days))
            self.cursor.execute(
                f'''
                SELECT u.discord_id,
                       u.albion_name,
                       u.username,
                       u.silver_balance AS balance,
                       MIN(l.created_at) AS oldest_credit_at,
                       CAST(julianday('now') - julianday(MIN(l.created_at))
                            AS INTEGER) AS days_waiting
                FROM user_profiles u
                JOIN silver_ledger l ON l.discord_id = u.discord_id
                WHERE COALESCE(u.silver_balance, 0) >= ?
                  AND l.delta > 0{guild_clause}
                GROUP BY u.discord_id
                HAVING days_waiting >= ?
                ORDER BY days_waiting DESC, balance DESC
                ''',
                tuple(params),
            )
            return [dict(r) for r in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"fetch_unpaid_silver_aged failed: {e}")
            return []

    # ── Guild treasury (in-game silver balance, recorded daily) ──────────────

    def record_guild_treasury(
        self, date: str, balance: int,
        recorded_by: str | None = None, note: str | None = None,
    ) -> bool:
        """Upsert one daily snapshot. `date` is a YYYY-MM-DD string."""
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute('''
                INSERT INTO guild_treasury_history (date, balance, recorded_by, note)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    balance     = excluded.balance,
                    recorded_by = excluded.recorded_by,
                    note        = excluded.note,
                    recorded_at = CURRENT_TIMESTAMP
            ''', (date, int(balance), recorded_by, note))
            self.connection.commit()
            return True
        except sqlite3.Error as e:
            debug.error_log(f"record_guild_treasury failed for {date}: {e}")
            return False

    def fetch_guild_treasury_history(self, days: int = 30) -> list[dict]:
        """Return the most recent N days of treasury snapshots, ordered ASC."""
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute('''
                SELECT date, balance, recorded_by, note, recorded_at
                FROM guild_treasury_history
                ORDER BY date DESC
                LIMIT ?
            ''', (int(days),))
            rows = [dict(r) for r in self.cursor.fetchall()]
            rows.reverse()  # ASC for plotting
            return rows
        except sqlite3.Error as e:
            debug.error_log(f"fetch_guild_treasury_history failed: {e}")
            return []

    def fetch_latest_guild_treasury(self) -> dict | None:
        try:
            if not self.connection:
                self.connect()
            row = self.cursor.execute(
                'SELECT date, balance, recorded_by, note, recorded_at '
                'FROM guild_treasury_history ORDER BY date DESC LIMIT 1'
            ).fetchone()
            return dict(row) if row else None
        except sqlite3.Error as e:
            debug.error_log(f"fetch_latest_guild_treasury failed: {e}")
            return None

    # ── Guild revenue (append-only income log) ──────────────────────────────

    def record_guild_revenue(
        self,
        *,
        date: str,
        source: str,
        amount: int,
        rate: int | None = None,
        recorded_by: str | None = None,
        note: str | None = None,
        base_amount: int | None = None,
        matched_count: int | None = None,
        unmatched_count: int | None = None,
    ) -> int:
        """Append one revenue row. Returns the new row id (or 0 on failure)."""
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                'INSERT INTO guild_revenue '
                '(date, source, amount, rate, recorded_by, note, '
                ' base_amount, matched_count, unmatched_count) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    date, source, int(amount), rate, recorded_by, note,
                    base_amount, matched_count, unmatched_count,
                ),
            )
            self.connection.commit()
            return int(self.cursor.lastrowid or 0)
        except sqlite3.Error as e:
            debug.error_log(f"record_guild_revenue failed: {e}")
            return 0

    def fetch_guild_revenue_total(
        self, *, since_iso: str | None = None, source: str | None = None,
    ) -> int:
        """Sum of revenue rows. Pass ``since_iso`` to limit to a window."""
        try:
            if not self.connection:
                self.connect()
            sql = "SELECT COALESCE(SUM(amount), 0) AS total FROM guild_revenue WHERE 1=1"
            params: list = []
            if since_iso:
                sql += " AND date >= ?"
                params.append(since_iso)
            if source:
                sql += " AND source = ?"
                params.append(source)
            row = self.cursor.execute(sql, params).fetchone()
            return int((row["total"] if row else 0) or 0)
        except sqlite3.Error as e:
            debug.error_log(f"fetch_guild_revenue_total failed: {e}")
            return 0

    def fetch_recent_guild_revenue(self, limit: int = 10) -> list[dict]:
        """Return the most recent revenue rows (newest first)."""
        try:
            if not self.connection:
                self.connect()
            rows = self.cursor.execute(
                'SELECT id, date, source, amount, rate, recorded_by, note, '
                '       base_amount, matched_count, unmatched_count, created_at '
                '  FROM guild_revenue ORDER BY id DESC LIMIT ?',
                (int(limit),),
            ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.Error as e:
            debug.error_log(f"fetch_recent_guild_revenue failed: {e}")
            return []

    def reset_points_window(self, window: str) -> int:
        """Reset a single window ('weekly' | 'monthly' | 'season') to 0 for everyone.

        Returns the number of rows affected.
        """
        if window not in self._POINT_WINDOWS:
            debug.error_log(f"reset_points_window: invalid window {window!r}")
            return 0
        column = f"points_{window}"
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(f'UPDATE user_profiles SET {column} = 0')
            self.connection.commit()
            return self.cursor.rowcount or 0
        except sqlite3.Error as e:
            debug.error_log(f"Error resetting {window} points: {e}")
            return 0

    def top_points(self, window: str, limit: int = 10,
                   home_guild: str | None = None) -> list:
        """Return top N profiles for a window. Each row is a dict with discord_id,
        albion_name, username, and the requested points column aliased as 'points'.

        If ``home_guild`` is supplied, only profiles whose current
        ``guild_name`` matches (case-insensitive) are included.
        """
        if window not in self._POINT_WINDOWS:
            return []
        column = f"points_{window}"
        try:
            if not self.connection:
                self.connect()
            params: list = []
            guild_clause = ""
            if home_guild:
                guild_clause = " AND LOWER(guild_name) = LOWER(?)"
                params.append(home_guild)
            params.append(int(limit))
            # Only show fully-registered profiles (those with a linked Albion
            # character) so the leaderboard always renders the in-game name.
            self.cursor.execute(f'''
                SELECT discord_id, albion_name, username, {column} AS points
                FROM user_profiles
                WHERE {column} > 0
                  AND albion_player_id IS NOT NULL
                  AND albion_name IS NOT NULL
                  AND TRIM(albion_name) != ''{guild_clause}
                ORDER BY {column} DESC, albion_name ASC
                LIMIT ?
            ''', tuple(params))
            return [dict(row) for row in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"Error fetching top {window} points: {e}")
            return []

    # ── Live graph trackers ───────────────────────────────────────────────────

    def initialize_live_graphs_table(self):
        self.execute('''
            CREATE TABLE IF NOT EXISTS live_graphs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                type       TEXT NOT NULL,   -- 'player' or 'guild'
                target_id  TEXT NOT NULL,   -- discord_id (player) or guild_id (guild)
                channel_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                UNIQUE(type, target_id)
            )
        ''')
        debug.info_log("Initialized live_graphs table.")

    def upsert_live_graph(self, type_: str, target_id: str, channel_id: str, message_id: str):
        self.execute('''
            INSERT INTO live_graphs (type, target_id, channel_id, message_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(type, target_id) DO UPDATE SET
                channel_id = excluded.channel_id,
                message_id = excluded.message_id
        ''', (type_, target_id, channel_id, message_id))

    def delete_live_graph(self, type_: str, target_id: str):
        self.execute(
            'DELETE FROM live_graphs WHERE type = ? AND target_id = ?',
            (type_, target_id)
        )

    def fetch_all_live_graphs(self):
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute('SELECT * FROM live_graphs')
            return [dict(row) for row in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"Error fetching live graphs: {e}")
            return []

    # ── Voice activity ───────────────────────────────────────────────────────
    #
    # Per-player per-UTC-day rollup of seconds spent in any voice channel.
    # Powered by cogs/voice.py listening on on_voice_state_update.

    def initialize_voice_activity_table(self) -> None:
        self.execute('''
            CREATE TABLE IF NOT EXISTS voice_activity (
                discord_id TEXT NOT NULL,
                date_utc   TEXT NOT NULL,    -- YYYY-MM-DD
                seconds    INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (discord_id, date_utc)
            )
        ''')
        self.execute(
            'CREATE INDEX IF NOT EXISTS idx_voice_activity_date '
            'ON voice_activity(date_utc)'
        )
        debug.info_log("Initialized voice_activity table.")

    def add_voice_seconds(self, discord_id: str, date_utc: str, seconds: int) -> None:
        if seconds <= 0:
            return
        self.execute('''
            INSERT INTO voice_activity (discord_id, date_utc, seconds)
            VALUES (?, ?, ?)
            ON CONFLICT(discord_id, date_utc) DO UPDATE SET
                seconds = seconds + excluded.seconds
        ''', (str(discord_id), date_utc, int(seconds)))

    def fetch_voice_seconds_total(self, discord_id: str) -> int:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                'SELECT COALESCE(SUM(seconds), 0) AS total '
                'FROM voice_activity WHERE discord_id = ?',
                (str(discord_id),),
            )
            row = self.cursor.fetchone()
            return int(row["total"]) if row else 0
        except sqlite3.Error as exc:
            debug.error_log(f"fetch_voice_seconds_total failed: {exc!r}")
            return 0

    def fetch_voice_seconds_window(self, discord_id: str, since_iso: str) -> int:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                'SELECT COALESCE(SUM(seconds), 0) AS total FROM voice_activity '
                'WHERE discord_id = ? AND date_utc >= ?',
                (str(discord_id), since_iso),
            )
            row = self.cursor.fetchone()
            return int(row["total"]) if row else 0
        except sqlite3.Error as exc:
            debug.error_log(f"fetch_voice_seconds_window failed: {exc!r}")
            return 0

    def top_voice(self, since_iso: str, limit: int = 10,
                  home_guild: str | None = None) -> list[dict]:
        """Top N profiles by voice seconds since ``since_iso`` (YYYY-MM-DD).

        If ``home_guild`` is supplied, only profiles whose current
        ``user_profiles.guild_name`` matches (case-insensitive) are included.
        """
        try:
            if not self.connection:
                self.connect()
            params: list = [since_iso]
            guild_clause = ""
            if home_guild:
                guild_clause = " AND LOWER(u.guild_name) = LOWER(?)"
                params.append(home_guild)
            params.append(int(limit))
            self.cursor.execute(f'''
                SELECT v.discord_id AS discord_id,
                       u.albion_name AS albion_name,
                       u.username   AS username,
                       SUM(v.seconds) AS seconds
                FROM voice_activity v
                JOIN user_profiles u ON u.discord_id = v.discord_id
                WHERE v.date_utc >= ?{guild_clause}
                GROUP BY v.discord_id
                ORDER BY seconds DESC
                LIMIT ?
            ''', tuple(params))
            return [dict(row) for row in self.cursor.fetchall()]
        except sqlite3.Error as exc:
            debug.error_log(f"top_voice failed: {exc!r}")
            return []

    # ── Staff applications ────────────────────────────────────────────────────

    def initialize_staff_applications_table(self):
        self.execute('''
            CREATE TABLE IF NOT EXISTS staff_applications (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id  TEXT NOT NULL,
                rank        TEXT NOT NULL,
                reason      TEXT,
                status      TEXT NOT NULL DEFAULT 'pending',
                applied_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                reviewed_at TEXT,
                reviewed_by TEXT,
                review_note TEXT,
                review_channel_id TEXT,
                review_message_id TEXT
            )
        ''')
        if not self.connection:
            self.connect()
        for column, ddl in (
            ("review_channel_id", "TEXT"),
            ("review_message_id", "TEXT"),
        ):
            try:
                self.cursor.execute(
                    f"ALTER TABLE staff_applications ADD COLUMN {column} {ddl}"
                )
                self.connection.commit()
            except sqlite3.OperationalError:
                pass
        debug.info_log("Initialized staff_applications table.")

    def insert_staff_application(self, discord_id: str, rank: str, reason: str) -> int:
        self.execute(
            'INSERT INTO staff_applications (discord_id, rank, reason) VALUES (?, ?, ?)',
            (discord_id, rank, reason)
        )
        return int(self.cursor.lastrowid or 0)

    def fetch_staff_application(self, app_id: int):
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute('SELECT * FROM staff_applications WHERE id = ?', (app_id,))
            row = self.cursor.fetchone()
            return dict(row) if row else None
        except sqlite3.Error as e:
            debug.error_log(f"Error fetching staff application {app_id}: {e}")
            return None

    def fetch_pending_application(self, discord_id: str, rank: str):
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "SELECT * FROM staff_applications WHERE discord_id = ? AND rank = ? AND status = 'pending'",
                (discord_id, rank)
            )
            row = self.cursor.fetchone()
            return dict(row) if row else None
        except sqlite3.Error as e:
            debug.error_log(f"Error fetching pending application: {e}")
            return None

    def fetch_pending_applications(self, rank: str = None):
        try:
            if not self.connection:
                self.connect()
            if rank:
                self.cursor.execute(
                    "SELECT * FROM staff_applications WHERE status = 'pending' AND rank = ? ORDER BY applied_at ASC",
                    (rank,)
                )
            else:
                self.cursor.execute(
                    "SELECT * FROM staff_applications WHERE status = 'pending' ORDER BY applied_at ASC"
                )
            return [dict(row) for row in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"Error fetching pending applications: {e}")
            return []

    def update_application_status(self, app_id: int, status: str, reviewer_id: str, note: str = None):
        self.execute('''
            UPDATE staff_applications
            SET status = ?, reviewed_at = CURRENT_TIMESTAMP, reviewed_by = ?, review_note = ?
            WHERE id = ?
        ''', (status, reviewer_id, note, app_id))

    def set_staff_application_review_message(
        self,
        app_id: int,
        channel_id: str | None,
        message_id: str | None,
    ) -> None:
        self.execute(
            "UPDATE staff_applications "
            "SET review_channel_id = ?, review_message_id = ? "
            "WHERE id = ?",
            (channel_id, message_id, app_id),
        )

    # ── Staff role tenure tracking ────────────────────────────────────────────

    def initialize_staff_role_grants_table(self):
        self.execute('''
            CREATE TABLE IF NOT EXISTS staff_role_grants (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id  TEXT NOT NULL,
                rank        TEXT NOT NULL,
                granted_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(discord_id, rank)
            )
        ''')
        debug.info_log("Initialized staff_role_grants table.")

    def record_staff_grant(self, discord_id: str, rank: str) -> None:
        """Record the first time a member was granted a staff rank.
        No-op if a record already exists (preserves the original tenure start)."""
        self.execute(
            'INSERT OR IGNORE INTO staff_role_grants (discord_id, rank) VALUES (?, ?)',
            (discord_id, rank)
        )

    def fetch_first_grant_date(self, discord_id: str, rank: str):
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                'SELECT granted_at FROM staff_role_grants WHERE discord_id = ? AND rank = ?',
                (discord_id, rank)
            )
            row = self.cursor.fetchone()
            return row["granted_at"] if row else None
        except sqlite3.Error as e:
            debug.error_log(f"Error fetching grant date for {discord_id}/{rank}: {e}")
            return None

    def revoke_staff_grants(self, discord_id: str, ranks: list[str] | tuple[str, ...] | None = None) -> int:
        """Remove staff-tenure rows for a member.

        Used when a member leaves the home Albion guild and their Discord staff
        positions are stripped. Keeping this table clean prevents dashboards
        and prerequisite checks from treating them as still holding a role.
        """
        try:
            if not self.connection:
                self.connect()
            if ranks:
                clean = [str(rank) for rank in ranks if str(rank).strip()]
                if not clean:
                    return 0
                placeholders = ",".join("?" for _ in clean)
                self.cursor.execute(
                    f"DELETE FROM staff_role_grants "
                    f"WHERE discord_id = ? AND rank IN ({placeholders})",
                    (str(discord_id), *clean),
                )
            else:
                self.cursor.execute(
                    "DELETE FROM staff_role_grants WHERE discord_id = ?",
                    (str(discord_id),),
                )
            removed = int(self.cursor.rowcount or 0)
            self.connection.commit()
            return removed
        except sqlite3.Error as e:
            debug.error_log(f"Error revoking staff grants for {discord_id}: {e}")
            return 0

    def fetch_staff_holders_with_activity(self, rank: str, discord_ids: list):
        """Given a list of discord IDs holding a rank, return their last_activity_date and activity_points,
        ordered for demotion priority (least active first: oldest activity, then lowest points)."""
        if not discord_ids:
            return []
        try:
            if not self.connection:
                self.connect()
            placeholders = ",".join("?" * len(discord_ids))
            self.cursor.execute(f'''
                SELECT discord_id, albion_name, last_activity_date, activity_points
                FROM user_profiles
                WHERE discord_id IN ({placeholders})
                ORDER BY (last_activity_date IS NULL) DESC,
                         last_activity_date ASC,
                         COALESCE(activity_points, 0) ASC
            ''', tuple(discord_ids))
            return [dict(row) for row in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"Error fetching staff holders for {rank}: {e}")
            return []

    def count_registered_members(self) -> int:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "SELECT COUNT(*) AS n FROM user_profiles WHERE albion_player_id IS NOT NULL"
            )
            row = self.cursor.fetchone()
            return int(row["n"]) if row else 0
        except sqlite3.Error as e:
            debug.error_log(f"Error counting registered members: {e}")
            return 0

    # ── Guild applications (recruitment) ──────────────────────────────────────

    def initialize_guild_applications_table(self):
        self.execute('''
            CREATE TABLE IF NOT EXISTS guild_applications (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id  TEXT NOT NULL,
                albion_name TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'pending',
                applied_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                reviewed_at TEXT,
                reviewed_by TEXT,
                review_note TEXT,
                message_id  TEXT
            )
        ''')
        debug.info_log("Initialized guild_applications table.")

    def insert_guild_application(self, discord_id: str, albion_name: str) -> int:
        self.execute(
            'INSERT INTO guild_applications (discord_id, albion_name) VALUES (?, ?)',
            (discord_id, albion_name)
        )
        return int(self.cursor.lastrowid or 0)

    def set_guild_application_message(self, app_id: int, message_id: str) -> None:
        self.execute(
            'UPDATE guild_applications SET message_id = ? WHERE id = ?',
            (message_id, app_id)
        )

    def fetch_guild_application(self, app_id: int):
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute('SELECT * FROM guild_applications WHERE id = ?', (app_id,))
            row = self.cursor.fetchone()
            return dict(row) if row else None
        except sqlite3.Error as e:
            debug.error_log(f"Error fetching guild application {app_id}: {e}")
            return None

    def fetch_guild_application_by_message(self, message_id: str):
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                'SELECT * FROM guild_applications WHERE message_id = ?', (message_id,)
            )
            row = self.cursor.fetchone()
            return dict(row) if row else None
        except sqlite3.Error as e:
            debug.error_log(f"Error fetching guild application by message {message_id}: {e}")
            return None

    def fetch_pending_guild_application(self, discord_id: str):
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "SELECT * FROM guild_applications WHERE discord_id = ? AND status = 'pending'",
                (discord_id,)
            )
            row = self.cursor.fetchone()
            return dict(row) if row else None
        except sqlite3.Error as e:
            debug.error_log(f"Error fetching pending guild application: {e}")
            return None

    def fetch_pending_guild_applications(self):
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "SELECT * FROM guild_applications WHERE status = 'pending' ORDER BY applied_at ASC"
            )
            return [dict(row) for row in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"Error fetching pending guild applications: {e}")
            return []

    def fetch_guild_applications_by_status(self, status: str):
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "SELECT * FROM guild_applications WHERE status = ? ORDER BY applied_at ASC",
                (status,),
            )
            return [dict(row) for row in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"Error fetching guild applications by status {status}: {e}")
            return []

    def update_guild_application_status(self, app_id: int, status: str, reviewer_id: str, note: str = None):
        self.execute('''
            UPDATE guild_applications
            SET status = ?, reviewed_at = CURRENT_TIMESTAMP, reviewed_by = ?, review_note = ?
            WHERE id = ?
        ''', (status, reviewer_id, note, app_id))

    def initialize_all_tables(self):
        self.initialize_user_profiles_table()
        self.initialize_guilds_table()
        self.initialize_guild_stats_history_table()
        self.initialize_player_stats_history_table()
        self.initialize_guild_config_table()
        self.initialize_message_archive_table()
        self.initialize_live_graphs_table()
        self.initialize_staff_applications_table()
        self.initialize_staff_role_grants_table()
        self.initialize_guild_applications_table()
        self.initialize_duties_tables()
        self.initialize_lfg_tables()
        self.initialize_discord_inventory_tables()
        self.initialize_automation_tables()
        self.initialize_bounties_table()
        self.initialize_bounty_kill_matches_table()
        self.initialize_bounty_shopping_items_table()
        self.initialize_help_tickets_table()
        self.initialize_voice_activity_table()
        self.initialize_blacklist_table()
        self.initialize_risk_watch_table()
        self.initialize_member_lifecycle_table()
        self.initialize_loadout_chest_tables()
        self.initialize_recruits_table()
        self.initialize_weekly_schedule_table()
        self.initialize_raffles_table()
        self._ensure_supplemental_indices()
        debug.info_log("Initialized all database tables.")

    # ──────────────────────────────────────────────────────────────────────
    # Supplemental indices for hot WHERE/ORDER columns that the per-table
    # init methods didn't already cover. Safe to re-run (IF NOT EXISTS).
    # ──────────────────────────────────────────────────────────────────────
    def _ensure_supplemental_indices(self) -> None:
        indices = (
            # guild_applications — looked up by status & by applicant.
            "CREATE INDEX IF NOT EXISTS idx_guild_apps_status   ON guild_applications(status)",
            "CREATE INDEX IF NOT EXISTS idx_guild_apps_discord  ON guild_applications(discord_id)",
            # staff_applications — same access patterns.
            "CREATE INDEX IF NOT EXISTS idx_staff_apps_status   ON staff_applications(status)",
            "CREATE INDEX IF NOT EXISTS idx_staff_apps_discord  ON staff_applications(discord_id)",
            # help_tickets — open-ticket queries + per-asker history.
            "CREATE INDEX IF NOT EXISTS idx_help_tickets_status ON help_tickets(status)",
            "CREATE INDEX IF NOT EXISTS idx_help_tickets_asker  ON help_tickets(asker_id)",
            "CREATE INDEX IF NOT EXISTS idx_help_tickets_claim  ON help_tickets(claimed_by)",
            # regear_requests — staff review queue + member history.
            "CREATE INDEX IF NOT EXISTS idx_regear_status       ON regear_requests(status)",
            "CREATE INDEX IF NOT EXISTS idx_regear_discord      ON regear_requests(discord_id)",
            "CREATE INDEX IF NOT EXISTS idx_regear_event        ON regear_requests(event_id)",
            # lfg_events — list-by-status, upcoming-by-start, by-creator.
            "CREATE INDEX IF NOT EXISTS idx_lfg_events_status   ON lfg_events(status)",
            "CREATE INDEX IF NOT EXISTS idx_lfg_events_starts   ON lfg_events(starts_at)",
            "CREATE INDEX IF NOT EXISTS idx_lfg_events_creator  ON lfg_events(creator_id)",
            "CREATE INDEX IF NOT EXISTS idx_lfg_events_comp     ON lfg_events(comp_id)",
            # bounties — open-board scan + per-claimer view.
            "CREATE INDEX IF NOT EXISTS idx_bounties_status     ON bounties(status)",
            "CREATE INDEX IF NOT EXISTS idx_bounties_claimed_by ON bounties(claimed_by)",
            # user_profiles — common per-guild / lifecycle scans.
            "CREATE INDEX IF NOT EXISTS idx_user_profiles_guild     ON user_profiles(guild_id)",
            "CREATE INDEX IF NOT EXISTS idx_user_profiles_lifecycle ON user_profiles(lifecycle_role)",
            # message_archive — bounded recent-message context lookups.
            "CREATE INDEX IF NOT EXISTS idx_msg_archive_channel_time ON message_archive(channel_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_msg_archive_author_time  ON message_archive(author_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_msg_archive_created      ON message_archive(created_at)",
        )
        for stmt in indices:
            try:
                self.execute(stmt, quiet=True)
            except sqlite3.Error as e:  # pragma: no cover
                debug.warning_log(f"Could not create index: {stmt} ({e})")


    # ──────────────────────────────────────────────────────────────────────
    # Automation tables: regear queue, anniversaries, fame milestones,
    # event reminders, voice attendance snapshots, SOP policy snapshots.
    # All powered by cogs/automation.py and cogs/regear.py.
    # ──────────────────────────────────────────────────────────────────────
    def initialize_automation_tables(self) -> None:
        # Regear requests submitted by members; reviewed by staff.
        self.execute('''
            CREATE TABLE IF NOT EXISTS regear_requests (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id      TEXT    NOT NULL,
                event_id        INTEGER,
                content_type    TEXT,
                gear_value      INTEGER,
                image_url       TEXT,
                notes           TEXT,
                status          TEXT    NOT NULL DEFAULT 'pending',
                review_message_id TEXT,
                review_channel_id TEXT,
                decided_by      TEXT,
                decision_notes  TEXT,
                submitted_at    TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                decided_at      TEXT
            )
        ''')
        # Migration: structured gear breakdown captured at submit-time for
        # the death-flow path. Used on approval to decrement loadout-chest
        # stock. NULL on legacy / manual regears.
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "ALTER TABLE regear_requests ADD COLUMN gear_items_json TEXT"
            )
            self.connection.commit()
        except sqlite3.OperationalError:
            pass
        # Per-(discord_id, year) flag — prevents anniversary spam.
        self.execute('''
            CREATE TABLE IF NOT EXISTS anniversaries_posted (
                discord_id  TEXT NOT NULL,
                year        INTEGER NOT NULL,
                posted_at   TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                PRIMARY KEY (discord_id, year)
            )
        ''')
        # Fame-milestone post log — dedupes per (player, metric, threshold-bucket).
        self.execute('''
            CREATE TABLE IF NOT EXISTS fame_milestones_posted (
                discord_id  TEXT NOT NULL,
                metric      TEXT NOT NULL,
                bucket      INTEGER NOT NULL,
                posted_at   TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                PRIMARY KEY (discord_id, metric, bucket)
            )
        ''')
        # Event reminder dispatch log — per (event, signup) so we DM once.
        self.execute('''
            CREATE TABLE IF NOT EXISTS event_reminders_sent (
                event_id    INTEGER NOT NULL,
                discord_id  TEXT NOT NULL,
                sent_at     TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                PRIMARY KEY (event_id, discord_id)
            )
        ''')
        # Under-filled-comp alert dispatch log — per event, so officers only
        # get pinged once per event when signups are running thin.
        self.execute('''
            CREATE TABLE IF NOT EXISTS event_underfill_alerts_sent (
                event_id    INTEGER PRIMARY KEY,
                sent_at     TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                filled      INTEGER NOT NULL DEFAULT 0,
                total       INTEGER NOT NULL DEFAULT 0
            )
        ''')
        # New-member shout-out dispatch log — one welcome post per member,
        # ever. Prevents repeat shouts if they cycle Guest→Recruit→Guest etc.
        self.execute('''
            CREATE TABLE IF NOT EXISTS member_shoutouts_sent (
                discord_id  TEXT PRIMARY KEY,
                lifecycle   TEXT,
                sent_at     TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
        ''')
        # Verification review posts — maps the officer-channel message back to
        # the applicant so the Approve/Deny buttons stay functional across
        # bot restarts (static custom_ids + message_id lookup).
        self.execute('''
            CREATE TABLE IF NOT EXISTS verification_requests (
                message_id  TEXT PRIMARY KEY,
                discord_id  TEXT NOT NULL,
                channel_id  TEXT,
                created_at  TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
        ''')
        self.execute(
            'CREATE INDEX IF NOT EXISTS ix_verif_req_discord ON verification_requests(discord_id)'
        )
        # Voice presence snapshots taken during an event window.
        self.execute('''
            CREATE TABLE IF NOT EXISTS event_voice_snapshots (
                event_id    INTEGER NOT NULL,
                discord_id  TEXT NOT NULL,
                snapshot_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
        ''')
        self.execute(
            'CREATE INDEX IF NOT EXISTS ix_evs_event ON event_voice_snapshots(event_id)'
        )
        # Whether voice-attendance reconciliation has run for an event.
        self.execute('''
            CREATE TABLE IF NOT EXISTS event_voice_reconciled (
                event_id    INTEGER PRIMARY KEY,
                reconciled_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
        ''')
        # Officer-entered loot summary for post-event reports. This is
        # deliberately separate from /loot split: entering loot here updates
        # analytics/profit-loss only and does not credit member balances.
        self.execute('''
            CREATE TABLE IF NOT EXISTS event_loot_summaries (
                event_id    INTEGER PRIMARY KEY,
                gross_loot  INTEGER NOT NULL DEFAULT 0,
                guild_cut   INTEGER NOT NULL DEFAULT 0,
                notes       TEXT,
                updated_by  TEXT,
                updated_at  TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
        ''')
        # Event reports can finish before killboard/pricing data is ready.
        # This queue lets automation retry silently and post a clean update later.
        self.execute('''
            CREATE TABLE IF NOT EXISTS event_report_pending_data (
                event_id        INTEGER PRIMARY KEY,
                reason          TEXT NOT NULL,
                attempts        INTEGER NOT NULL DEFAULT 0,
                first_seen_at   TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                last_attempt_at TEXT,
                next_retry_at   TEXT NOT NULL,
                updated_at      TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
        ''')
        self.execute(
            'CREATE INDEX IF NOT EXISTS ix_event_report_pending_due '
            'ON event_report_pending_data(next_retry_at)'
        )
        # Last successful combat/regear lookup for an event report. External
        # killboard/price APIs can be flaky, so reposts should not lose known
        # death rows and gear estimates just because the newest lookup timed out.
        self.execute('''
            CREATE TABLE IF NOT EXISTS event_report_combat_cache (
                event_id        INTEGER PRIMARY KEY,
                kills_json      TEXT NOT NULL DEFAULT '[]',
                deaths_json     TEXT NOT NULL DEFAULT '[]',
                albionbb_json   TEXT NOT NULL DEFAULT '{}',
                scanned         INTEGER NOT NULL DEFAULT 0,
                updated_at      TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
        ''')
        # SOP / policy snapshots — content_hash compared daily for drift.
        self.execute('''
            CREATE TABLE IF NOT EXISTS policy_snapshots (
                channel_id   TEXT PRIMARY KEY,
                channel_name TEXT,
                message_id   TEXT,
                content_hash TEXT NOT NULL,
                content      TEXT NOT NULL,
                snapshot_at  TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
        ''')
        # Daily snapshot of the in-game guild treasury, recorded by an officer
        # via the daily prompt or /audit treasury-record. One row per UTC date
        # (later writes for the same date overwrite earlier ones).
        self.execute('''
            CREATE TABLE IF NOT EXISTS guild_treasury_history (
                date        TEXT PRIMARY KEY,
                balance     INTEGER NOT NULL,
                recorded_by TEXT,
                note        TEXT,
                recorded_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
        ''')
        # Append-only revenue log. Each row = one income event (e.g. weekly
        # tax import, donation, market sale). Never edited; the running total
        # is just SUM(amount) over the rows.
        self.execute('''
            CREATE TABLE IF NOT EXISTS guild_revenue (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                date            TEXT NOT NULL,
                source          TEXT NOT NULL,
                amount          INTEGER NOT NULL,
                rate            INTEGER,
                recorded_by     TEXT,
                note            TEXT,
                base_amount     INTEGER,
                matched_count   INTEGER,
                unmatched_count INTEGER,
                created_at      TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
        ''')
        self.execute(
            'CREATE INDEX IF NOT EXISTS idx_guild_revenue_date '
            'ON guild_revenue (date DESC)'
        )
        # Market arbitrage watch list. One row per item id we want to scan
        # via cogs/market.py. Quality is the lowest quality to scan (we'll
        # always include quality 1 = Normal).
        self.execute('''
            CREATE TABLE IF NOT EXISTS market_watch (
                item_id     TEXT PRIMARY KEY,
                added_by    TEXT,
                added_at    TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                note        TEXT
            )
        ''')
        # Comp templates (e.g. "ZvZ Standard", "Hellgate 5v5 A"). A comp is
        # a named build template; comp_slots are the individual positions.
        self.execute('''
            CREATE TABLE IF NOT EXISTS comps (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT NOT NULL UNIQUE COLLATE NOCASE,
                content_type    TEXT,
                description     TEXT,
                created_by      TEXT NOT NULL,
                created_at      TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                archived        INTEGER NOT NULL DEFAULT 0
            )
        ''')
        # Individual slots inside a comp. Free-text item names in v1 — no
        # validation against an item enum yet. is_two_handed=1 means offhand
        # is implicitly locked even if a stray string ends up there.
        self.execute('''
            CREATE TABLE IF NOT EXISTS comp_slots (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                comp_id         INTEGER NOT NULL REFERENCES comps(id) ON DELETE CASCADE,
                slot_order      INTEGER NOT NULL,
                role            TEXT NOT NULL,
                build_type      TEXT,
                weapon          TEXT,
                is_two_handed   INTEGER NOT NULL DEFAULT 0,
                offhand         TEXT,
                head            TEXT,
                chest           TEXT,
                shoes           TEXT,
                cape            TEXT,
                mount           TEXT,
                food            TEXT,
                potion          TEXT,
                ip_min          INTEGER NOT NULL DEFAULT 0,
                required        INTEGER NOT NULL DEFAULT 1,
                notes           TEXT
            )
        ''')
        self.execute(
            'CREATE INDEX IF NOT EXISTS ix_comp_slots_comp '
            'ON comp_slots(comp_id, slot_order)'
        )
        # Migration: optional ``swaps`` column for officer-approved gear
        # alternates that fulfil the same role (e.g. "Bedrock Mace |
        # Camlann Mace; Soldier Helmet T8"). Free-text, shown verbatim in
        # the build briefing.
        try:
            cols = {
                r[1] for r in
                self.cursor.execute("PRAGMA table_info(comp_slots)").fetchall()
            }
            if "swaps" not in cols:
                self.cursor.execute("ALTER TABLE comp_slots ADD COLUMN swaps TEXT")
                self.connection.commit()
        except sqlite3.Error as e:
            debug.error_log(f"comp_slots.swaps migration error: {e}")
        # Per-event slot assignments. event_id nullable so officers can pre-
        # assign before an event row exists. UNIQUE(event_id, slot_id) keeps
        # us from double-booking a slot.
        self.execute('''
            CREATE TABLE IF NOT EXISTS comp_assignments (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id        INTEGER,
                comp_id         INTEGER NOT NULL REFERENCES comps(id) ON DELETE CASCADE,
                slot_id         INTEGER NOT NULL REFERENCES comp_slots(id) ON DELETE CASCADE,
                discord_id      TEXT NOT NULL,
                assigned_by     TEXT NOT NULL,
                assigned_at     TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                confirmed       INTEGER NOT NULL DEFAULT 0,
                UNIQUE(event_id, slot_id)
            )
        ''')
        self.execute(
            'CREATE INDEX IF NOT EXISTS ix_comp_assign_event '
            'ON comp_assignments(event_id)'
        )
        # Albion item dictionary — seeded once from ao-bin-dumps. Powers
        # autocomplete for /comp add-slot weapon/armor/etc. fields.
        self.execute('''
            CREATE TABLE IF NOT EXISTS items (
                unique_name TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                category    TEXT NOT NULL,
                tier        INTEGER NOT NULL DEFAULT 0
            )
        ''')
        self.execute(
            'CREATE INDEX IF NOT EXISTS ix_items_cat_name '
            'ON items(category, name COLLATE NOCASE)'
        )
        debug.info_log("Initialized automation tables.")

    # ── Market watch list ───────────────────────────────────────────────────

    # Default seed: refined resources T4-T8, common bags/capes/mounts/food
    # that move a lot of silver between cities. Quality 1 only.
    _MARKET_SEED = [
        # Refined resources (each city specializes in one — huge arb potential).
        "T4_PLANKS", "T5_PLANKS", "T6_PLANKS", "T7_PLANKS", "T8_PLANKS",
        "T4_METALBAR", "T5_METALBAR", "T6_METALBAR", "T7_METALBAR", "T8_METALBAR",
        "T4_LEATHER", "T5_LEATHER", "T6_LEATHER", "T7_LEATHER", "T8_LEATHER",
        "T4_CLOTH", "T5_CLOTH", "T6_CLOTH", "T7_CLOTH", "T8_CLOTH",
        "T4_STONEBLOCK", "T5_STONEBLOCK", "T6_STONEBLOCK",
        # Bags & capes — universal demand.
        "T4_BAG", "T5_BAG", "T6_BAG", "T7_BAG", "T8_BAG",
        "T4_CAPE", "T5_CAPE", "T6_CAPE", "T7_CAPE", "T8_CAPE",
        # Common consumables — players always need them.
        "T4_POTION_HEAL", "T5_POTION_HEAL", "T6_POTION_HEAL",
        "T4_POTION_STONESKIN", "T5_POTION_STONESKIN", "T6_POTION_STONESKIN",
        "T4_POTION_INVISIBILITY", "T5_POTION_INVISIBILITY",
        "T4_MEAL_OMELETTE", "T5_MEAL_OMELETTE", "T6_MEAL_OMELETTE",
        "T4_MEAL_PIE", "T5_MEAL_PIE", "T6_MEAL_PIE",
    ]

    def seed_market_watch_if_empty(self) -> int:
        """One-time seed of the watch list. Returns count inserted."""
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute("SELECT COUNT(*) AS n FROM market_watch")
            row = self.cursor.fetchone()
            if row and int(row["n"]) > 0:
                return 0
            inserted = 0
            for item_id in self._MARKET_SEED:
                try:
                    self.cursor.execute(
                        "INSERT INTO market_watch (item_id, added_by, note) "
                        "VALUES (?, ?, ?)",
                        (item_id, "seed", "default seed"),
                    )
                    inserted += 1
                except sqlite3.Error:
                    continue
            self.connection.commit()
            debug.info_log(f"Seeded market_watch with {inserted} items.")
            return inserted
        except sqlite3.Error as e:
            debug.error_log(f"seed_market_watch_if_empty error: {e}")
            return 0

    def list_market_watch(self) -> list[dict]:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "SELECT item_id, added_by, added_at, note "
                "FROM market_watch ORDER BY item_id"
            )
            return [dict(r) for r in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"list_market_watch error: {e}")
            return []

    def add_market_watch(self, item_id: str, added_by: str, note: str | None = None) -> bool:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "INSERT OR IGNORE INTO market_watch (item_id, added_by, note) "
                "VALUES (?, ?, ?)",
                (item_id.strip().upper(), added_by, note),
            )
            self.connection.commit()
            return self.cursor.rowcount > 0
        except sqlite3.Error as e:
            debug.error_log(f"add_market_watch error: {e}")
            return False

    def remove_market_watch(self, item_id: str) -> bool:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "DELETE FROM market_watch WHERE item_id = ?",
                (item_id.strip().upper(),),
            )
            self.connection.commit()
            return self.cursor.rowcount > 0
        except sqlite3.Error as e:
            debug.error_log(f"remove_market_watch error: {e}")
            return False

    # ── Comps & comp slots ──────────────────────────────────────────────────

    def create_comp(
        self, *, name: str, content_type: str | None,
        description: str | None, created_by: str,
    ) -> int:
        """Create a new comp template. Returns the new id, or 0 on conflict."""
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "INSERT INTO comps (name, content_type, description, created_by) "
                "VALUES (?, ?, ?, ?)",
                (name.strip(), content_type, description, created_by),
            )
            self.connection.commit()
            return int(self.cursor.lastrowid or 0)
        except sqlite3.IntegrityError:
            return 0
        except sqlite3.Error as e:
            debug.error_log(f"create_comp error: {e}")
            return 0

    def fetch_comp(self, name_or_id: str | int) -> dict | None:
        try:
            if not self.connection:
                self.connect()
            if isinstance(name_or_id, int) or str(name_or_id).isdigit():
                self.cursor.execute(
                    "SELECT * FROM comps WHERE id = ?", (int(name_or_id),),
                )
            else:
                self.cursor.execute(
                    "SELECT * FROM comps WHERE name = ? COLLATE NOCASE",
                    (str(name_or_id).strip(),),
                )
            row = self.cursor.fetchone()
            return dict(row) if row else None
        except sqlite3.Error as e:
            debug.error_log(f"fetch_comp error: {e}")
            return None

    def list_comps(self, *, include_archived: bool = False) -> list[dict]:
        try:
            if not self.connection:
                self.connect()
            sql = "SELECT * FROM comps"
            if not include_archived:
                sql += " WHERE archived = 0"
            sql += " ORDER BY content_type, name COLLATE NOCASE"
            self.cursor.execute(sql)
            return [dict(r) for r in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"list_comps error: {e}")
            return []

    def archive_comp(self, comp_id: int, *, archived: bool = True) -> bool:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "UPDATE comps SET archived = ? WHERE id = ?",
                (1 if archived else 0, comp_id),
            )
            self.connection.commit()
            return self.cursor.rowcount > 0
        except sqlite3.Error as e:
            debug.error_log(f"archive_comp error: {e}")
            return False

    def delete_comp(self, comp_id: int) -> bool:
        """Hard-delete a comp and (via ON DELETE CASCADE) its slots and
        assignments. Officers should usually archive instead."""
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute("DELETE FROM comps WHERE id = ?", (comp_id,))
            self.connection.commit()
            return self.cursor.rowcount > 0
        except sqlite3.Error as e:
            debug.error_log(f"delete_comp error: {e}")
            return False

    def add_comp_slot(
        self, comp_id: int, fields: dict,
    ) -> int:
        """Add a slot to a comp. ``fields`` is a dict of slot column → value;
        unknown keys are silently dropped. ``slot_order`` is auto-assigned
        if not provided."""
        allowed = {
            "slot_order", "role", "build_type", "weapon", "is_two_handed",
            "offhand", "head", "chest", "shoes", "cape", "mount",
            "food", "potion", "ip_min", "required", "notes", "swaps",
        }
        clean = {k: v for k, v in fields.items() if k in allowed}
        if "role" not in clean or not clean.get("role"):
            return 0
        try:
            if not self.connection:
                self.connect()
            if "slot_order" not in clean:
                self.cursor.execute(
                    "SELECT COALESCE(MAX(slot_order), 0) + 1 AS n "
                    "FROM comp_slots WHERE comp_id = ?",
                    (comp_id,),
                )
                row = self.cursor.fetchone()
                clean["slot_order"] = int(row["n"]) if row else 1
            cols = ["comp_id"] + list(clean.keys())
            placeholders = ", ".join(["?"] * len(cols))
            values = [comp_id] + list(clean.values())
            self.cursor.execute(
                f"INSERT INTO comp_slots ({', '.join(cols)}) VALUES ({placeholders})",
                values,
            )
            self.connection.commit()
            return int(self.cursor.lastrowid or 0)
        except sqlite3.Error as e:
            debug.error_log(f"add_comp_slot error: {e}")
            return 0

    def update_comp_slot(self, slot_id: int, fields: dict) -> bool:
        allowed = {
            "slot_order", "role", "build_type", "weapon", "is_two_handed",
            "offhand", "head", "chest", "shoes", "cape", "mount",
            "food", "potion", "ip_min", "required", "notes", "swaps",
        }
        clean = {k: v for k, v in fields.items() if k in allowed}
        if not clean:
            return False
        try:
            if not self.connection:
                self.connect()
            sets = ", ".join(f"{k} = ?" for k in clean)
            values = list(clean.values()) + [slot_id]
            self.cursor.execute(
                f"UPDATE comp_slots SET {sets} WHERE id = ?", values,
            )
            self.connection.commit()
            return self.cursor.rowcount > 0
        except sqlite3.Error as e:
            debug.error_log(f"update_comp_slot error: {e}")
            return False

    def fetch_comp_slot(self, slot_id: int) -> dict | None:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "SELECT * FROM comp_slots WHERE id = ?", (slot_id,),
            )
            row = self.cursor.fetchone()
            return dict(row) if row else None
        except sqlite3.Error as e:
            debug.error_log(f"fetch_comp_slot error: {e}")
            return None

    def list_comp_slots(self, comp_id: int) -> list[dict]:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "SELECT * FROM comp_slots WHERE comp_id = ? "
                "ORDER BY slot_order, id",
                (comp_id,),
            )
            return [dict(r) for r in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"list_comp_slots error: {e}")
            return []

    def remove_comp_slot(self, slot_id: int) -> bool:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute("DELETE FROM comp_slots WHERE id = ?", (slot_id,))
            self.connection.commit()
            return self.cursor.rowcount > 0
        except sqlite3.Error as e:
            debug.error_log(f"remove_comp_slot error: {e}")
            return False

    def duplicate_comp(
        self, source_comp_id: int, new_name: str, created_by: str,
    ) -> int:
        """Copy a comp and all of its slots under ``new_name``. Returns the
        new comp id, or 0 on failure / name conflict."""
        src = self.fetch_comp(source_comp_id)
        if not src:
            return 0
        new_id = self.create_comp(
            name=new_name,
            content_type=src.get("content_type"),
            description=src.get("description"),
            created_by=created_by,
        )
        if not new_id:
            return 0
        for slot in self.list_comp_slots(source_comp_id):
            payload = {k: v for k, v in slot.items() if k not in ("id", "comp_id")}
            self.add_comp_slot(new_id, payload)
        return new_id


    # ── Albion item dictionary (for autocomplete) ───────────────────────────

    # UniqueName prefix → category code stored in the items table.
    _ITEM_CATEGORY_PATTERNS = (
        ("2H",     re.compile(r"^T(\d+)_2H_")),
        ("MAIN",   re.compile(r"^T(\d+)_MAIN_")),
        ("OFF",    re.compile(r"^T(\d+)_OFF_")),
        ("HEAD",   re.compile(r"^T(\d+)_HEAD_")),
        ("ARMOR",  re.compile(r"^T(\d+)_ARMOR_")),
        ("SHOES",  re.compile(r"^T(\d+)_SHOES_")),
        ("CAPE",   re.compile(r"^T(\d+)_CAPE")),
        ("MOUNT",  re.compile(r"^T(\d+)_MOUNT_")),
        ("MEAL",   re.compile(r"^T(\d+)_MEAL_")),
        ("POTION", re.compile(r"^T(\d+)_POTION_")),
    )

    def count_items(self) -> int:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute("SELECT COUNT(*) FROM items")
            row = self.cursor.fetchone()
            return int(row[0]) if row else 0
        except sqlite3.Error as e:
            debug.error_log(f"count_items error: {e}")
            return 0

    def seed_items_from_url(
        self,
        url: str = "https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/formatted/items.json",
        *,
        force: bool = False,
    ) -> int:
        """Download the ao-bin-dumps items.json and load gear/consumable
        entries into the items table. Skips the @N enchanted duplicates —
        autocomplete only needs base names. Returns the row count inserted.

        Safe to call from a worker thread (e.g. ``asyncio.to_thread``): we
        open a dedicated short-lived sqlite connection here rather than
        reusing ``self.connection``, which is bound to the main thread.
        """
        try:
            import json
            import urllib.request

            with urllib.request.urlopen(url, timeout=30) as r:
                payload = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            debug.error_log(f"seed_items_from_url download error: {e}")
            return 0

        rows: dict[str, tuple[str, str, int]] = {}
        tier_re = re.compile(r"^T(\d+)_")
        for entry in payload:
            unique = str(entry.get("UniqueName") or "")
            if "@" in unique:
                continue  # enchantment variant — same base name
            base = unique
            cat: str | None = None
            tier = 0
            for code, pattern in self._ITEM_CATEGORY_PATTERNS:
                m = pattern.match(base)
                if m:
                    cat = code
                    tier = int(m.group(1))
                    break
            # Items not matching the gear/consumable patterns (bags,
            # artifacts, materials, resources, journals, etc.) are still
            # indexed under category=OTHER so market autocomplete can reach
            # them. Per-slot autocomplete in /comp add-slot keeps using the
            # specific category codes.
            if not cat:
                cat = "OTHER"
                m2 = tier_re.match(base)
                if m2:
                    tier = int(m2.group(1))
            names = entry.get("LocalizedNames") or {}
            if not isinstance(names, dict):
                continue
            en = names.get("EN-US")
            if not en:
                continue
            rows[base] = (str(en), cat, tier)

        if not rows:
            return 0
        # Use a dedicated connection so this works from worker threads.
        try:
            conn = sqlite3.connect(self.database_path, timeout=30)
            try:
                conn.execute("PRAGMA busy_timeout=5000")
                if not force:
                    cur = conn.execute("SELECT COUNT(*) FROM items")
                    existing = int((cur.fetchone() or [0])[0])
                    if existing > 0:
                        return 0
                else:
                    conn.execute("DELETE FROM items")
                conn.executemany(
                    "INSERT OR REPLACE INTO items "
                    "(unique_name, name, category, tier) VALUES (?, ?, ?, ?)",
                    [(u, n, c, t) for u, (n, c, t) in rows.items()],
                )
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as e:
            debug.error_log(f"seed_items_from_url insert error: {e}")
            return 0
        debug.info_log(f"Seeded {len(rows)} Albion items.")
        return len(rows)

    def search_items(
        self, query: str, *, categories: list[str] | None = None,
        limit: int = 25,
    ) -> list[dict]:
        """Substring search across item names *and* unique_names.

        Improvements over a plain LIKE:
        * Multi-word queries require **every** word to match (AND), so
          ``riding horse`` doesn't get drowned by every horse skin.
        * Exact name → name prefix → unique_name prefix rank highest.
        * Cosmetic/skin entries (``UNIQUE_*``) and zero-tier rows are
          pushed to the bottom so real craftable items surface first.
        """
        q = (query or "").strip()
        if not q:
            sql = "SELECT unique_name, name, category, tier FROM items"
            params: list = []
            if categories:
                placeholders = ",".join(["?"] * len(categories))
                sql += f" WHERE category IN ({placeholders})"
                params.extend(categories)
            sql += (
                " ORDER BY CASE WHEN unique_name LIKE 'UNIQUE_%' THEN 1 "
                " ELSE 0 END, name COLLATE NOCASE LIMIT ?"
            )
            params.append(int(limit))
        else:
            tokens = [t for t in q.split() if t]
            # Every token must be present in name OR unique_name.
            where_clauses: list[str] = []
            params = []
            for tok in tokens:
                where_clauses.append(
                    "(name LIKE ? COLLATE NOCASE "
                    " OR unique_name LIKE ? COLLATE NOCASE)"
                )
                params.extend([f"%{tok}%", f"%{tok}%"])
            sql = (
                "SELECT unique_name, name, category, tier FROM items "
                "WHERE " + " AND ".join(where_clauses)
            )
            if categories:
                placeholders = ",".join(["?"] * len(categories))
                sql += f" AND category IN ({placeholders})"
                params.extend(categories)
            # Ranking buckets (lower = better):
            #   0 exact name match
            #   1 name starts with full query
            #   2 unique_name starts with full query
            #   3 name contains full query
            #   4 multi-word AND match
            # Plus +5 if it's a UNIQUE_ skin/cosmetic, +5 if tier=0.
            sql += (
                " ORDER BY ("
                "  CASE WHEN name = ? COLLATE NOCASE THEN 0"
                "       WHEN name LIKE ? COLLATE NOCASE THEN 1"
                "       WHEN unique_name LIKE ? COLLATE NOCASE THEN 2"
                "       WHEN name LIKE ? COLLATE NOCASE THEN 3"
                "       ELSE 4 END"
                "  + CASE WHEN unique_name LIKE 'UNIQUE_%' THEN 5 ELSE 0 END"
                "  + CASE WHEN tier = 0 THEN 5 ELSE 0 END"
                " ), tier, name COLLATE NOCASE LIMIT ?"
            )
            params.append(q)            # exact
            params.append(f"{q}%")      # name prefix
            params.append(f"{q}%")      # unique_name prefix
            params.append(f"%{q}%")     # name contains
            params.append(int(limit))
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(sql, params)
            cols = [d[0] for d in self.cursor.description]
            return [dict(zip(cols, row)) for row in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"search_items error: {e}")
            return []


    # ── Regear queue ────────────────────────────────────────────────────────

    def create_regear_request(
        self, *, discord_id: str, event_id: int | None,
        content_type: str | None, gear_value: int | None,
        image_url: str | None, notes: str | None,
        gear_items_json: str | None = None,
    ) -> int:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                '''INSERT INTO regear_requests
                   (discord_id, event_id, content_type, gear_value,
                    image_url, notes, gear_items_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (discord_id, event_id, content_type, gear_value,
                 image_url, notes, gear_items_json),
            )
            self.connection.commit()
            return int(self.cursor.lastrowid or 0)
        except sqlite3.Error as e:
            debug.error_log(f"create_regear_request error: {e}")
            return 0

    def fetch_regear_request(self, request_id: int) -> dict | None:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "SELECT * FROM regear_requests WHERE id = ?", (request_id,),
            )
            row = self.cursor.fetchone()
            return dict(row) if row else None
        except sqlite3.Error as e:
            debug.error_log(f"fetch_regear_request error: {e}")
            return None

    def fetch_regear_request_for_death(
        self,
        discord_id: str,
        killboard_event_id: int | str,
    ) -> dict | None:
        """Return an existing regear tied to a specific killboard death.

        ``regear_requests.event_id`` stores the Albion killboard event id for
        death-sourced requests. Used by event reports to avoid creating the
        same automatic regear task twice.
        """
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                """
                SELECT * FROM regear_requests
                WHERE discord_id = ? AND event_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (str(discord_id), int(killboard_event_id or 0)),
            )
            row = self.cursor.fetchone()
            return dict(row) if row else None
        except (sqlite3.Error, TypeError, ValueError) as e:
            debug.error_log(f"fetch_regear_request_for_death error: {e}")
            return None

    def set_regear_review_message(
        self, request_id: int, channel_id: str, message_id: str,
    ) -> None:
        self.execute(
            "UPDATE regear_requests SET review_channel_id = ?, review_message_id = ? "
            "WHERE id = ?",
            (channel_id, message_id, request_id),
        )

    def resolve_regear_request(
        self, request_id: int, *, status: str, decided_by: str,
        decision_notes: str | None = None,
    ) -> None:
        self.execute(
            "UPDATE regear_requests SET status = ?, decided_by = ?, "
            "decision_notes = ?, decided_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (status, decided_by, decision_notes, request_id),
        )

    # ── Anniversaries ───────────────────────────────────────────────────────

    def has_anniversary_posted(self, discord_id: str, year: int) -> bool:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "SELECT 1 FROM anniversaries_posted WHERE discord_id = ? AND year = ?",
                (discord_id, year),
            )
            return self.cursor.fetchone() is not None
        except sqlite3.Error as e:
            debug.error_log(f"has_anniversary_posted error: {e}")
            return True  # fail-closed: don't double-post on DB error

    def mark_anniversary_posted(self, discord_id: str, year: int) -> None:
        self.execute(
            "INSERT OR IGNORE INTO anniversaries_posted (discord_id, year) VALUES (?, ?)",
            (discord_id, year),
        )

    # ── Fame milestones ────────────────────────────────────────────────────

    def has_milestone_posted(
        self, discord_id: str, metric: str, bucket: int,
    ) -> bool:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "SELECT 1 FROM fame_milestones_posted "
                "WHERE discord_id = ? AND metric = ? AND bucket = ?",
                (discord_id, metric, bucket),
            )
            return self.cursor.fetchone() is not None
        except sqlite3.Error as e:
            debug.error_log(f"has_milestone_posted error: {e}")
            return True

    def mark_milestone_posted(
        self, discord_id: str, metric: str, bucket: int,
    ) -> None:
        self.execute(
            "INSERT OR IGNORE INTO fame_milestones_posted "
            "(discord_id, metric, bucket) VALUES (?, ?, ?)",
            (discord_id, metric, bucket),
        )

    # ── Event reminders ─────────────────────────────────────────────────────

    def fetch_upcoming_events(
        self, lower_iso: str, upper_iso: str,
    ) -> list[dict]:
        """Open events whose starts_at is in (lower, upper]."""
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "SELECT * FROM lfg_events WHERE status = 'open' "
                "AND starts_at > ? AND starts_at <= ? ORDER BY starts_at",
                (lower_iso, upper_iso),
            )
            return [dict(r) for r in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"fetch_upcoming_events error: {e}")
            return []

    def has_reminder_been_sent(self, event_id: int, discord_id: str) -> bool:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "SELECT 1 FROM event_reminders_sent WHERE event_id = ? AND discord_id = ?",
                (event_id, discord_id),
            )
            return self.cursor.fetchone() is not None
        except sqlite3.Error as e:
            debug.error_log(f"has_reminder_been_sent error: {e}")
            return True

    def mark_reminder_sent(self, event_id: int, discord_id: str) -> None:
        self.execute(
            "INSERT OR IGNORE INTO event_reminders_sent (event_id, discord_id) VALUES (?, ?)",
            (event_id, discord_id),
        )

    def has_underfill_alert_been_sent(self, event_id: int) -> bool:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "SELECT 1 FROM event_underfill_alerts_sent WHERE event_id = ?",
                (event_id,),
            )
            return self.cursor.fetchone() is not None
        except sqlite3.Error as e:
            debug.error_log(f"has_underfill_alert_been_sent error: {e}")
            return True  # fail closed — don't re-spam on DB errors

    def mark_underfill_alert_sent(
        self, event_id: int, filled: int, total: int,
    ) -> None:
        self.execute(
            "INSERT OR IGNORE INTO event_underfill_alerts_sent "
            "(event_id, filled, total) VALUES (?, ?, ?)",
            (event_id, int(filled), int(total)),
        )

    def has_member_shoutout_been_sent(self, discord_id: str) -> bool:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "SELECT 1 FROM member_shoutouts_sent WHERE discord_id = ?",
                (str(discord_id),),
            )
            return self.cursor.fetchone() is not None
        except sqlite3.Error as e:
            debug.error_log(f"has_member_shoutout_been_sent error: {e}")
            return True  # fail closed — don't double-post

    def mark_member_shoutout_sent(
        self, discord_id: str, lifecycle: str | None,
    ) -> None:
        self.execute(
            "INSERT OR IGNORE INTO member_shoutouts_sent "
            "(discord_id, lifecycle) VALUES (?, ?)",
            (str(discord_id), lifecycle),
        )

    # Verification-review message ↔ applicant mapping (restart-safe buttons).
    def record_verification_request(
        self, message_id: int | str, discord_id: str, channel_id: int | str | None = None,
    ) -> None:
        self.execute(
            "INSERT OR REPLACE INTO verification_requests "
            "(message_id, discord_id, channel_id) VALUES (?, ?, ?)",
            (str(message_id), str(discord_id),
             str(channel_id) if channel_id is not None else None),
        )

    def fetch_verification_request(self, message_id: int | str) -> dict | None:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "SELECT message_id, discord_id, channel_id, created_at "
                "FROM verification_requests WHERE message_id = ?",
                (str(message_id),),
            )
            row = self.cursor.fetchone()
            return dict(row) if row else None
        except sqlite3.Error as e:
            debug.error_log(f"fetch_verification_request error: {e}")
            return None

    def delete_verification_request(self, message_id: int | str) -> None:
        self.execute(
            "DELETE FROM verification_requests WHERE message_id = ?",
            (str(message_id),),
        )

    def fetch_active_event_window(self) -> list[dict]:
        """Events whose voice attendance should be snapshotted.

        The scheduled end time is only an estimate. If an event has a temporary
        event VC, keep snapshotting until that VC is deleted so long-running
        content still counts for attendance/regear analytics.
        """
        try:
            if not self.connection:
                self.connect()
            now_iso = _datetime.datetime.now(_datetime.timezone.utc).isoformat()
            self.cursor.execute(
                """
                SELECT e.* FROM lfg_events e
                LEFT JOIN event_voice_reconciled r ON r.event_id = e.id
                WHERE e.status != 'cancelled'
                  AND r.event_id IS NULL
                  AND datetime(e.starts_at, '-' || COALESCE(e.prep_minutes, 30) || ' minutes')
                      <= datetime(?)
                  AND (
                        datetime(e.ends_at) >= datetime(?)
                        OR (
                            e.voice_channel_id IS NOT NULL
                            AND e.voice_channel_id != ''
                            AND e.voice_channel_deleted_at IS NULL
                        )
                  )
                """,
                (now_iso, now_iso),
            )
            return [dict(r) for r in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"fetch_active_event_window error: {e}")
            return []

    def fetch_events_needing_reconciliation(
        self,
        fallback_grace_minutes: int = 30,
    ) -> list[dict]:
        """Ended events ready for voice-attendance reconciliation.

        Events with temporary event VCs wait until the VC is deleted. That keeps
        reports from firing while members are still doing content past the
        scheduled end time. Legacy/no-VC events reconcile after the review
        window plus a small grace period.
        """
        try:
            if not self.connection:
                self.connect()
            now_iso = _datetime.datetime.now(_datetime.timezone.utc).isoformat()
            grace = max(0, int(fallback_grace_minutes or 0))
            self.cursor.execute('''
                SELECT e.* FROM lfg_events e
                LEFT JOIN event_voice_reconciled r ON r.event_id = e.id
                WHERE e.status != 'cancelled'
                  AND datetime(e.ends_at) <= datetime(?)
                  AND r.event_id IS NULL
                  AND (
                        (
                            e.voice_channel_id IS NOT NULL
                            AND e.voice_channel_id != ''
                            AND e.voice_channel_deleted_at IS NOT NULL
                        )
                        OR (
                            (e.voice_channel_id IS NULL OR e.voice_channel_id = '')
                            AND datetime(
                                e.ends_at,
                                '+' || (COALESCE(e.review_minutes, 15) + ?) || ' minutes'
                            ) <= datetime(?)
                        )
                  )
                ORDER BY e.ends_at ASC
                LIMIT 25
            ''', (now_iso, grace, now_iso))
            return [dict(r) for r in self.cursor.fetchall()]
        except (sqlite3.Error, TypeError, ValueError) as e:
            debug.error_log(f"fetch_events_needing_reconciliation error: {e}")
            return []

    def archive_completed_events(self) -> int:
        """Mark non-cancelled past events as 'completed'. Returns # updated."""
        try:
            if not self.connection:
                self.connect()
            now_iso = _datetime.datetime.now(_datetime.timezone.utc).isoformat()
            self.cursor.execute(
                "UPDATE lfg_events SET status = 'completed' "
                "WHERE status = 'open' AND ends_at < ?",
                (now_iso,),
            )
            n = self.cursor.rowcount
            self.connection.commit()
            return int(n or 0)
        except sqlite3.Error as e:
            debug.error_log(f"archive_completed_events error: {e}")
            return 0

    # ── Voice attendance ────────────────────────────────────────────────────

    def record_voice_snapshot(self, event_id: int, discord_ids: list[str]) -> None:
        if not discord_ids:
            return
        try:
            if not self.connection:
                self.connect()
            self.cursor.executemany(
                "INSERT INTO event_voice_snapshots (event_id, discord_id) VALUES (?, ?)",
                [(event_id, did) for did in discord_ids],
            )
            self.connection.commit()
        except sqlite3.Error as e:
            debug.error_log(f"record_voice_snapshot error: {e}")

    def fetch_voice_snapshot_summary(self, event_id: int) -> dict[str, int]:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "SELECT discord_id, COUNT(*) AS n FROM event_voice_snapshots "
                "WHERE event_id = ? GROUP BY discord_id",
                (event_id,),
            )
            return {r["discord_id"]: int(r["n"]) for r in self.cursor.fetchall()}
        except sqlite3.Error as e:
            debug.error_log(f"fetch_voice_snapshot_summary error: {e}")
            return {}

    def fetch_voice_snapshot_flow(self, event_id: int) -> list[dict]:
        """Return per-snapshot VC population for an event.

        Each tracker tick inserts one row per member present in the event voice
        channel. Grouping by the timestamp gives the officer report a retention
        curve instead of only a final attendance count.
        """
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                """
                SELECT snapshot_at, COUNT(DISTINCT discord_id) AS members
                  FROM event_voice_snapshots
                 WHERE event_id = ?
                 GROUP BY snapshot_at
                 ORDER BY datetime(snapshot_at) ASC
                """,
                (event_id,),
            )
            return [dict(r) for r in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"fetch_voice_snapshot_flow error: {e}")
            return []

    def mark_event_reconciled(self, event_id: int) -> None:
        self.execute(
            "INSERT OR IGNORE INTO event_voice_reconciled (event_id) VALUES (?)",
            (event_id,),
        )

    def upsert_event_loot_summary(
        self,
        event_id: int,
        *,
        gross_loot: int,
        guild_cut: int = 0,
        notes: str | None = None,
        updated_by: str | None = None,
    ) -> None:
        """Store officer-entered loot value for an event report.

        ``gross_loot`` is the estimated/sold value brought home. ``guild_cut``
        is any amount held back by the guild before member distribution. This
        is analytics-only; actual balance credits still go through /loot split.
        """
        self.execute(
            """
            INSERT INTO event_loot_summaries
                (event_id, gross_loot, guild_cut, notes, updated_by, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(event_id) DO UPDATE SET
                gross_loot = excluded.gross_loot,
                guild_cut = excluded.guild_cut,
                notes = excluded.notes,
                updated_by = excluded.updated_by,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                int(event_id),
                max(0, int(gross_loot or 0)),
                max(0, int(guild_cut or 0)),
                notes,
                updated_by,
            ),
        )

    def fetch_event_loot_summary(self, event_id: int) -> dict | None:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "SELECT * FROM event_loot_summaries WHERE event_id = ?",
                (int(event_id),),
            )
            row = self.cursor.fetchone()
            return dict(row) if row else None
        except sqlite3.Error as e:
            debug.error_log(f"fetch_event_loot_summary error: {e}")
            return None

    def fetch_event_report_pending_data(self, event_id: int) -> dict | None:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "SELECT * FROM event_report_pending_data WHERE event_id = ?",
                (int(event_id),),
            )
            row = self.cursor.fetchone()
            return dict(row) if row else None
        except sqlite3.Error as e:
            debug.error_log(f"fetch_event_report_pending_data error: {e}")
            return None

    def upsert_event_report_pending_data(
        self,
        event_id: int,
        *,
        reason: str,
        next_retry_at: str,
    ) -> None:
        self.execute(
            """
            INSERT INTO event_report_pending_data
                (event_id, reason, attempts, last_attempt_at, next_retry_at, updated_at)
            VALUES (?, ?, 1, CURRENT_TIMESTAMP, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(event_id) DO UPDATE SET
                reason = excluded.reason,
                attempts = attempts + 1,
                last_attempt_at = CURRENT_TIMESTAMP,
                next_retry_at = excluded.next_retry_at,
                updated_at = CURRENT_TIMESTAMP
            """,
            (int(event_id), str(reason or "data pending"), str(next_retry_at)),
        )

    def fetch_due_event_report_pending_data(
        self,
        now_iso: str,
        *,
        limit: int = 3,
    ) -> list[dict]:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                """
                SELECT *
                  FROM event_report_pending_data
                 WHERE next_retry_at <= ?
                 ORDER BY next_retry_at ASC
                 LIMIT ?
                """,
                (str(now_iso), max(1, int(limit))),
            )
            return [dict(r) for r in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"fetch_due_event_report_pending_data error: {e}")
            return []

    def clear_event_report_pending_data(self, event_id: int) -> None:
        self.execute(
            "DELETE FROM event_report_pending_data WHERE event_id = ?",
            (int(event_id),),
        )

    def upsert_event_report_combat_cache(
        self,
        event_id: int,
        *,
        kills_json: str,
        deaths_json: str,
        albionbb_json: str = "{}",
        scanned: int = 0,
    ) -> None:
        self.execute(
            """
            INSERT INTO event_report_combat_cache
                (event_id, kills_json, deaths_json, albionbb_json, scanned, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(event_id) DO UPDATE SET
                kills_json = excluded.kills_json,
                deaths_json = excluded.deaths_json,
                albionbb_json = excluded.albionbb_json,
                scanned = excluded.scanned,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                int(event_id),
                str(kills_json or "[]"),
                str(deaths_json or "[]"),
                str(albionbb_json or "{}"),
                max(0, int(scanned or 0)),
            ),
        )

    def fetch_event_report_combat_cache(self, event_id: int) -> dict | None:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "SELECT * FROM event_report_combat_cache WHERE event_id = ?",
                (int(event_id),),
            )
            row = self.cursor.fetchone()
            return dict(row) if row else None
        except sqlite3.Error as e:
            debug.error_log(f"fetch_event_report_combat_cache error: {e}")
            return None

    # ── Policy snapshots (SOP drift) ───────────────────────────────────────

    def upsert_policy_snapshot(
        self, *, channel_id: str, channel_name: str, message_id: str,
        content: str, content_hash: str,
    ) -> None:
        self.execute(
            "INSERT OR REPLACE INTO policy_snapshots "
            "(channel_id, channel_name, message_id, content_hash, content, snapshot_at) "
            "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (channel_id, channel_name, message_id, content_hash, content),
        )

    def fetch_all_policy_snapshots(self) -> list[dict]:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute("SELECT * FROM policy_snapshots")
            return [dict(r) for r in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"fetch_all_policy_snapshots error: {e}")
            return []

    def delete_policy_snapshot(self, channel_id: str) -> None:
        self.execute(
            "DELETE FROM policy_snapshots WHERE channel_id = ?", (channel_id,),
        )

    # ── Anti-poach helper: orphan guilds ───────────────────────────────────

    def delete_orphan_guilds(self) -> int:
        """Delete `guilds` rows that no registered profile points at via guild_name.
        Skips the configured home_guild_name. Returns # deleted."""
        try:
            if not self.connection:
                self.connect()
            home = (self.get_config("home_guild_name") or "").strip()
            self.cursor.execute('''
                DELETE FROM guilds
                WHERE guild_name != COALESCE(?, '')
                  AND guild_name NOT IN (
                      SELECT DISTINCT guild_name FROM user_profiles
                      WHERE guild_name IS NOT NULL AND guild_name != ''
                  )
            ''', (home,))
            n = self.cursor.rowcount
            self.connection.commit()
            return int(n or 0)
        except sqlite3.Error as e:
            debug.error_log(f"delete_orphan_guilds error: {e}")
            return 0

    # ── Inactivity helpers ─────────────────────────────────────────────────

    def fetch_inactive_profiles(
        self, threshold_iso: str, *, home_guild: str | None = None,
    ) -> list[dict]:
        """Registered, in-home-guild profiles whose last_activity_date is null
        or older than ``threshold_iso``."""
        try:
            if not self.connection:
                self.connect()
            params: list = [threshold_iso]
            sql = '''SELECT discord_id, albion_name, last_activity_date,
                            verified_date, guild_name, lifecycle_role
                     FROM user_profiles
                     WHERE pending_verification = 0
                       AND (last_activity_date IS NULL OR last_activity_date < ?)'''
            if home_guild:
                sql += " AND LOWER(guild_name) = LOWER(?)"
                params.append(home_guild)
            sql += " ORDER BY (last_activity_date IS NULL) DESC, last_activity_date ASC"
            self.cursor.execute(sql, params)
            return [dict(r) for r in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"fetch_inactive_profiles error: {e}")
            return []

    def fetch_inactivity_nudge_targets(
        self,
        *,
        idle_low_iso: str,
        idle_high_iso: str,
        cooldown_iso: str,
        home_guild: str | None = None,
    ) -> list[dict]:
        """Members whose last_activity_date sits between ``idle_low_iso`` and
        ``idle_high_iso`` (so they're approaching the inactivity threshold but
        not yet past it) AND who haven't been nudged since ``cooldown_iso``.

        Skips alumni / inactive / guest lifecycles — only nudges members we
        still consider active.
        """
        try:
            if not self.connection:
                self.connect()
            params: list = [idle_low_iso, idle_high_iso, cooldown_iso]
            sql = (
                "SELECT discord_id, albion_name, last_activity_date, "
                "       lifecycle_role, inactivity_nudge_sent_date "
                "  FROM user_profiles "
                " WHERE pending_verification = 0 "
                "   AND last_activity_date IS NOT NULL "
                "   AND last_activity_date >= ? "
                "   AND last_activity_date <  ? "
                "   AND (inactivity_nudge_sent_date IS NULL "
                "        OR inactivity_nudge_sent_date < ?) "
                "   AND (lifecycle_role IS NULL "
                "        OR lifecycle_role NOT IN ('Alumni','Inactive','Guest'))"
                "   AND (loa_until IS NULL OR loa_until = '' "
                "        OR loa_until < date('now'))"
            )
            if home_guild:
                sql += " AND LOWER(guild_name) = LOWER(?)"
                params.append(home_guild)
            sql += " ORDER BY last_activity_date ASC"
            self.cursor.execute(sql, params)
            return [dict(r) for r in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"fetch_inactivity_nudge_targets error: {e}")
            return []

    def mark_inactivity_nudge_sent(self, discord_id: str, today_iso: str) -> None:
        """Record that a nudge DM was sent today, so cooldown gating works."""
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "UPDATE user_profiles SET inactivity_nudge_sent_date = ? "
                " WHERE discord_id = ?",
                (today_iso, str(discord_id)),
            )
            self.connection.commit()
        except sqlite3.Error as e:
            debug.error_log(f"mark_inactivity_nudge_sent error: {e}")

    def fetch_unverified_nudge_targets(
        self,
        *,
        joined_before_iso: str,
        cooldown_iso: str,
        max_count: int,
    ) -> list[dict]:
        """Return basic profile rows for unregistered users who are due for an
        Unverified registration reminder.

        Discord role membership is still checked live by the automation cog;
        this query only handles persisted age/cooldown/count gates.
        """
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                """
                SELECT discord_id, username, join_date,
                       unverified_nudge_sent_date, unverified_nudge_count
                  FROM user_profiles
                 WHERE albion_player_id IS NULL
                   AND pending_verification = 0
                   AND join_date IS NOT NULL
                   AND join_date <= ?
                   AND COALESCE(unverified_nudge_count, 0) < ?
                   AND (unverified_nudge_sent_date IS NULL
                        OR unverified_nudge_sent_date < ?)
                 ORDER BY join_date ASC
                """,
                (joined_before_iso, int(max_count), cooldown_iso),
            )
            return [dict(r) for r in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"fetch_unverified_nudge_targets error: {e}")
            return []

    def mark_unverified_nudge_sent(self, discord_id: str, today_iso: str) -> None:
        """Record a registration nudge attempt so cooldown/max-count gates work."""
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                """
                UPDATE user_profiles
                   SET unverified_nudge_sent_date = ?,
                       unverified_nudge_count = COALESCE(unverified_nudge_count, 0) + 1
                 WHERE discord_id = ?
                """,
                (today_iso, str(discord_id)),
            )
            self.connection.commit()
        except sqlite3.Error as e:
            debug.error_log(f"mark_unverified_nudge_sent error: {e}")

    # ── Leave of Absence ───────────────────────────────────────────────────

    def set_timezone(self, discord_id: str, tz_name: str | None) -> None:
        """Store an IANA timezone (e.g. 'America/Chicago') on a profile.
        Pass ``None`` or empty string to clear."""
        try:
            if not self.connection:
                self.connect()
            value = (tz_name or "").strip() or None
            self.cursor.execute(
                "UPDATE user_profiles SET timezone = ? WHERE discord_id = ?",
                (value, str(discord_id)),
            )
            self.connection.commit()
        except sqlite3.Error as e:
            debug.error_log(f"set_timezone error: {e}")

    def set_loa(
        self, discord_id: str, until_iso: str, reason: str | None = None,
    ) -> None:
        """Place a member on Leave of Absence through ``until_iso`` (YYYY-MM-DD).

        While LOA is active, the nightly nudge loop skips them and the
        lifecycle auto-promote step does NOT demote them to Inactive.
        """
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "UPDATE user_profiles SET loa_until = ?, loa_reason = ? "
                " WHERE discord_id = ?",
                (until_iso, (reason or "").strip() or None, str(discord_id)),
            )
            self.connection.commit()
        except sqlite3.Error as e:
            debug.error_log(f"set_loa error: {e}")

    def clear_loa(self, discord_id: str) -> None:
        """End a LOA immediately."""
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "UPDATE user_profiles SET loa_until = NULL, loa_reason = NULL "
                " WHERE discord_id = ?",
                (str(discord_id),),
            )
            self.connection.commit()
        except sqlite3.Error as e:
            debug.error_log(f"clear_loa error: {e}")

    def fetch_active_loa(self) -> list[dict]:
        """All profiles with an unexpired LOA, soonest-return first."""
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "SELECT discord_id, albion_name, loa_until, loa_reason, "
                "       lifecycle_role "
                "  FROM user_profiles "
                " WHERE loa_until IS NOT NULL AND loa_until != '' "
                "   AND loa_until >= date('now') "
                " ORDER BY loa_until ASC"
            )
            return [dict(r) for r in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"fetch_active_loa error: {e}")
            return []

    def is_on_loa(self, discord_id: str) -> bool:
        """True if this member has an active (unexpired) LOA."""
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "SELECT 1 FROM user_profiles "
                " WHERE discord_id = ? "
                "   AND loa_until IS NOT NULL AND loa_until != '' "
                "   AND loa_until >= date('now')",
                (str(discord_id),),
            )
            return self.cursor.fetchone() is not None
        except sqlite3.Error as e:
            debug.error_log(f"is_on_loa error: {e}")
            return False

    # ── Recruitment funnel ─────────────────────────────────────────────────

    def fetch_recruitment_funnel(self) -> dict[str, int]:
        """Counts for each stage of the recruitment funnel."""
        try:
            if not self.connection:
                self.connect()
            home = (self.get_config("home_guild_name") or "").strip()
            self.cursor.execute("SELECT COUNT(*) AS n FROM user_profiles")
            in_discord = int((self.cursor.fetchone() or {"n": 0})["n"] or 0)
            self.cursor.execute(
                "SELECT COUNT(*) AS n FROM user_profiles WHERE albion_player_id IS NOT NULL"
            )
            registered = int((self.cursor.fetchone() or {"n": 0})["n"] or 0)
            self.cursor.execute(
                "SELECT COUNT(*) AS n FROM user_profiles WHERE verified_date IS NOT NULL"
            )
            verified = int((self.cursor.fetchone() or {"n": 0})["n"] or 0)
            in_guild = 0
            if home:
                self.cursor.execute(
                    "SELECT COUNT(*) AS n FROM user_profiles "
                    "WHERE LOWER(guild_name) = LOWER(?)",
                    (home,),
                )
                in_guild = int((self.cursor.fetchone() or {"n": 0})["n"] or 0)
            self.cursor.execute(
                "SELECT COUNT(*) AS n FROM user_profiles "
                "WHERE last_activity_date >= datetime('now', '-30 days')"
            )
            active_30d = int((self.cursor.fetchone() or {"n": 0})["n"] or 0)
            return {
                "discord_members": in_discord,
                "registered":      registered,
                "verified":        verified,
                "in_home_guild":   in_guild,
                "active_30d":      active_30d,
            }
        except sqlite3.Error as e:
            debug.error_log(f"fetch_recruitment_funnel error: {e}")
            return {}

    # ──────────────────────────────────────────────────────────────────────
    # Discord inventory: cached snapshot of the live guild's roles + channels.
    # Populated by cogs/admin (or events) on startup so any cog (LFG, etc.)
    # can read the current Discord layout from the DB without making API calls.
    # ──────────────────────────────────────────────────────────────────────
    def initialize_discord_inventory_tables(self) -> None:
        self.execute('''
            CREATE TABLE IF NOT EXISTS discord_roles (
                role_id      TEXT PRIMARY KEY,
                guild_id     TEXT NOT NULL,
                name         TEXT NOT NULL,
                color        INTEGER NOT NULL DEFAULT 0,
                position     INTEGER NOT NULL DEFAULT 0,
                hoist        INTEGER NOT NULL DEFAULT 0,
                mentionable  INTEGER NOT NULL DEFAULT 0,
                managed      INTEGER NOT NULL DEFAULT 0,
                is_default   INTEGER NOT NULL DEFAULT 0,
                member_count INTEGER NOT NULL DEFAULT 0,
                permissions  INTEGER NOT NULL DEFAULT 0,
                last_synced  TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
        ''')
        # Best-effort migration for pre-existing rows from older schema.
        # ALTER TABLE ... ADD COLUMN is idempotent only via try/except in SQLite.
        # Use quiet=True so the expected "duplicate column" error doesn't spam logs on every startup.
        self.execute(
            "ALTER TABLE discord_roles ADD COLUMN permissions INTEGER NOT NULL DEFAULT 0",
            quiet=True,
        )
        self.execute('''
            CREATE TABLE IF NOT EXISTS discord_channels (
                channel_id   TEXT PRIMARY KEY,
                guild_id     TEXT NOT NULL,
                name         TEXT NOT NULL,
                kind         TEXT NOT NULL,         -- text / voice / category / forum / stage / news / thread
                category_id  TEXT,
                category_name TEXT,
                position     INTEGER NOT NULL DEFAULT 0,
                topic        TEXT,
                nsfw         INTEGER NOT NULL DEFAULT 0,
                last_synced  TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
        ''')
        # Member snapshot. Composite key on (guild_id, user_id) because the
        # same Discord user can technically be in multiple guilds the bot
        # serves; each membership has its own display_name and joined_at.
        self.execute('''
            CREATE TABLE IF NOT EXISTS discord_members (
                guild_id     TEXT NOT NULL,
                user_id      TEXT NOT NULL,
                name         TEXT NOT NULL,         -- global username
                display_name TEXT NOT NULL,         -- per-guild nickname or display name
                is_bot       INTEGER NOT NULL DEFAULT 0,
                joined_at    TEXT,
                last_synced  TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                PRIMARY KEY (guild_id, user_id)
            )
        ''')
        # Association table — one row per (member, role) pairing. Replaced
        # wholesale on every sync, so removed roles disappear automatically.
        self.execute('''
            CREATE TABLE IF NOT EXISTS discord_member_roles (
                guild_id  TEXT NOT NULL,
                user_id   TEXT NOT NULL,
                role_id   TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id, role_id)
            )
        ''')
        # Indexes to keep "who has role X?" / "what roles does user Y have?"
        # cheap even on servers with thousands of members.
        self.execute("CREATE INDEX IF NOT EXISTS idx_member_roles_role ON discord_member_roles(role_id)")
        self.execute("CREATE INDEX IF NOT EXISTS idx_member_roles_user ON discord_member_roles(guild_id, user_id)")
        # Per-channel permission overwrites. Each channel can override the
        # server-level permissions for any role or member with an `allow`
        # bitmask (perms granted) and a `deny` bitmask (perms revoked). We
        # store one row per (channel, target) pair; replaced wholesale on
        # every sync. ``target_kind`` is 'role' or 'member'; ``target_name``
        # is denormalised so the scan dump doesn't need extra joins.
        self.execute('''
            CREATE TABLE IF NOT EXISTS discord_channel_overwrites (
                guild_id    TEXT NOT NULL,
                channel_id  TEXT NOT NULL,
                target_id   TEXT NOT NULL,
                target_kind TEXT NOT NULL,           -- 'role' or 'member'
                target_name TEXT NOT NULL,
                allow       INTEGER NOT NULL DEFAULT 0,
                deny        INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (channel_id, target_id)
            )
        ''')
        self.execute("CREATE INDEX IF NOT EXISTS idx_channel_overwrites_guild ON discord_channel_overwrites(guild_id)")
        self.execute("CREATE INDEX IF NOT EXISTS idx_channel_overwrites_target ON discord_channel_overwrites(target_id)")
        debug.info_log("Initialized discord_roles / discord_channels / discord_members / discord_member_roles / discord_channel_overwrites tables.")

    def sync_discord_inventory(self, guild) -> tuple[int, int, int]:
        """Replace the cached snapshot for ``guild`` with the current live state.

        Snapshots roles, channels, members, and the member→role association.
        Removes rows for that guild that no longer exist (so renames / departures
        / role-removes propagate). Returns ``(role_count, channel_count, member_count)``.
        ``guild`` is a ``discord.Guild`` but the function only uses public
        attributes so the DB layer doesn't import discord.
        """
        try:
            if not self.connection:
                self.connect()
            gid = str(guild.id)

            # Roles --------------------------------------------------------
            self.cursor.execute("DELETE FROM discord_roles WHERE guild_id = ?", (gid,))
            role_rows = []
            for r in guild.roles:
                role_rows.append((
                    str(r.id), gid, r.name, int(getattr(r.color, "value", 0) or 0),
                    int(r.position), 1 if r.hoist else 0,
                    1 if r.mentionable else 0, 1 if r.managed else 0,
                    1 if r.is_default() else 0, len(r.members),
                    int(getattr(r.permissions, "value", 0) or 0),
                ))
            self.cursor.executemany('''
                INSERT INTO discord_roles
                    (role_id, guild_id, name, color, position, hoist,
                     mentionable, managed, is_default, member_count, permissions)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', role_rows)

            # Channels -----------------------------------------------------
            self.cursor.execute("DELETE FROM discord_channels WHERE guild_id = ?", (gid,))
            self.cursor.execute("DELETE FROM discord_channel_overwrites WHERE guild_id = ?", (gid,))
            chan_rows = []
            overwrite_rows: list[tuple] = []
            for ch in guild.channels:
                cat = getattr(ch, "category", None)
                kind = type(ch).__name__.replace("Channel", "").lower() or "unknown"
                chan_rows.append((
                    str(ch.id), gid, ch.name, kind,
                    str(cat.id) if cat else None,
                    cat.name if cat else None,
                    int(getattr(ch, "position", 0) or 0),
                    getattr(ch, "topic", None),
                    1 if getattr(ch, "nsfw", False) else 0,
                ))
                # Permission overwrites: dict {target -> PermissionOverwrite}.
                # ``.pair()`` returns (allow, deny) Permissions objects whose
                # .value is the bitmask we want to persist. Skip categories
                # silently if they fail (defensive — every channel type that
                # supports overwrites exposes ``overwrites``).
                try:
                    for target, overwrite in getattr(ch, "overwrites", {}).items():
                        allow_perms, deny_perms = overwrite.pair()
                        allow_bits = int(getattr(allow_perms, "value", 0) or 0)
                        deny_bits = int(getattr(deny_perms, "value", 0) or 0)
                        if allow_bits == 0 and deny_bits == 0:
                            continue  # neutral overwrite — nothing to record
                        # ``target`` is a Role or Member-like object.
                        kind_str = "role" if hasattr(target, "permissions") and not hasattr(target, "joined_at") else "member"
                        overwrite_rows.append((
                            gid, str(ch.id), str(target.id), kind_str,
                            getattr(target, "name", str(target.id)),
                            allow_bits, deny_bits,
                        ))
                except Exception as exc:  # noqa: BLE001
                    debug.error_log(
                        f"sync_discord_inventory: failed to read overwrites for "
                        f"#{ch.name} ({ch.id}): {exc!r}"
                    )
            self.cursor.executemany('''
                INSERT INTO discord_channels
                    (channel_id, guild_id, name, kind,
                     category_id, category_name, position, topic, nsfw)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', chan_rows)
            self.cursor.executemany('''
                INSERT OR REPLACE INTO discord_channel_overwrites
                    (guild_id, channel_id, target_id, target_kind,
                     target_name, allow, deny)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', overwrite_rows)

            # Members + member↔role association --------------------------
            # Note: needs the privileged "Server Members" intent to see the
            # full roster. Without it `guild.members` only contains the bot
            # itself plus anyone seen in a recent event — sync still works,
            # just incomplete. The bot already requests members intent.
            self.cursor.execute("DELETE FROM discord_members WHERE guild_id = ?", (gid,))
            self.cursor.execute("DELETE FROM discord_member_roles WHERE guild_id = ?", (gid,))
            member_rows = []
            membership_rows = []
            for m in guild.members:
                member_rows.append((
                    gid, str(m.id), m.name, m.display_name,
                    1 if m.bot else 0,
                    m.joined_at.isoformat() if m.joined_at else None,
                ))
                for r in m.roles:
                    if r.is_default():
                        # Skip @everyone; every member implicitly has it and
                        # storing it would just bloat the table by ~N rows.
                        continue
                    membership_rows.append((gid, str(m.id), str(r.id)))
            self.cursor.executemany('''
                INSERT INTO discord_members
                    (guild_id, user_id, name, display_name, is_bot, joined_at)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', member_rows)
            self.cursor.executemany('''
                INSERT OR IGNORE INTO discord_member_roles
                    (guild_id, user_id, role_id)
                VALUES (?, ?, ?)
            ''', membership_rows)

            self.connection.commit()
            return len(role_rows), len(chan_rows), len(member_rows)
        except sqlite3.Error as e:
            debug.error_log(f"sync_discord_inventory error: {e}")
            return 0, 0, 0

    def fetch_discord_roles(self, guild_id: str | None = None) -> list[dict]:
        try:
            if not self.connection:
                self.connect()
            if guild_id:
                self.cursor.execute(
                    "SELECT * FROM discord_roles WHERE guild_id = ? ORDER BY position DESC",
                    (guild_id,),
                )
            else:
                self.cursor.execute("SELECT * FROM discord_roles ORDER BY position DESC")
            return [dict(row) for row in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"fetch_discord_roles error: {e}")
            return []

    def fetch_discord_channels(self, guild_id: str | None = None) -> list[dict]:
        try:
            if not self.connection:
                self.connect()
            if guild_id:
                self.cursor.execute(
                    "SELECT * FROM discord_channels WHERE guild_id = ? ORDER BY category_name, position",
                    (guild_id,),
                )
            else:
                self.cursor.execute(
                    "SELECT * FROM discord_channels ORDER BY guild_id, category_name, position"
                )
            return [dict(row) for row in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"fetch_discord_channels error: {e}")
            return []

    def fetch_channel_overwrites(
        self,
        guild_id: str | None = None,
        channel_id: str | None = None,
    ) -> list[dict]:
        """Return cached per-channel permission overwrites.

        Filterable by guild and/or channel; returns one row per
        (channel, target) pair with ``allow`` and ``deny`` bitmasks plus a
        denormalised ``target_name`` for easy display.
        """
        try:
            if not self.connection:
                self.connect()
            clauses, params = [], []
            if guild_id:
                clauses.append("guild_id = ?")
                params.append(guild_id)
            if channel_id:
                clauses.append("channel_id = ?")
                params.append(channel_id)
            sql = "SELECT * FROM discord_channel_overwrites"
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY channel_id, target_kind, target_name"
            self.cursor.execute(sql, params)
            return [dict(row) for row in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"fetch_channel_overwrites error: {e}")
            return []

    # ── Member / member-role helpers ─────────────────────────────────────
    def fetch_discord_members(self, guild_id: str | None = None) -> list[dict]:
        """Return every cached member row, optionally scoped to one guild.

        Ordered by display_name (case-insensitive) so output is stable for
        humans skimming it.
        """
        try:
            if not self.connection:
                self.connect()
            if guild_id:
                self.cursor.execute(
                    "SELECT * FROM discord_members WHERE guild_id = ? ORDER BY LOWER(display_name)",
                    (guild_id,),
                )
            else:
                self.cursor.execute(
                    "SELECT * FROM discord_members ORDER BY guild_id, LOWER(display_name)"
                )
            return [dict(row) for row in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"fetch_discord_members error: {e}")
            return []

    def fetch_member_roles(self, guild_id: str, user_id: str) -> list[dict]:
        """Return the role rows assigned to one member, top→bottom (highest
        position first). Skips @everyone (we don't store it). Useful for
        permission checks and "what is this person?" lookups.
        """
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute('''
                SELECT r.*
                FROM discord_member_roles mr
                JOIN discord_roles r ON r.role_id = mr.role_id
                WHERE mr.guild_id = ? AND mr.user_id = ?
                ORDER BY r.position DESC
            ''', (guild_id, user_id))
            return [dict(row) for row in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"fetch_member_roles error: {e}")
            return []

    def fetch_role_members(self, role_id: str) -> list[dict]:
        """Return every member who has the given role_id. Joined with
        discord_members so the caller gets display_name without a second
        query. Stable alphabetical order.
        """
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute('''
                SELECT m.*
                FROM discord_member_roles mr
                JOIN discord_members m
                  ON m.guild_id = mr.guild_id AND m.user_id = mr.user_id
                WHERE mr.role_id = ?
                ORDER BY LOWER(m.display_name)
            ''', (role_id,))
            return [dict(row) for row in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"fetch_role_members error: {e}")
            return []

    def member_has_role(
        self,
        guild_id: str,
        user_id: str,
        role_names: list[str] | None = None,
        role_ids: list[str] | None = None,
    ) -> bool:
        """Cheap "does this member have ANY of these roles?" check used by
        permission gates (e.g. prime-time event creation). Pass ``role_names``
        for human-readable callers (matched case-insensitively against the
        cached snapshot) or ``role_ids`` when you already know the IDs.
        """
        if not role_names and not role_ids:
            return False
        try:
            if not self.connection:
                self.connect()
            if role_ids:
                placeholders = ",".join("?" for _ in role_ids)
                self.cursor.execute(
                    f"SELECT 1 FROM discord_member_roles WHERE guild_id = ? AND user_id = ? "
                    f"AND role_id IN ({placeholders}) LIMIT 1",
                    (guild_id, user_id, *role_ids),
                )
                if self.cursor.fetchone():
                    return True
            if role_names:
                placeholders = ",".join("?" for _ in role_names)
                self.cursor.execute(
                    f"SELECT 1 FROM discord_member_roles mr "
                    f"JOIN discord_roles r ON r.role_id = mr.role_id "
                    f"WHERE mr.guild_id = ? AND mr.user_id = ? "
                    f"AND LOWER(r.name) IN ({placeholders}) LIMIT 1",
                    (guild_id, user_id, *[n.lower() for n in role_names]),
                )
                if self.cursor.fetchone():
                    return True
            return False
        except sqlite3.Error as e:
            debug.error_log(f"member_has_role error: {e}")
            return False

    def find_role_by_keywords(self, guild_id: str, keywords: list[str]) -> dict | None:
        """Substring/keyword fuzzy match on cached roles. Skips @everyone and managed roles."""
        if not keywords:
            return None
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "SELECT * FROM discord_roles WHERE guild_id = ? AND is_default = 0 AND managed = 0",
                (guild_id,),
            )
            rows = [dict(r) for r in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"find_role_by_keywords error: {e}")
            return None

        norm_keys = ["".join(c for c in k.lower() if c.isalnum()) for k in keywords if k]
        best: tuple[int, dict] | None = None
        for row in rows:
            n = "".join(c for c in row["name"].lower() if c.isalnum())
            category = "".join(c for c in (row.get("category_name") or "").lower() if c.isalnum())
            combined = f"{category}{n}"
            score = 0
            for k in norm_keys:
                if not k:
                    continue
                if n == k:
                    score = max(score, 3)
                elif combined == k or combined.endswith(k):
                    score = max(score, 4)
                elif n.startswith(k) or n.endswith(k):
                    score = max(score, 2)
                elif k in combined:
                    score = max(score, 2)
                elif k in n:
                    score = max(score, 1)
            if score and (best is None or score > best[0]):
                best = (score, row)
        return best[1] if best else None

    def find_channel_by_keywords(
        self,
        guild_id: str,
        keywords: list[str],
        kind: str | None = "text",
        exclude_categories: list[str] | None = None,
    ) -> dict | None:
        """Fuzzy match a cached channel by keyword.

        ``exclude_categories`` is a list of category-name keywords (matched
        case-insensitively as substrings against ``category_name``). Channels
        living under a matching category are filtered out before scoring.
        Useful for ignoring info/guide channels when auto-detecting an
        LFG-post channel — e.g. so a "ZvZ" content role doesn't accidentally
        get mapped to ``#small-scales-zvz`` inside a "guides" category.
        """
        if not keywords:
            return None
        try:
            if not self.connection:
                self.connect()
            if kind:
                self.cursor.execute(
                    "SELECT * FROM discord_channels WHERE guild_id = ? AND kind = ?",
                    (guild_id, kind),
                )
            else:
                self.cursor.execute(
                    "SELECT * FROM discord_channels WHERE guild_id = ?",
                    (guild_id,),
                )
            rows = [dict(r) for r in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"find_channel_by_keywords error: {e}")
            return None

        excludes = [e.lower() for e in (exclude_categories or []) if e]
        if excludes:
            rows = [
                r for r in rows
                if not any(ex in (r.get("category_name") or "").lower() for ex in excludes)
            ]

        norm_keys = ["".join(c for c in k.lower() if c.isalnum()) for k in keywords if k]
        best: tuple[int, dict] | None = None
        for row in rows:
            n = "".join(c for c in row["name"].lower() if c.isalnum())
            category = "".join(c for c in (row.get("category_name") or "").lower() if c.isalnum())
            combined = f"{category}{n}"
            score = 0
            for k in norm_keys:
                if not k:
                    continue
                if n == k:
                    score = max(score, 3)
                elif combined == k or combined.endswith(k):
                    score = max(score, 4)
                elif n.startswith(k) or n.endswith(k):
                    score = max(score, 2)
                elif k in combined:
                    score = max(score, 2)
                elif k in n:
                    score = max(score, 1)
            if score and (best is None or score > best[0]):
                best = (score, row)
        return best[1] if best else None

    # LFG / Event board persistence lives in cogs._db_lfg.LfgDatabaseMixin.
    # ──────────────────────────────────────────────────────────────────────
    # Duties (per-rank recurring checklists)
    # ──────────────────────────────────────────────────────────────────────
    def initialize_duties_tables(self) -> None:
        self.execute('''
            CREATE TABLE IF NOT EXISTS duty_definitions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                rank_name     TEXT NOT NULL,
                title         TEXT NOT NULL,
                description   TEXT,
                cadence       TEXT NOT NULL DEFAULT 'weekly',  -- daily/weekly/once
                display_order INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                UNIQUE(rank_name, title)
            )
        ''')
        self.execute('''
            CREATE TABLE IF NOT EXISTS duty_completions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                duty_id       INTEGER NOT NULL,
                completed_by  TEXT NOT NULL,
                completed_at  TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                period_key    TEXT NOT NULL,
                note          TEXT,
                UNIQUE(duty_id, period_key, completed_by),
                FOREIGN KEY(duty_id) REFERENCES duty_definitions(id) ON DELETE CASCADE
            )
        ''')
        debug.info_log("Initialized duties tables.")

    def add_duty(self, rank_name: str, title: str, description: str,
                 cadence: str, display_order: int = 0) -> int | None:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute('''
                INSERT OR IGNORE INTO duty_definitions
                    (rank_name, title, description, cadence, display_order)
                VALUES (?, ?, ?, ?, ?)
            ''', (rank_name, title, description, cadence, display_order))
            self.connection.commit()
            return self.cursor.lastrowid or None
        except sqlite3.Error as e:
            debug.error_log(f"Error adding duty: {e}")
            return None

    def remove_duty(self, duty_id: int) -> None:
        self.execute('DELETE FROM duty_completions WHERE duty_id = ?', (duty_id,))
        self.execute('DELETE FROM duty_definitions WHERE id = ?', (duty_id,))

    def fetch_duty(self, duty_id: int) -> dict | None:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute('SELECT * FROM duty_definitions WHERE id = ?', (duty_id,))
            row = self.cursor.fetchone()
            return dict(row) if row else None
        except sqlite3.Error as e:
            debug.error_log(f"Error fetching duty {duty_id}: {e}")
            return None

    def fetch_duties_for_rank(self, rank_name: str) -> list[dict]:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute('''
                SELECT * FROM duty_definitions WHERE rank_name = ?
                ORDER BY display_order, id
            ''', (rank_name,))
            return [dict(r) for r in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"Error fetching duties for {rank_name}: {e}")
            return []

    def fetch_all_duties(self) -> list[dict]:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute('''
                SELECT * FROM duty_definitions
                ORDER BY rank_name, display_order, id
            ''')
            return [dict(r) for r in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"Error fetching all duties: {e}")
            return []

    def fetch_completions_for_period(self, duty_id: int, period_key: str) -> list[dict]:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute('''
                SELECT * FROM duty_completions
                WHERE duty_id = ? AND period_key = ?
                ORDER BY completed_at
            ''', (duty_id, period_key))
            return [dict(r) for r in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"Error fetching completions: {e}")
            return []

    def record_duty_completion(self, duty_id: int, user_id: str,
                               period_key: str, note: str | None) -> bool:
        """Returns True if a new completion was recorded, False if duplicate."""
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute('''
                INSERT OR IGNORE INTO duty_completions (duty_id, completed_by, period_key, note)
                VALUES (?, ?, ?, ?)
            ''', (duty_id, user_id, period_key, note))
            self.connection.commit()
            return self.cursor.rowcount > 0
        except sqlite3.Error as e:
            debug.error_log(f"Error recording completion: {e}")
            return False

    def fetch_user_recent_completions(self, user_id: str, limit: int = 20) -> list[dict]:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute('''
                SELECT c.*, d.title, d.rank_name, d.cadence
                FROM duty_completions c
                JOIN duty_definitions d ON d.id = c.duty_id
                WHERE c.completed_by = ?
                ORDER BY c.completed_at DESC
                LIMIT ?
            ''', (user_id, limit))
            return [dict(r) for r in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"Error fetching user completions: {e}")
            return []

    # ──────────────────────────────────────────────────────────────────────
    # Bounties (public bounty board)
    # ──────────────────────────────────────────────────────────────────────
    def initialize_bounties_table(self) -> None:
        self.execute('''
            CREATE TABLE IF NOT EXISTS bounties (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                title           TEXT NOT NULL,
                description     TEXT NOT NULL,
                reward_points   INTEGER NOT NULL DEFAULT 0,
                posted_by       TEXT NOT NULL,
                posted_at       TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                deadline        TEXT,
                status          TEXT NOT NULL DEFAULT 'open',
                claimed_by      TEXT,
                claimed_at      TEXT,
                submitted_at    TEXT,
                proof           TEXT,
                completed_by    TEXT,
                completed_at    TEXT,
                paid_by         TEXT,
                paid_at         TEXT,
                channel_id      TEXT,
                message_id      TEXT
            )
        ''')
        if not self.connection:
            self.connect()
        for column, ddl in (
            ("paid_by", "TEXT"),
            ("paid_at", "TEXT"),
        ):
            try:
                self.cursor.execute(f"ALTER TABLE bounties ADD COLUMN {column} {ddl}")
                self.connection.commit()
            except sqlite3.OperationalError:
                pass
        debug.info_log("Initialized bounties table.")

    def initialize_bounty_kill_matches_table(self) -> None:
        """De-dupe auto-detected killboard submissions for enemy bounties."""
        self.execute('''
            CREATE TABLE IF NOT EXISTS bounty_kill_matches (
                event_id          TEXT NOT NULL,
                bounty_id         INTEGER NOT NULL,
                killer_discord_id TEXT NOT NULL,
                killer_player_id  TEXT,
                killer_name       TEXT,
                victim_name       TEXT,
                victim_guild      TEXT,
                victim_alliance   TEXT,
                kill_fame         INTEGER DEFAULT 0,
                killboard_url     TEXT,
                event_time        TEXT,
                matched_at        TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                PRIMARY KEY (event_id, bounty_id)
            )
        ''')
        debug.info_log("Initialized bounty kill matches table.")

    def initialize_bounty_shopping_items_table(self) -> None:
        """Line-item tracking for the consolidated 'Crafting shopping list'
        bounty. Members can call dibs on individual rows so two crafters
        don't waste effort on the same item."""
        self.execute('''
            CREATE TABLE IF NOT EXISTS bounty_shopping_items (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                bounty_id       INTEGER NOT NULL,
                line_index      INTEGER NOT NULL,
                item_id         TEXT NOT NULL,
                name            TEXT NOT NULL,
                quality         INTEGER NOT NULL DEFAULT 1,
                enchant         INTEGER NOT NULL DEFAULT 0,
                needed          INTEGER NOT NULL DEFAULT 1,
                fulfilled       INTEGER NOT NULL DEFAULT 0,
                unit_reward     INTEGER NOT NULL DEFAULT 0,
                service_fee     INTEGER NOT NULL DEFAULT 0,
                claimed_by      TEXT,
                claimed_at      TEXT,
                UNIQUE(bounty_id, line_index)
            )
        ''')
        # Migrate older rows that predate the reward columns.
        if not self.connection:
            self.connect()
        for col, ddl in (
            ("unit_reward", "INTEGER NOT NULL DEFAULT 0"),
            ("service_fee", "INTEGER NOT NULL DEFAULT 0"),
            ("submitted_at", "TEXT"),
            ("confirmed_by", "TEXT"),
            ("confirmed_at", "TEXT"),
            ("rejection_note", "TEXT"),
        ):
            try:
                self.cursor.execute(
                    f"ALTER TABLE bounty_shopping_items ADD COLUMN {col} {ddl}"
                )
                self.connection.commit()
            except sqlite3.OperationalError:
                pass  # Column already exists — silent no-op.
        debug.info_log("Initialized bounty_shopping_items table.")

    def initialize_help_tickets_table(self) -> None:
        self.execute('''
            CREATE TABLE IF NOT EXISTS help_tickets (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id            TEXT,
                asker_id            TEXT NOT NULL,
                asker_name          TEXT NOT NULL,
                question            TEXT NOT NULL,
                source_channel_id   TEXT,
                source_message_id   TEXT,
                source_jump_url     TEXT,
                created_at          TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                status              TEXT NOT NULL DEFAULT 'open',
                claimed_by          TEXT,
                claimed_by_name     TEXT,
                claimed_at          TEXT,
                resolved_by         TEXT,
                resolved_by_name    TEXT,
                resolved_at         TEXT,
                resolution_note     TEXT,
                ticket_channel_id   TEXT,
                ticket_message_id   TEXT
            )
        ''')
        # Migration: add guild_id to older tables created before multi-guild support.
        try:
            self.cursor.execute("PRAGMA table_info(help_tickets)")
            cols = {row[1] for row in self.cursor.fetchall()}
            if "guild_id" not in cols:
                self.execute("ALTER TABLE help_tickets ADD COLUMN guild_id TEXT")
                debug.info_log("help_tickets: migrated — added guild_id column.")
        except Exception as exc:  # noqa: BLE001
            debug.error_log(f"help_tickets: guild_id migration failed: {exc!r}")
        debug.info_log("Initialized help_tickets table.")

    # ──────────────────────────────────────────────────────────────────────
    # Blacklist: discord IDs and/or Albion player IDs permanently denied.
    # Auto-kicked on Discord join, blocked from registration. Tracking the
    # Albion player_id catches alt Discord accounts trying to re-register
    # the same character.
    # ──────────────────────────────────────────────────────────────────────
    def initialize_blacklist_table(self) -> None:
        self.execute('''
            CREATE TABLE IF NOT EXISTS blacklist (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id        TEXT,
                albion_player_id  TEXT,
                albion_name       TEXT,
                username          TEXT,
                reason            TEXT,
                added_by          TEXT,
                added_at          TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
        ''')
        # One row per discord_id and per albion_player_id so lookups are O(1).
        self.execute(
            'CREATE UNIQUE INDEX IF NOT EXISTS idx_blacklist_discord '
            'ON blacklist (discord_id) WHERE discord_id IS NOT NULL'
        )
        self.execute(
            'CREATE UNIQUE INDEX IF NOT EXISTS idx_blacklist_player '
            'ON blacklist (albion_player_id) WHERE albion_player_id IS NOT NULL'
        )
        debug.info_log("Initialized blacklist table.")

    def add_to_blacklist(self, *, discord_id: str | None = None,
                          albion_player_id: str | None = None,
                          albion_name: str | None = None,
                          username: str | None = None,
                          reason: str | None = None,
                          added_by: str | None = None) -> None:
        """Add a discord_id, an albion_player_id, or both to the blacklist.

        Either ``discord_id`` or ``albion_player_id`` is required. Caller
        should pre-resolve a player name → id before calling.
        """
        if not discord_id and not albion_player_id:
            debug.error_log("add_to_blacklist: need at least discord_id or albion_player_id")
            return
        # Upsert by whichever key is present. If both, store as one row.
        try:
            if not self.connection:
                self.connect()
            # Remove any prior rows for either key so we don't duplicate.
            if discord_id:
                self.cursor.execute('DELETE FROM blacklist WHERE discord_id = ?', (str(discord_id),))
            if albion_player_id:
                self.cursor.execute('DELETE FROM blacklist WHERE albion_player_id = ?', (str(albion_player_id),))
            self.cursor.execute(
                'INSERT INTO blacklist (discord_id, albion_player_id, albion_name, '
                'username, reason, added_by) VALUES (?, ?, ?, ?, ?, ?)',
                (
                    str(discord_id) if discord_id else None,
                    str(albion_player_id) if albion_player_id else None,
                    albion_name, username, reason, added_by,
                )
            )
            self.connection.commit()
        except sqlite3.Error as exc:
            debug.error_log(f"add_to_blacklist failed: {exc!r}")

    def remove_from_blacklist(self, *, discord_id: str | None = None,
                               albion_player_id: str | None = None) -> int:
        """Delete blacklist rows matching either key. Returns rows removed."""
        if not discord_id and not albion_player_id:
            return 0
        try:
            if not self.connection:
                self.connect()
            removed = 0
            if discord_id:
                self.cursor.execute('DELETE FROM blacklist WHERE discord_id = ?', (str(discord_id),))
                removed += self.cursor.rowcount
            if albion_player_id:
                self.cursor.execute('DELETE FROM blacklist WHERE albion_player_id = ?', (str(albion_player_id),))
                removed += self.cursor.rowcount
            self.connection.commit()
            return removed
        except sqlite3.Error as exc:
            debug.error_log(f"remove_from_blacklist failed: {exc!r}")
            return 0

    def is_blacklisted(self, *, discord_id: str | None = None,
                        albion_player_id: str | None = None) -> dict | None:
        """Return the matching blacklist row if either key matches, else None."""
        if not discord_id and not albion_player_id:
            return None
        try:
            if not self.connection:
                self.connect()
            clauses, params = [], []
            if discord_id:
                clauses.append('discord_id = ?')
                params.append(str(discord_id))
            if albion_player_id:
                clauses.append('albion_player_id = ?')
                params.append(str(albion_player_id))
            self.cursor.execute(
                'SELECT id, discord_id, albion_player_id, albion_name, username, '
                'reason, added_by, added_at FROM blacklist '
                f'WHERE {" OR ".join(clauses)} LIMIT 1',
                tuple(params)
            )
            row = self.cursor.fetchone()
            return dict(row) if row else None
        except sqlite3.Error as exc:
            debug.error_log(f"is_blacklisted failed: {exc!r}")
            return None

    def fetch_all_blacklist(self) -> list[dict]:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                'SELECT id, discord_id, albion_player_id, albion_name, username, '
                'reason, added_by, added_at FROM blacklist ORDER BY added_at DESC'
            )
            return [dict(row) for row in self.cursor.fetchall()]
        except sqlite3.Error as exc:
            debug.error_log(f"fetch_all_blacklist failed: {exc!r}")
            return []

    # ──────────────────────────────────────────────────────────────────────
    # Albion risk watch: officer-only tracking for public guild/alliance
    # movement of specific Albion characters. This does not message outside
    # guilds automatically; it only gives officers a private review alert.
    # ──────────────────────────────────────────────────────────────────────
    def initialize_risk_watch_table(self) -> None:
        self.execute('''
            CREATE TABLE IF NOT EXISTS albion_risk_watch (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                albion_player_id    TEXT NOT NULL UNIQUE,
                albion_name         TEXT,
                server              TEXT NOT NULL DEFAULT 'americas',
                reason              TEXT,
                evidence_note       TEXT,
                added_by            TEXT,
                active              INTEGER NOT NULL DEFAULT 1,
                last_guild_id       TEXT,
                last_guild_name     TEXT,
                last_alliance_id    TEXT,
                last_alliance_name  TEXT,
                last_alliance_tag   TEXT,
                last_seen_at        TEXT,
                last_alerted_at     TEXT,
                added_at            TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                updated_at          TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
        ''')
        self.execute(
            'CREATE INDEX IF NOT EXISTS idx_albion_risk_watch_active '
            'ON albion_risk_watch (active, albion_player_id)'
        )
        debug.info_log("Initialized albion_risk_watch table.")

    def add_risk_watch(
        self,
        *,
        albion_player_id: str,
        albion_name: str | None = None,
        server: str = "americas",
        reason: str | None = None,
        evidence_note: str | None = None,
        added_by: str | None = None,
        last_guild_id: str | None = None,
        last_guild_name: str | None = None,
        last_alliance_id: str | None = None,
        last_alliance_name: str | None = None,
        last_alliance_tag: str | None = None,
    ) -> None:
        if not albion_player_id:
            debug.error_log("add_risk_watch: missing albion_player_id")
            return
        try:
            if not self.connection:
                self.connect()
            now = _datetime.datetime.now(_datetime.timezone.utc).isoformat()
            self.cursor.execute(
                '''
                INSERT INTO albion_risk_watch (
                    albion_player_id, albion_name, server, reason, evidence_note,
                    added_by, active, last_guild_id, last_guild_name,
                    last_alliance_id, last_alliance_name, last_alliance_tag,
                    last_seen_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(albion_player_id) DO UPDATE SET
                    albion_name        = excluded.albion_name,
                    server             = excluded.server,
                    reason             = excluded.reason,
                    evidence_note      = excluded.evidence_note,
                    added_by           = excluded.added_by,
                    active             = 1,
                    last_guild_id      = excluded.last_guild_id,
                    last_guild_name    = excluded.last_guild_name,
                    last_alliance_id   = excluded.last_alliance_id,
                    last_alliance_name = excluded.last_alliance_name,
                    last_alliance_tag  = excluded.last_alliance_tag,
                    last_seen_at       = excluded.last_seen_at,
                    updated_at         = excluded.updated_at
                ''',
                (
                    str(albion_player_id),
                    albion_name,
                    server or "americas",
                    reason,
                    evidence_note,
                    added_by,
                    last_guild_id,
                    last_guild_name,
                    last_alliance_id,
                    last_alliance_name,
                    last_alliance_tag,
                    now,
                    now,
                ),
            )
            self.connection.commit()
        except sqlite3.Error as exc:
            debug.error_log(f"add_risk_watch failed: {exc!r}")

    def remove_risk_watch(self, *, albion_player_id: str | None = None,
                          albion_name: str | None = None) -> int:
        if not albion_player_id and not albion_name:
            return 0
        try:
            if not self.connection:
                self.connect()
            if albion_player_id:
                self.cursor.execute(
                    "UPDATE albion_risk_watch SET active = 0, updated_at = CURRENT_TIMESTAMP "
                    "WHERE albion_player_id = ?",
                    (str(albion_player_id),),
                )
            else:
                self.cursor.execute(
                    "UPDATE albion_risk_watch SET active = 0, updated_at = CURRENT_TIMESTAMP "
                    "WHERE LOWER(albion_name) = LOWER(?)",
                    (str(albion_name),),
                )
            changed = int(self.cursor.rowcount or 0)
            self.connection.commit()
            return changed
        except sqlite3.Error as exc:
            debug.error_log(f"remove_risk_watch failed: {exc!r}")
            return 0

    def fetch_risk_watch(self, albion_player_id: str) -> dict | None:
        if not albion_player_id:
            return None
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "SELECT * FROM albion_risk_watch "
                "WHERE albion_player_id = ? AND active = 1 LIMIT 1",
                (str(albion_player_id),),
            )
            row = self.cursor.fetchone()
            return dict(row) if row else None
        except sqlite3.Error as exc:
            debug.error_log(f"fetch_risk_watch failed: {exc!r}")
            return None

    def fetch_all_risk_watch(self, *, active_only: bool = True) -> list[dict]:
        try:
            if not self.connection:
                self.connect()
            where = "WHERE active = 1" if active_only else ""
            self.cursor.execute(
                f"SELECT * FROM albion_risk_watch {where} "
                "ORDER BY active DESC, updated_at DESC, added_at DESC"
            )
            return [dict(row) for row in self.cursor.fetchall()]
        except sqlite3.Error as exc:
            debug.error_log(f"fetch_all_risk_watch failed: {exc!r}")
            return []

    def update_risk_watch_seen(
        self,
        *,
        albion_player_id: str,
        albion_name: str | None = None,
        guild_id: str | None = None,
        guild_name: str | None = None,
        alliance_id: str | None = None,
        alliance_name: str | None = None,
        alliance_tag: str | None = None,
        alerted: bool = False,
    ) -> None:
        if not albion_player_id:
            return
        try:
            if not self.connection:
                self.connect()
            now = _datetime.datetime.now(_datetime.timezone.utc).isoformat()
            self.cursor.execute(
                '''
                UPDATE albion_risk_watch
                SET albion_name = COALESCE(?, albion_name),
                    last_guild_id = ?,
                    last_guild_name = ?,
                    last_alliance_id = ?,
                    last_alliance_name = ?,
                    last_alliance_tag = ?,
                    last_seen_at = ?,
                    last_alerted_at = CASE WHEN ? THEN ? ELSE last_alerted_at END,
                    updated_at = ?
                WHERE albion_player_id = ?
                ''',
                (
                    albion_name,
                    guild_id,
                    guild_name,
                    alliance_id,
                    alliance_name,
                    alliance_tag,
                    now,
                    1 if alerted else 0,
                    now,
                    now,
                    str(albion_player_id),
                ),
            )
            self.connection.commit()
        except sqlite3.Error as exc:
            debug.error_log(f"update_risk_watch_seen failed: {exc!r}")

    # ──────────────────────────────────────────────────────────────────────
    # Member lifecycle events (joins / leaves) — append-only audit log so we
    # can chart server growth/churn over time. Joins can be backfilled from
    # ``discord_members.joined_at``; leaves can only be captured going
    # forward (Discord doesn't keep prior membership history we can read).
    # ──────────────────────────────────────────────────────────────────────
    def initialize_member_lifecycle_table(self) -> None:
        self.execute('''
            CREATE TABLE IF NOT EXISTS member_lifecycle_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id    TEXT NOT NULL,
                user_id     TEXT NOT NULL,
                name        TEXT,
                kind        TEXT NOT NULL CHECK (kind IN ('join','leave')),
                occurred_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
        ''')
        self.execute(
            'CREATE INDEX IF NOT EXISTS idx_lifecycle_when '
            'ON member_lifecycle_events (occurred_at)'
        )
        # Prevent duplicate join rows from the backfill running multiple times
        # (same user, same join timestamp → same row).
        self.execute(
            'CREATE UNIQUE INDEX IF NOT EXISTS idx_lifecycle_unique '
            'ON member_lifecycle_events (guild_id, user_id, kind, occurred_at)'
        )
        debug.info_log("Initialized member_lifecycle_events table.")

    def log_member_lifecycle_event(self, guild_id: str, user_id: str,
                                    kind: str, *, name: str | None = None,
                                    occurred_at: str | None = None) -> None:
        """Append a join/leave row. Silently ignores duplicates (unique idx)."""
        if kind not in ("join", "leave"):
            debug.error_log(f"log_member_lifecycle_event: bad kind {kind!r}")
            return
        try:
            if not self.connection:
                self.connect()
            if occurred_at:
                self.cursor.execute(
                    'INSERT OR IGNORE INTO member_lifecycle_events '
                    '(guild_id, user_id, name, kind, occurred_at) '
                    'VALUES (?, ?, ?, ?, ?)',
                    (str(guild_id), str(user_id), name, kind, occurred_at),
                )
            else:
                self.cursor.execute(
                    'INSERT OR IGNORE INTO member_lifecycle_events '
                    '(guild_id, user_id, name, kind) VALUES (?, ?, ?, ?)',
                    (str(guild_id), str(user_id), name, kind),
                )
            self.connection.commit()
        except sqlite3.Error as exc:
            debug.error_log(f"log_member_lifecycle_event failed: {exc!r}")

    def backfill_member_joins(self, guild_id: str) -> int:
        """Seed the lifecycle log from ``discord_members.joined_at`` for any
        rows we don't already have. Returns the number of rows inserted.
        Safe to run repeatedly thanks to the unique index.
        """
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute('''
                INSERT OR IGNORE INTO member_lifecycle_events
                    (guild_id, user_id, name, kind, occurred_at)
                SELECT guild_id, user_id, COALESCE(display_name, name),
                       'join', joined_at
                FROM discord_members
                WHERE guild_id = ?
                  AND is_bot = 0
                  AND joined_at IS NOT NULL
            ''', (str(guild_id),))
            n = self.cursor.rowcount or 0
            self.connection.commit()
            return n
        except sqlite3.Error as exc:
            debug.error_log(f"backfill_member_joins failed: {exc!r}")
            return 0

    def fetch_lifecycle_weekly(self, days: int = 30) -> list[dict]:
        """Return per-week joiner & leaver counts over the last ``days``.

        Output rows: ``{week_start: 'YYYY-MM-DD', joins: int, leaves: int}``
        ordered ascending. Weeks with zero of both are still included so the
        chart doesn't have gaps.
        """
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute('''
                WITH bucketed AS (
                    SELECT date(occurred_at,
                                'weekday 0', '-6 days') AS week_start,
                           kind
                    FROM member_lifecycle_events
                    WHERE occurred_at >= datetime('now', ?)
                )
                SELECT week_start,
                       SUM(CASE WHEN kind='join'  THEN 1 ELSE 0 END) AS joins,
                       SUM(CASE WHEN kind='leave' THEN 1 ELSE 0 END) AS leaves
                FROM bucketed
                GROUP BY week_start
                ORDER BY week_start ASC
            ''', (f'-{int(days)} days',))
            return [dict(r) for r in self.cursor.fetchall()]
        except sqlite3.Error as exc:
            debug.error_log(f"fetch_lifecycle_weekly failed: {exc!r}")
            return []

    # ──────────────────────────────────────────────────────────────────────
    # Silver finance summaries — used by the dashboard's finance panel.
    # ──────────────────────────────────────────────────────────────────────
    def fetch_silver_top(self, limit: int = 5,
                         home_guild: str | None = None) -> dict:
        """Return ``{'creditors': [...], 'debtors': [...], 'totals': {...}}``.

        Creditors = guild owes them (positive ``silver_balance``).
        Debtors   = they owe the guild (negative ``silver_balance``).
        Each entry: ``{name, discord_id, balance}``.
        ``totals`` has ``owed_to_members``, ``owed_by_members``, ``net``,
        and counts (``n_creditors``, ``n_debtors``, ``n_zero``).
        """
        try:
            if not self.connection:
                self.connect()
            params: list = []
            guild_clause = ""
            if home_guild:
                guild_clause = " AND LOWER(guild_name) = LOWER(?)"
                params.append(home_guild)

            self.cursor.execute(
                f'''SELECT albion_name AS name, discord_id,
                           COALESCE(silver_balance, 0) AS balance
                    FROM user_profiles
                    WHERE COALESCE(silver_balance, 0) > 0{guild_clause}
                    ORDER BY balance DESC LIMIT ?''',
                (*params, limit),
            )
            creditors = [dict(r) for r in self.cursor.fetchall()]

            self.cursor.execute(
                f'''SELECT albion_name AS name, discord_id,
                           COALESCE(silver_balance, 0) AS balance
                    FROM user_profiles
                    WHERE COALESCE(silver_balance, 0) < 0{guild_clause}
                    ORDER BY balance ASC LIMIT ?''',
                (*params, limit),
            )
            debtors = [dict(r) for r in self.cursor.fetchall()]

            self.cursor.execute(
                f'''SELECT
                       SUM(CASE WHEN COALESCE(silver_balance,0) > 0
                                THEN silver_balance ELSE 0 END) AS owed_to,
                       SUM(CASE WHEN COALESCE(silver_balance,0) < 0
                                THEN -silver_balance ELSE 0 END) AS owed_by,
                       SUM(CASE WHEN COALESCE(silver_balance,0) > 0
                                THEN 1 ELSE 0 END) AS n_creditors,
                       SUM(CASE WHEN COALESCE(silver_balance,0) < 0
                                THEN 1 ELSE 0 END) AS n_debtors,
                       SUM(CASE WHEN COALESCE(silver_balance,0) = 0
                                THEN 1 ELSE 0 END) AS n_zero
                    FROM user_profiles
                    WHERE 1=1{guild_clause}''',
                tuple(params),
            )
            row = self.cursor.fetchone()
            totals = {
                "owed_to_members": int(row["owed_to"] or 0),
                "owed_by_members": int(row["owed_by"] or 0),
                "n_creditors": int(row["n_creditors"] or 0),
                "n_debtors": int(row["n_debtors"] or 0),
                "n_zero": int(row["n_zero"] or 0),
            }
            totals["net"] = totals["owed_to_members"] - totals["owed_by_members"]
            return {"creditors": creditors, "debtors": debtors, "totals": totals}
        except sqlite3.Error as exc:
            debug.error_log(f"fetch_silver_top failed: {exc!r}")
            return {"creditors": [], "debtors": [], "totals": {}}

    # Loadout chest persistence lives in cogs._db_loadout_chest.LoadoutChestDatabaseMixin.

    # ──────────────────────────────────────────────────────────────────────
    # Weekly content schedule — the guild's published rhythm
    # (Mon = fame farm, Tue = faction warfare, etc). Each entry is a
    # template: day-of-week, optional time, event_type, title/description.
    # Officers can publish the board with /schedule view or auto-create
    # this week's LFG events from the templates with /schedule generate.
    # ──────────────────────────────────────────────────────────────────────
    def initialize_weekly_schedule_table(self) -> None:
        self.execute('''
            CREATE TABLE IF NOT EXISTS weekly_schedule (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                day_of_week   INTEGER NOT NULL,
                  -- 0 = Monday … 6 = Sunday (ISO weekday - 1)
                start_time    TEXT,
                  -- "HH:MM" UTC; NULL = "anytime that day"
                duration_min  INTEGER NOT NULL DEFAULT 120,
                event_type    TEXT,
                  -- matches cogs/_lfg_config.EVENT_TYPES key
                title         TEXT NOT NULL,
                description   TEXT,
                comp          TEXT,
                  -- optional comp name (auto-fills LFG comp_notes)
                lead_role     TEXT,
                  -- e.g. "Shotcaller", "Mentor" — free text
                active        INTEGER NOT NULL DEFAULT 1,
                created_by    TEXT,
                created_at    TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
        ''')
        self.execute(
            'CREATE INDEX IF NOT EXISTS ix_weekly_dow '
            'ON weekly_schedule(day_of_week, start_time)'
        )
        debug.info_log("Initialized weekly_schedule table.")

    def schedule_add(
        self, *, day_of_week: int, start_time: str | None,
        duration_min: int, event_type: str | None, title: str,
        description: str | None, comp: str | None,
        lead_role: str | None, created_by: str,
    ) -> int:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "INSERT INTO weekly_schedule "
                "(day_of_week, start_time, duration_min, event_type, "
                " title, description, comp, lead_role, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (int(day_of_week), start_time, int(duration_min),
                 event_type, title.strip(), description, comp,
                 lead_role, created_by),
            )
            self.connection.commit()
            return int(self.cursor.lastrowid or 0)
        except sqlite3.Error as e:
            debug.error_log(f"schedule_add error: {e}")
            return 0

    def schedule_remove(self, entry_id: int) -> bool:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "DELETE FROM weekly_schedule WHERE id = ?", (int(entry_id),),
            )
            self.connection.commit()
            return self.cursor.rowcount > 0
        except sqlite3.Error as e:
            debug.error_log(f"schedule_remove error: {e}")
            return False

    def schedule_set_active(self, entry_id: int, active: bool) -> bool:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "UPDATE weekly_schedule SET active = ? WHERE id = ?",
                (1 if active else 0, int(entry_id)),
            )
            self.connection.commit()
            return self.cursor.rowcount > 0
        except sqlite3.Error as e:
            debug.error_log(f"schedule_set_active error: {e}")
            return False

    def schedule_list(self, *, include_inactive: bool = False) -> list[dict]:
        try:
            if not self.connection:
                self.connect()
            sql = "SELECT * FROM weekly_schedule"
            if not include_inactive:
                sql += " WHERE active = 1"
            sql += " ORDER BY day_of_week, COALESCE(start_time, '99:99'), id"
            self.cursor.execute(sql)
            return [dict(r) for r in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"schedule_list error: {e}")
            return []

    # ──────────────────────────────────────────────────────────────────────
    # Recruitment funnel — tracks prospects from first contact through their
    # first event so officers can see which recruiters/sources actually
    # convert.
    # ──────────────────────────────────────────────────────────────────────
    def initialize_recruits_table(self) -> None:
        self.execute('''
            CREATE TABLE IF NOT EXISTS recruits (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                albion_name     TEXT NOT NULL,
                source          TEXT,
                recruiter_id    TEXT,
                discord_id      TEXT,
                status          TEXT NOT NULL DEFAULT 'contacted',
                  -- contacted / discord / registered / first_event /
                  -- retained / lost
                joined_discord_at  TEXT,
                registered_at      TEXT,
                first_event_at     TEXT,
                retained_at        TEXT,
                lost_at            TEXT,
                notes              TEXT,
                created_at         TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                updated_at         TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
        ''')
        self.execute(
            'CREATE INDEX IF NOT EXISTS ix_recruits_status '
            'ON recruits(status, created_at DESC)'
        )
        self.execute(
            'CREATE INDEX IF NOT EXISTS ix_recruits_recruiter '
            'ON recruits(recruiter_id)'
        )
        debug.info_log("Initialized recruits table.")

    _RECRUIT_STAGES = (
        "contacted", "discord", "registered",
        "first_event", "retained", "lost",
    )

    def recruit_add(
        self, *, albion_name: str, source: str | None,
        recruiter_id: str | None, notes: str | None = None,
    ) -> int:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "INSERT INTO recruits "
                "(albion_name, source, recruiter_id, notes) "
                "VALUES (?, ?, ?, ?)",
                ((albion_name or "").strip(), source, recruiter_id, notes),
            )
            self.connection.commit()
            return int(self.cursor.lastrowid or 0)
        except sqlite3.Error as e:
            debug.error_log(f"recruit_add error: {e}")
            return 0

    def recruit_update(
        self, recruit_id: int, *,
        status: str | None = None, notes: str | None = None,
        discord_id: str | None = None, mark_stage: bool = True,
    ) -> bool:
        """Update a recruit row. If ``status`` is set and ``mark_stage`` is
        True, the matching stage timestamp column is filled with NOW."""
        sets: list[str] = []
        params: list = []
        if status is not None:
            if status not in self._RECRUIT_STAGES:
                return False
            sets.append("status = ?")
            params.append(status)
            if mark_stage:
                col = {
                    "discord": "joined_discord_at",
                    "registered": "registered_at",
                    "first_event": "first_event_at",
                    "retained": "retained_at",
                    "lost": "lost_at",
                }.get(status)
                if col:
                    sets.append(f"{col} = CURRENT_TIMESTAMP")
        if notes is not None:
            sets.append("notes = ?")
            params.append(notes)
        if discord_id is not None:
            sets.append("discord_id = ?")
            params.append(discord_id)
        if not sets:
            return False
        sets.append("updated_at = CURRENT_TIMESTAMP")
        try:
            if not self.connection:
                self.connect()
            params.append(recruit_id)
            self.cursor.execute(
                f"UPDATE recruits SET {', '.join(sets)} WHERE id = ?",
                tuple(params),
            )
            self.connection.commit()
            return self.cursor.rowcount > 0
        except sqlite3.Error as e:
            debug.error_log(f"recruit_update error: {e}")
            return False

    def recruit_get(self, recruit_id: int) -> dict | None:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "SELECT * FROM recruits WHERE id = ?", (int(recruit_id),),
            )
            row = self.cursor.fetchone()
            return dict(row) if row else None
        except sqlite3.Error as e:
            debug.error_log(f"recruit_get error: {e}")
            return None

    def recruit_find_by_name(self, albion_name: str) -> dict | None:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "SELECT * FROM recruits WHERE albion_name = ? "
                "COLLATE NOCASE ORDER BY id DESC LIMIT 1",
                ((albion_name or "").strip(),),
            )
            row = self.cursor.fetchone()
            return dict(row) if row else None
        except sqlite3.Error as e:
            debug.error_log(f"recruit_find_by_name error: {e}")
            return None

    def recruit_list(
        self, *, status: str | None = None,
        recruiter_id: str | None = None, limit: int = 100,
    ) -> list[dict]:
        try:
            if not self.connection:
                self.connect()
            sql = "SELECT * FROM recruits WHERE 1=1"
            params: list = []
            if status:
                sql += " AND status = ?"
                params.append(status)
            if recruiter_id:
                sql += " AND recruiter_id = ?"
                params.append(str(recruiter_id))
            sql += " ORDER BY created_at DESC LIMIT ?"
            params.append(int(limit))
            self.cursor.execute(sql, tuple(params))
            return [dict(r) for r in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"recruit_list error: {e}")
            return []

    def recruit_leaderboard(
        self, *, since_iso: str | None = None,
    ) -> list[dict]:
        """Per-recruiter funnel stats: prospects, joined_discord, registered,
        first_event, retained. Used by /recruit leaderboard."""
        try:
            if not self.connection:
                self.connect()
            where = "WHERE recruiter_id IS NOT NULL AND recruiter_id != ''"
            params: list = []
            if since_iso:
                where += " AND created_at >= ?"
                params.append(since_iso)
            self.cursor.execute(
                f"""SELECT recruiter_id,
                       COUNT(*) AS prospects,
                       SUM(CASE WHEN joined_discord_at IS NOT NULL THEN 1 ELSE 0 END) AS joined_discord,
                       SUM(CASE WHEN registered_at IS NOT NULL THEN 1 ELSE 0 END) AS registered,
                       SUM(CASE WHEN first_event_at IS NOT NULL THEN 1 ELSE 0 END) AS first_event,
                       SUM(CASE WHEN retained_at IS NOT NULL THEN 1 ELSE 0 END) AS retained,
                       SUM(CASE WHEN status = 'lost' THEN 1 ELSE 0 END) AS lost
                    FROM recruits
                    {where}
                    GROUP BY recruiter_id
                    ORDER BY retained DESC, first_event DESC, prospects DESC""",
                tuple(params),
            )
            return [dict(r) for r in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"recruit_leaderboard error: {e}")
            return []

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    # ──────────────────────────────────────────────────────────────────────
    # Raffles: officers draw a random winner from a pool. v1 entry source is
    # event attendees (LFG signups with attended=1); future sources can be
    # added by writing rows to ``raffle_entries`` from any other query.
    # ──────────────────────────────────────────────────────────────────────
    def initialize_raffles_table(self) -> None:
        try:
            if not self.connection:
                self.connect()
            self.cursor.executescript('''
                CREATE TABLE IF NOT EXISTS raffles (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id     TEXT,
                    creator_id   TEXT NOT NULL,
                    source_type  TEXT NOT NULL,   -- 'event' (room to grow)
                    source_ref   TEXT,             -- e.g. lfg event id as text
                    prize_type   TEXT NOT NULL,    -- 'silver' | 'points' | 'text'
                    prize_amount INTEGER NOT NULL DEFAULT 0,
                    prize_text   TEXT,
                    status       TEXT NOT NULL DEFAULT 'open',  -- 'open'|'drawn'|'cancelled'
                    winner_id    TEXT,
                    channel_id   TEXT,
                    message_id   TEXT,
                    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
                    drawn_at     TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_raffles_status  ON raffles(status);
                CREATE INDEX IF NOT EXISTS idx_raffles_creator ON raffles(creator_id);

                CREATE TABLE IF NOT EXISTS raffle_entries (
                    raffle_id   INTEGER NOT NULL,
                    discord_id  TEXT    NOT NULL,
                    tickets     INTEGER NOT NULL DEFAULT 1,
                    added_at    TEXT    NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (raffle_id, discord_id),
                    FOREIGN KEY (raffle_id) REFERENCES raffles(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_raffle_entries_raffle ON raffle_entries(raffle_id);
            ''')
            self.connection.commit()
        except sqlite3.Error as e:
            debug.error_log(f"initialize_raffles_table error: {e}")

    def create_raffle(
        self,
        *,
        guild_id: str | None,
        creator_id: str,
        source_type: str,
        source_ref: str | None,
        prize_type: str,
        prize_amount: int = 0,
        prize_text: str | None = None,
    ) -> int | None:
        """Insert a new raffle row and return its id (or None on failure)."""
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                'INSERT INTO raffles '
                '(guild_id, creator_id, source_type, source_ref, '
                ' prize_type, prize_amount, prize_text) '
                'VALUES (?, ?, ?, ?, ?, ?, ?)',
                (guild_id, str(creator_id), source_type, source_ref,
                 prize_type, int(prize_amount or 0), prize_text),
            )
            self.connection.commit()
            return int(self.cursor.lastrowid or 0) or None
        except sqlite3.Error as e:
            debug.error_log(f"create_raffle error: {e}")
            return None

    def add_raffle_entries(self, raffle_id: int, discord_ids: list[str]) -> int:
        """Insert one ticket per discord_id (deduped via PK). Returns count
        of rows actually inserted (existing entries are skipped)."""
        if not discord_ids:
            return 0
        try:
            if not self.connection:
                self.connect()
            inserted = 0
            for did in dict.fromkeys(str(d) for d in discord_ids if d):
                self.cursor.execute(
                    'INSERT OR IGNORE INTO raffle_entries (raffle_id, discord_id) '
                    'VALUES (?, ?)',
                    (int(raffle_id), did),
                )
                inserted += self.cursor.rowcount
            self.connection.commit()
            return inserted
        except sqlite3.Error as e:
            debug.error_log(f"add_raffle_entries error: {e}")
            return 0

    def fetch_raffle(self, raffle_id: int) -> dict | None:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                'SELECT * FROM raffles WHERE id = ?', (int(raffle_id),))
            row = self.cursor.fetchone()
            return dict(row) if row else None
        except sqlite3.Error as e:
            debug.error_log(f"fetch_raffle error: {e}")
            return None

    def fetch_raffle_entries(self, raffle_id: int) -> list[dict]:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                'SELECT discord_id, tickets, added_at FROM raffle_entries '
                'WHERE raffle_id = ? ORDER BY added_at',
                (int(raffle_id),),
            )
            return [dict(r) for r in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"fetch_raffle_entries error: {e}")
            return []

    def fetch_open_raffles(self) -> list[dict]:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "SELECT * FROM raffles WHERE status = 'open' "
                "ORDER BY created_at DESC"
            )
            return [dict(r) for r in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"fetch_open_raffles error: {e}")
            return []

    def set_raffle_message(
        self, raffle_id: int, channel_id: str, message_id: str,
    ) -> None:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                'UPDATE raffles SET channel_id = ?, message_id = ? WHERE id = ?',
                (str(channel_id), str(message_id), int(raffle_id)),
            )
            self.connection.commit()
        except sqlite3.Error as e:
            debug.error_log(f"set_raffle_message error: {e}")

    def mark_raffle_drawn(self, raffle_id: int, winner_id: str) -> bool:
        """Atomically flip status open→drawn and record the winner. Returns
        False if the raffle was already drawn/cancelled (prevents double-draws)."""
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "UPDATE raffles "
                "SET status = 'drawn', winner_id = ?, drawn_at = datetime('now') "
                "WHERE id = ? AND status = 'open'",
                (str(winner_id), int(raffle_id)),
            )
            self.connection.commit()
            return self.cursor.rowcount > 0
        except sqlite3.Error as e:
            debug.error_log(f"mark_raffle_drawn error: {e}")
            return False

    def cancel_raffle(self, raffle_id: int) -> bool:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "UPDATE raffles SET status = 'cancelled' "
                "WHERE id = ? AND status = 'open'",
                (int(raffle_id),),
            )
            self.connection.commit()
            return self.cursor.rowcount > 0
        except sqlite3.Error as e:
            debug.error_log(f"cancel_raffle error: {e}")
            return False
