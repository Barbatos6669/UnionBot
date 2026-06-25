"""Database mixin for LFG/event board persistence.

The public ``Database`` object still exposes these methods directly via
inheritance; this module only keeps the large LFG SQL cluster out of the core
connection/table bootstrap file.
"""
from __future__ import annotations

import datetime as _datetime
import sqlite3
from collections import defaultdict

import debug


class LfgDatabaseMixin:
    # ──────────────────────────────────────────────────────────────────────
    # LFG / Event board (prime-time slots + general LFG)
    # ──────────────────────────────────────────────────────────────────────
    def initialize_lfg_tables(self) -> None:
        # An "event" is a single LFG post. ``slot_label`` is "PRIME 18:00-19:00"
        # or "GENERAL". ``starts_at`` / ``ends_at`` are ISO-8601 UTC strings.
        # ``message_id`` / ``channel_id`` are the posted Discord message so we
        # can edit it when signups change.
        self.execute('''
            CREATE TABLE IF NOT EXISTS lfg_events (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                slot_label    TEXT NOT NULL,
                is_prime      INTEGER NOT NULL DEFAULT 0,
                event_type    TEXT,
                title         TEXT NOT NULL,
                description   TEXT,
                comp_notes    TEXT,
                ip_requirement TEXT,
                starts_at     TEXT NOT NULL,
                ends_at       TEXT NOT NULL,
                prep_minutes  INTEGER NOT NULL DEFAULT 30,
                review_minutes INTEGER NOT NULL DEFAULT 15,
                creator_id    TEXT NOT NULL,
                channel_id    TEXT,
                message_id    TEXT,
                discussion_thread_id TEXT,
                lfg_cleaned_at TEXT,
                voice_channel_id TEXT,
                voice_channel_created_at TEXT,
                voice_channel_pinged_at TEXT,
                voice_channel_deleted_at TEXT,
                access_role_id TEXT,
                access_role_created_at TEXT,
                access_role_deleted_at TEXT,
                cancel_reason TEXT,
                cancelled_by TEXT,
                cancelled_at TEXT,
                status        TEXT NOT NULL DEFAULT 'open',  -- open / cancelled / completed
                created_at    TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
        ''')
        # Migration: add event_type to existing databases that predate it.
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute("ALTER TABLE lfg_events ADD COLUMN event_type TEXT")
            self.connection.commit()
        except sqlite3.OperationalError:
            pass  # already exists
        # Migration: native Discord scheduled event id (the in-app "event tracker").
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "ALTER TABLE lfg_events ADD COLUMN scheduled_event_id TEXT"
            )
            self.connection.commit()
        except sqlite3.OperationalError:
            pass
        # Migration: per-event discussion thread attached to the LFG post.
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "ALTER TABLE lfg_events ADD COLUMN discussion_thread_id TEXT"
            )
            self.connection.commit()
        except sqlite3.OperationalError:
            pass
        # Migration: timestamp for when the posted LFG message was cleaned up
        # from the channel after the event finished.
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "ALTER TABLE lfg_events ADD COLUMN lfg_cleaned_at TEXT"
            )
            self.connection.commit()
        except sqlite3.OperationalError:
            pass
        # Migration: temporary voice channel tied to the event lifecycle.
        for column, column_type in (
            ("voice_channel_id", "TEXT"),
            ("voice_channel_created_at", "TEXT"),
            ("voice_channel_pinged_at", "TEXT"),
            ("voice_channel_deleted_at", "TEXT"),
            ("access_role_id", "TEXT"),
            ("access_role_created_at", "TEXT"),
            ("access_role_deleted_at", "TEXT"),
            ("cancel_reason", "TEXT"),
            ("cancelled_by", "TEXT"),
            ("cancelled_at", "TEXT"),
        ):
            try:
                if not self.connection:
                    self.connect()
                self.cursor.execute(
                    f"ALTER TABLE lfg_events ADD COLUMN {column} {column_type}"
                )
                self.connection.commit()
            except sqlite3.OperationalError:
                pass
        # Migration: link an event to a comp template so the post can render
        # a slot grid and members can claim specific builds at sign-up.
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "ALTER TABLE lfg_events ADD COLUMN comp_id INTEGER "
                "REFERENCES comps(id) ON DELETE SET NULL"
            )
            self.connection.commit()
        except sqlite3.OperationalError:
            pass
        self.execute('''
            CREATE TABLE IF NOT EXISTS lfg_signups (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id    INTEGER NOT NULL,
                discord_id  TEXT NOT NULL,
                signed_at   TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                UNIQUE(event_id, discord_id),
                FOREIGN KEY (event_id) REFERENCES lfg_events(id) ON DELETE CASCADE
            )
        ''')
        # Migration: per-signup attendance tracking. NULL = not marked,
        # 1 = attended. Older rows may contain 0 from the retired miss-tracking flow.
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute("ALTER TABLE lfg_signups ADD COLUMN attended INTEGER")
            self.connection.commit()
        except sqlite3.OperationalError:
            pass
        try:
            self.cursor.execute(
                "ALTER TABLE lfg_signups ADD COLUMN attendance_marked_at TEXT"
            )
            self.connection.commit()
        except sqlite3.OperationalError:
            pass
        # Migration: link a signup to a specific comp slot. NULL = generic
        # sign-up (no build claimed). UNIQUE per event so two members can't
        # double-book the same slot.
        try:
            self.cursor.execute(
                "ALTER TABLE lfg_signups ADD COLUMN slot_id INTEGER "
                "REFERENCES comp_slots(id) ON DELETE SET NULL"
            )
            self.connection.commit()
        except sqlite3.OperationalError:
            pass
        try:
            self.cursor.execute(
                "ALTER TABLE lfg_signups ADD COLUMN signup_kind TEXT "
                "NOT NULL DEFAULT 'main'"
            )
            self.connection.commit()
        except sqlite3.OperationalError:
            pass
        try:
            self.cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_lfg_signup_slot "
                "ON lfg_signups(event_id, slot_id) "
                "WHERE slot_id IS NOT NULL"
            )
            self.connection.commit()
        except sqlite3.OperationalError:
            pass
        # Migration: timestamp at which we DM'd signups the "starts soon" ping.
        # NULL = not yet sent. One-shot per event so a flapping bot can't
        # double-ping members.
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "ALTER TABLE lfg_events ADD COLUMN reminded_at TEXT"
            )
            self.connection.commit()
        except sqlite3.OperationalError:
            pass
        # Migration: normalized minimum-IP requirement, separate from freeform
        # comp notes so temporary voice channel names can be clean.
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "ALTER TABLE lfg_events ADD COLUMN ip_requirement TEXT"
            )
            self.connection.commit()
        except sqlite3.OperationalError:
            pass
        debug.info_log("Initialized lfg_events / lfg_signups tables.")

    def create_lfg_event(self, *, slot_label: str, is_prime: bool, title: str,
                         description: str, comp_notes: str, starts_at: str,
                         ends_at: str, prep_minutes: int, review_minutes: int,
                         creator_id: str, event_type: str | None = None,
                         ip_requirement: str | None = None) -> int:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute('''
                INSERT INTO lfg_events
                    (slot_label, is_prime, event_type, title, description, comp_notes, ip_requirement,
                     starts_at, ends_at, prep_minutes, review_minutes, creator_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (slot_label, 1 if is_prime else 0, event_type, title, description, comp_notes, ip_requirement,
                  starts_at, ends_at, prep_minutes, review_minutes, creator_id))
            self.connection.commit()
            return int(self.cursor.lastrowid)
        except sqlite3.Error as e:
            debug.error_log(f"create_lfg_event error: {e}")
            return 0

    def set_lfg_message(self, event_id: int, channel_id: str, message_id: str) -> None:
        self.execute(
            'UPDATE lfg_events SET channel_id = ?, message_id = ? WHERE id = ?',
            (channel_id, message_id, event_id),
        )

    def update_lfg_event(self, event_id: int, fields: dict) -> bool:
        """Patch arbitrary columns on an ``lfg_events`` row. Only columns in
        the allow-list below can be written; everything else is ignored so a
        caller can't accidentally rewrite ``creator_id`` / ``message_id`` /
        ``status`` etc. Returns True iff a row was actually updated.
        """
        allowed = {
            "title", "description", "comp_notes", "ip_requirement",
            "starts_at", "ends_at",
            "prep_minutes", "review_minutes", "is_prime",
            "event_type", "slot_label",
        }
        clean = {k: v for k, v in fields.items() if k in allowed}
        if not clean:
            return False
        try:
            if not self.connection:
                self.connect()
            cols = ", ".join(f"{k} = ?" for k in clean)
            params = list(clean.values()) + [event_id]
            self.cursor.execute(
                f"UPDATE lfg_events SET {cols} WHERE id = ?", params,
            )
            self.connection.commit()
            return self.cursor.rowcount > 0
        except sqlite3.Error as e:
            debug.error_log(f"update_lfg_event error: {e}")
            return False

    def delete_lfg_event(self, event_id: int) -> None:
        """Hard-delete an LFG event row and its signups. Used to roll back
        rows that were created but couldn't be posted to Discord."""
        self.execute('DELETE FROM lfg_signups WHERE event_id = ?', (event_id,))
        self.execute('DELETE FROM lfg_events WHERE id = ?', (event_id,))

    def set_lfg_scheduled_event_id(self, event_id: int, scheduled_event_id: str | None) -> None:
        """Store (or clear) the native Discord scheduled-event id linked to
        this LFG event. Used so cancel can also cancel the in-app event."""
        self.execute(
            'UPDATE lfg_events SET scheduled_event_id = ? WHERE id = ?',
            (scheduled_event_id, event_id),
        )

    def set_lfg_discussion_thread_id(self, event_id: int, thread_id: str | None) -> None:
        """Store (or clear) the Discord thread attached to this LFG event."""
        self.execute(
            'UPDATE lfg_events SET discussion_thread_id = ? WHERE id = ?',
            (thread_id, event_id),
        )

    def set_lfg_voice_channel_id(self, event_id: int, channel_id: str, when_iso: str) -> None:
        """Store the temporary voice channel created for an LFG event."""
        self.execute(
            "UPDATE lfg_events "
            "SET voice_channel_id = ?, voice_channel_created_at = COALESCE(voice_channel_created_at, ?) "
            "WHERE id = ?",
            (str(channel_id), when_iso, event_id),
        )

    def set_lfg_access_role_id(self, event_id: int, role_id: str, when_iso: str) -> None:
        """Store the temporary role allowed into an event voice channel."""
        self.execute(
            "UPDATE lfg_events "
            "SET access_role_id = ?, "
            "    access_role_created_at = COALESCE(access_role_created_at, ?), "
            "    access_role_deleted_at = NULL "
            "WHERE id = ?",
            (str(role_id), when_iso, event_id),
        )

    def mark_lfg_voice_pinged(self, event_id: int, when_iso: str) -> None:
        self.execute(
            "UPDATE lfg_events SET voice_channel_pinged_at = ? WHERE id = ?",
            (when_iso, event_id),
        )

    def mark_lfg_voice_deleted(self, event_id: int, when_iso: str) -> None:
        self.execute(
            "UPDATE lfg_events SET voice_channel_deleted_at = ? WHERE id = ?",
            (when_iso, event_id),
        )

    def mark_lfg_access_role_deleted(self, event_id: int, when_iso: str) -> None:
        self.execute(
            "UPDATE lfg_events SET access_role_deleted_at = ? WHERE id = ?",
            (when_iso, event_id),
        )

    def fetch_lfg_event_by_scheduled_id(self, scheduled_event_id: str) -> dict | None:
        """Reverse-lookup an LFG event from its linked Discord scheduled-event
        id. Used by the gateway listeners that sync Interested reactions."""
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                'SELECT * FROM lfg_events WHERE scheduled_event_id = ?',
                (str(scheduled_event_id),),
            )
            row = self.cursor.fetchone()
            return dict(row) if row else None
        except sqlite3.Error as e:
            debug.error_log(f"fetch_lfg_event_by_scheduled_id error: {e}")
            return None

    def fetch_lfg_event_by_voice_channel_id(self, voice_channel_id: str | int) -> dict | None:
        """Reverse-lookup an active LFG event from its temporary voice channel.

        The scheduled event status may already be ``completed`` while the
        temporary VC is still alive, because Albion runs often go long. As
        long as the voice channel has not been deleted and the event was not
        cancelled, treat a join/move into that VC as part of the event.
        """
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                """
                SELECT * FROM lfg_events
                WHERE voice_channel_id = ?
                  AND voice_channel_deleted_at IS NULL
                  AND status != 'cancelled'
                ORDER BY datetime(starts_at) DESC
                LIMIT 1
                """,
                (str(voice_channel_id),),
            )
            row = self.cursor.fetchone()
            return dict(row) if row else None
        except sqlite3.Error as e:
            debug.error_log(f"fetch_lfg_event_by_voice_channel_id error: {e}")
            return None

    def fetch_lfg_event(self, event_id: int) -> dict | None:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute('SELECT * FROM lfg_events WHERE id = ?', (event_id,))
            row = self.cursor.fetchone()
            return dict(row) if row else None
        except sqlite3.Error as e:
            debug.error_log(f"fetch_lfg_event error: {e}")
            return None

    def fetch_open_lfg_events(self) -> list[dict]:
        """All non-cancelled events whose review window hasn't fully passed yet.

        Used on startup to re-attach persistent button views to existing posts.
        """
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "SELECT * FROM lfg_events WHERE status = 'open' ORDER BY starts_at"
            )
            return [dict(row) for row in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"fetch_open_lfg_events error: {e}")
            return []

    def fetch_lfg_events_to_remind(self, now_iso: str, window_minutes: int) -> list[dict]:
        """Open events that start within the next ``window_minutes`` and that
        we haven't already DM'd a reminder for. Used by the reminder task to
        ping signups before kickoff.
        """
        try:
            if not self.connection:
                self.connect()
            # Cheap ISO string upper bound. Both columns are ISO-8601 UTC,
            # which sorts lexicographically.
            from datetime import datetime, timedelta
            now_dt = datetime.fromisoformat(now_iso)
            upper = (now_dt + timedelta(minutes=window_minutes)).isoformat()
            self.cursor.execute(
                "SELECT * FROM lfg_events "
                "WHERE status = 'open' AND reminded_at IS NULL "
                "  AND starts_at > ? AND starts_at <= ? "
                "ORDER BY starts_at",
                (now_iso, upper),
            )
            return [dict(row) for row in self.cursor.fetchall()]
        except (sqlite3.Error, ValueError) as e:
            debug.error_log(f"fetch_lfg_events_to_remind error: {e}")
            return []

    def mark_lfg_event_reminded(self, event_id: int, when_iso: str) -> None:
        """Stamp the one-shot reminder flag so we never DM twice for the same
        event, even across bot restarts."""
        self.execute(
            "UPDATE lfg_events SET reminded_at = ? WHERE id = ?",
            (when_iso, event_id),
        )

    def fetch_lfg_events_to_cleanup(
        self,
        now_iso: str,
        limit: int = 25,
    ) -> list[dict]:
        """Posted LFG messages that should be removed from the LFG channel.

        Completed events are eligible after their per-event review window.
        Cancelled events are eligible immediately. ``lfg_cleaned_at`` makes
        this one-shot so deleted/missing messages are not retried forever.
        """
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                """
                SELECT * FROM lfg_events
                WHERE lfg_cleaned_at IS NULL
                  AND channel_id IS NOT NULL AND channel_id != ''
                  AND message_id IS NOT NULL AND message_id != ''
                  AND status IN ('completed', 'cancelled')
                  AND (
                        status = 'cancelled'
                        OR (
                            datetime(ends_at, '+' || COALESCE(review_minutes, 15) || ' minutes')
                               <= datetime(?)
                            AND (
                                voice_channel_id IS NULL
                                OR voice_channel_id = ''
                                OR voice_channel_deleted_at IS NOT NULL
                            )
                        )
                  )
                ORDER BY datetime(ends_at) ASC
                LIMIT ?
                """,
                (now_iso, int(limit)),
            )
            return [dict(row) for row in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"fetch_lfg_events_to_cleanup error: {e}")
            return []

    def fetch_lfg_events_for_voice_lifecycle(
        self,
        now_iso: str,
        limit: int = 50,
    ) -> list[dict]:
        """Events whose temporary event voice channel should be created or cleaned up."""
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                """
                SELECT * FROM lfg_events
                WHERE voice_channel_deleted_at IS NULL
                  AND (
                        (voice_channel_id IS NOT NULL AND voice_channel_id != '')
                        OR (
                            voice_channel_id IS NULL
                            AND status != 'cancelled'
                            AND datetime(starts_at, '-' || COALESCE(prep_minutes, 30) || ' minutes')
                                <= datetime(?)
                            AND datetime(ends_at, '+' || COALESCE(review_minutes, 15) || ' minutes')
                                >= datetime(?)
                        )
                  )
                ORDER BY datetime(starts_at) ASC
                LIMIT ?
                """,
                (now_iso, now_iso, int(limit)),
            )
            return [dict(row) for row in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"fetch_lfg_events_for_voice_lifecycle error: {e}")
            return []

    def mark_lfg_event_cleaned(self, event_id: int, when_iso: str) -> None:
        """Remember that the Discord LFG post was cleaned up from the channel."""
        self.execute(
            "UPDATE lfg_events SET lfg_cleaned_at = ? WHERE id = ?",
            (when_iso, event_id),
        )

    def fetch_user_upcoming_lfg_events(
        self, discord_id: str, now_iso: str, limit: int = 10,
    ) -> list[dict]:
        """Open events a user has signed up for that haven't started yet.

        Ordered by ``starts_at`` ascending so the soonest event is first.
        Used by ``/lfg my-events`` to give members a quick personal view
        without scrolling the board.
        """
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "SELECT e.* FROM lfg_events e "
                "JOIN lfg_signups s ON s.event_id = e.id "
                "WHERE s.discord_id = ? "
                "  AND e.status = 'open' "
                "  AND e.starts_at >= ? "
                "ORDER BY e.starts_at ASC "
                "LIMIT ?",
                (discord_id, now_iso, int(limit)),
            )
            return [dict(row) for row in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"fetch_user_upcoming_lfg_events error: {e}")
            return []

    def fetch_user_lfg_attendance(
        self, discord_id: str, since_iso: str,
    ) -> dict:
        """Aggregate LFG counters for a single user over a time window.

        Returns ``{signups, attended, not_marked_attended}`` where signups is
        every LFG signup in the window and attended is the count explicitly
        marked attended. The bot no longer tracks missed attendance as a
        separate outcome.
        """
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "SELECT "
                "  COUNT(*) AS signups, "
                "  SUM(CASE WHEN s.attended = 1 THEN 1 ELSE 0 END) AS attended "
                "FROM lfg_signups s "
                "JOIN lfg_events e ON e.id = s.event_id "
                "WHERE s.discord_id = ? AND e.starts_at >= ?",
                (discord_id, since_iso),
            )
            row = self.cursor.fetchone()
            if not row:
                return {"signups": 0, "attended": 0, "not_marked_attended": 0}
            signups = int(row["signups"] or 0)
            attended = int(row["attended"] or 0)
            return {
                "signups": signups,
                "attended": attended,
                "not_marked_attended": max(0, signups - attended),
            }
        except sqlite3.Error as e:
            debug.error_log(f"fetch_user_lfg_attendance error: {e}")
            return {"signups": 0, "attended": 0, "not_marked_attended": 0}

    def fetch_overlapping_prime_events(self, starts_at: str, ends_at: str) -> list[dict]:
        """Return open prime-time events whose [prep..review] window overlaps the
        given interval. Used to block double-booked prime-time slots."""
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute('''
                SELECT * FROM lfg_events
                WHERE status = 'open' AND is_prime = 1
                  AND NOT (ends_at <= ? OR starts_at >= ?)
            ''', (starts_at, ends_at))
            return [dict(row) for row in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"fetch_overlapping_prime_events error: {e}")
            return []

    def cancel_lfg_event(
        self,
        event_id: int,
        *,
        reason: str | None = None,
        cancelled_by: str | None = None,
        cancelled_at: str | None = None,
    ) -> None:
        self.execute(
            """
            UPDATE lfg_events
               SET status = 'cancelled',
                   cancel_reason = COALESCE(?, cancel_reason),
                   cancelled_by = COALESCE(?, cancelled_by),
                   cancelled_at = COALESCE(?, cancelled_at)
             WHERE id = ?
            """,
            (reason, cancelled_by, cancelled_at, event_id),
        )

    def add_lfg_signup(self, event_id: int, discord_id: str) -> bool:
        """Returns True if added, False if already signed up."""
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                'INSERT OR IGNORE INTO lfg_signups (event_id, discord_id) VALUES (?, ?)',
                (event_id, discord_id),
            )
            self.connection.commit()
            return self.cursor.rowcount > 0
        except sqlite3.Error as e:
            debug.error_log(f"add_lfg_signup error: {e}")
            return False

    def remove_lfg_signup(self, event_id: int, discord_id: str) -> bool:
        """Returns True if removed, False if not signed up."""
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                'DELETE FROM lfg_signups WHERE event_id = ? AND discord_id = ?',
                (event_id, discord_id),
            )
            self.connection.commit()
            return self.cursor.rowcount > 0
        except sqlite3.Error as e:
            debug.error_log(f"remove_lfg_signup error: {e}")
            return False

    def fetch_lfg_signups(self, event_id: int) -> list[dict]:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                'SELECT discord_id, signed_at, attended, attendance_marked_at, '
                '       slot_id, signup_kind '
                'FROM lfg_signups WHERE event_id = ? ORDER BY signed_at',
                (event_id,),
            )
            return [dict(row) for row in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"fetch_lfg_signups error: {e}")
            return []

    # ── LFG ↔ Comp linking ─────────────────────────────────────────────────

    def set_lfg_event_comp(self, event_id: int, comp_id: int | None) -> bool:
        """Attach, change, or clear a comp template on an event.

        When the comp changes, all build-slot claims are wiped so nobody is
        stuck on a slot from the previous template.
        """
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "SELECT comp_id FROM lfg_events WHERE id = ?",
                (int(event_id),),
            )
            before = self.cursor.fetchone()
            old_comp_id = before["comp_id"] if before else None
            self.cursor.execute(
                "UPDATE lfg_events SET comp_id = ? WHERE id = ?",
                (comp_id, int(event_id)),
            )
            if str(old_comp_id or "") != str(comp_id or ""):
                self.cursor.execute(
                    "UPDATE lfg_signups SET slot_id = NULL "
                    "WHERE event_id = ?",
                    (int(event_id),),
                )
            self.connection.commit()
            return self.cursor.rowcount >= 0
        except sqlite3.Error as e:
            debug.error_log(f"set_lfg_event_comp error: {e}")
            return False

    def claim_lfg_slot(
        self, event_id: int, discord_id: str, slot_id: int,
    ) -> tuple[bool, str]:
        """Atomically: ensure signup exists, release any previous slot held
        by this user on this event, then claim the requested slot. Returns
        (ok, reason) — reason is 'claimed' / 'already_yours' / 'taken' /
        'not_in_comp' / 'error'."""
        try:
            if not self.connection:
                self.connect()
            # Slot must belong to the event's comp.
            self.cursor.execute(
                "SELECT s.id FROM comp_slots s "
                "JOIN lfg_events e ON e.comp_id = s.comp_id "
                "WHERE e.id = ? AND s.id = ?",
                (int(event_id), int(slot_id)),
            )
            if not self.cursor.fetchone():
                return False, "not_in_comp"
            # Is the slot already held by someone else?
            self.cursor.execute(
                "SELECT discord_id FROM lfg_signups "
                "WHERE event_id = ? AND slot_id = ?",
                (int(event_id), int(slot_id)),
            )
            held = self.cursor.fetchone()
            if held and held["discord_id"] != discord_id:
                return False, "taken"
            if held and held["discord_id"] == discord_id:
                return True, "already_yours"
            # Drop any other slot this user holds on this event.
            self.cursor.execute(
                "UPDATE lfg_signups SET slot_id = NULL "
                "WHERE event_id = ? AND discord_id = ?",
                (int(event_id), discord_id),
            )
            # Upsert signup with the new slot.
            self.cursor.execute(
                "INSERT INTO lfg_signups (event_id, discord_id, slot_id, "
                "                          signup_kind) "
                "VALUES (?, ?, ?, 'main') "
                "ON CONFLICT(event_id, discord_id) DO UPDATE SET "
                "    slot_id = excluded.slot_id, "
                "    signup_kind = 'main'",
                (int(event_id), discord_id, int(slot_id)),
            )
            self.connection.commit()
            return True, "claimed"
        except sqlite3.Error as e:
            debug.error_log(f"claim_lfg_slot error: {e}")
            try:
                self.connection.rollback()
            except sqlite3.Error:
                pass
            return False, "error"

    def release_lfg_slot(self, event_id: int, discord_id: str) -> bool:
        """Drop the user's slot claim but leave them on the roster as a
        generic signup. Returns True if a slot was released."""
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "UPDATE lfg_signups SET slot_id = NULL "
                "WHERE event_id = ? AND discord_id = ? "
                "      AND slot_id IS NOT NULL",
                (int(event_id), discord_id),
            )
            self.connection.commit()
            return self.cursor.rowcount > 0
        except sqlite3.Error as e:
            debug.error_log(f"release_lfg_slot error: {e}")
            return False

    def fetch_lfg_slot_grid(self, event_id: int) -> list[dict]:
        """Return rows: every comp slot for the event's comp, joined with
        its current claimant (if any). Empty list if the event has no comp.
        Each row: slot_id, slot_order, role, weapon, build_type, ip_min,
        required, notes, claimed_by (discord_id or NULL)."""
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "SELECT s.id AS slot_id, s.slot_order, s.role, s.weapon, "
                "       s.build_type, s.head, s.chest, s.shoes, s.offhand, "
                "       s.cape, s.mount, s.food, s.potion, s.ip_min, "
                "       s.required, s.notes, s.swaps, "
                "       (SELECT su.discord_id FROM lfg_signups su "
                "          WHERE su.event_id = ? AND su.slot_id = s.id "
                "          LIMIT 1) AS claimed_by "
                "FROM lfg_events e "
                "JOIN comp_slots s ON s.comp_id = e.comp_id "
                "WHERE e.id = ? "
                "ORDER BY s.slot_order ASC, s.id ASC",
                (int(event_id), int(event_id)),
            )
            return [dict(r) for r in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"fetch_lfg_slot_grid error: {e}")
            return []

    # ── Attendance ──────────────────────────────────────────────────────────

    def set_signup_attendance(
        self, event_id: int, discord_id: str, attended: bool,
    ) -> bool:
        """Record a signup's attendance flag.

        If the user wasn't signed up, inserts a synthetic signup row so the
        attendance is still tracked. Returns True if any row was written.
        """
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute('''
                INSERT INTO lfg_signups (event_id, discord_id, attended, attendance_marked_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(event_id, discord_id) DO UPDATE SET
                    attended = excluded.attended,
                    attendance_marked_at = CURRENT_TIMESTAMP
            ''', (event_id, discord_id, 1 if attended else 0))
            self.connection.commit()
            return True
        except sqlite3.Error as e:
            debug.error_log(f"set_signup_attendance error: {e}")
            return False

    def fetch_event_attendance(self, event_id: int) -> dict:
        """Return attendance counts for an event: signed/attended/unmarked."""
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute('''
                SELECT
                    COUNT(*)                                  AS signed,
                    SUM(CASE WHEN attended = 1 THEN 1 ELSE 0 END) AS attended,
                    SUM(CASE WHEN attended IS NULL THEN 1 ELSE 0 END) AS unmarked
                FROM lfg_signups WHERE event_id = ?
            ''', (event_id,))
            row = self.cursor.fetchone()
            if not row:
                return {"signed": 0, "attended": 0, "unmarked": 0}
            return {
                "signed":   int(row["signed"]   or 0),
                "attended": int(row["attended"] or 0),
                "unmarked": int(row["unmarked"] or 0),
            }
        except sqlite3.Error as e:
            debug.error_log(f"fetch_event_attendance error: {e}")
            return {"signed": 0, "attended": 0, "unmarked": 0}

    def fetch_attendance_trend(self, since_iso: str) -> list[dict]:
        """Per-event attendance summary for events starting at/after ``since_iso``.

        Only includes events where at least one signup was marked attended.
        """
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute('''
                SELECT
                    e.id           AS event_id,
                    e.title        AS title,
                    e.event_type   AS event_type,
                    e.starts_at    AS starts_at,
                    SUM(CASE WHEN s.attended = 1 THEN 1 ELSE 0 END) AS attended,
                    COUNT(s.id)    AS signed
                FROM lfg_events e
                LEFT JOIN lfg_signups s ON s.event_id = e.id
                WHERE e.starts_at >= ? AND e.status != 'cancelled'
                GROUP BY e.id
                HAVING attended > 0
                ORDER BY e.starts_at ASC
            ''', (since_iso,))
            return [dict(row) for row in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"fetch_attendance_trend error: {e}")
            return []

    # ── Cohort retention ────────────────────────────────────────────────────

    def fetch_cohort_retention(
        self, weeks_back: int = 8, retention_weeks: int = 8,
    ) -> dict:
        """For each registration cohort (week of ``verified_date``), compute
        the fraction of members showing positive fame movement in each
        subsequent week.

        Returns ``{cohort_iso_week: {0: total, 1: active_w1, 2: active_w2, ...}}``
        where week 0 is cohort size. Heavyweight query — call sparingly.
        """
        try:
            if not self.connection:
                self.connect()

            # Get cohorts: discord_id → cohort week (Monday of verified_date).
            self.cursor.execute('''
                SELECT discord_id, verified_date
                FROM user_profiles
                WHERE verified_date IS NOT NULL
                  AND verified_date >= date('now', ?)
            ''', (f'-{weeks_back * 7} days',))
            cohort_rows = self.cursor.fetchall()
            if not cohort_rows:
                return {}

            cohort_of: dict[str, str] = {}
            cohorts: dict[str, list[str]] = defaultdict(list)
            for row in cohort_rows:
                did = row["discord_id"]
                vd = row["verified_date"]
                # Bucket to ISO Monday for stable week labels.
                try:
                    dt = _datetime.datetime.fromisoformat(vd.replace("Z", "+00:00"))
                except Exception:
                    continue
                monday = dt.date() - _datetime.timedelta(days=dt.weekday())
                key = monday.isoformat()
                cohort_of[did] = key
                cohorts[key].append(did)

            # For each (player, week-index), did they have any positive
            # delta in kill_fame / pve_total / gather_all?  Compute via
            # consecutive snapshots: a row is "active" if its metric is
            # higher than the previous snapshot for the same player.
            self.cursor.execute('''
                WITH lagged AS (
                    SELECT
                        discord_id,
                        recorded_at,
                        CASE WHEN
                            kill_fame  > COALESCE(LAG(kill_fame)  OVER w, kill_fame)
                         OR pve_total  > COALESCE(LAG(pve_total)  OVER w, pve_total)
                         OR gather_all > COALESCE(LAG(gather_all) OVER w, gather_all)
                        THEN 1 ELSE 0 END AS was_active
                    FROM player_stats_history
                    WINDOW w AS (PARTITION BY discord_id ORDER BY recorded_at)
                )
                SELECT discord_id,
                       date(recorded_at, 'weekday 0', '-6 days') AS week_monday,
                       MAX(was_active) AS active
                FROM lagged
                GROUP BY discord_id, week_monday
            ''')
            active_rows = self.cursor.fetchall()

            active_set: set[tuple[str, str]] = set()
            for r in active_rows:
                if int(r["active"] or 0) == 1:
                    active_set.add((r["discord_id"], r["week_monday"]))

            # Build output. Week 0 = cohort total, weeks 1..N = active counts.
            out: dict[str, dict[int, int]] = {}
            for cohort_key, members in cohorts.items():
                cohort_dt = _datetime.date.fromisoformat(cohort_key)
                bucket = {0: len(members)}
                for offset in range(1, retention_weeks + 1):
                    target = (cohort_dt + _datetime.timedelta(weeks=offset)).isoformat()
                    bucket[offset] = sum(
                        1 for m in members if (m, target) in active_set
                    )
                out[cohort_key] = bucket
            return out
        except sqlite3.Error as e:
            debug.error_log(f"fetch_cohort_retention error: {e}")
            return {}

