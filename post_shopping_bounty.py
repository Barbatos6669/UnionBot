"""Create/refresh the consolidated 'Crafting Shopping List' bounty.

Reads top-N rows from ``data/shopping_list.tsv``, ensures a row exists
in ``bounties``, populates the ``bounty_shopping_items`` line-item
table, and posts (or edits) the bounty embed in the bounty-board
channel with the interactive **Items & Materials** button so members
can claim individual rows.

Usage:
    python post_shopping_bounty.py                  # create new
    python post_shopping_bounty.py --bounty-id 21   # backfill existing
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
from urllib import request as urlrequest
from urllib.error import HTTPError

from dotenv import load_dotenv

from cogs._materials import (
    DEFAULT_SERVICE_FEE,
    estimate_line_reward,
)
from sql_database import Database


SHOPPING_TSV = "data/shopping_list.tsv"
TOP_N = 20
SERVICE_FEE = DEFAULT_SERVICE_FEE  # flat per claimed item (silver)
DEADLINE_DAYS = 7
TITLE = "🧰 Crafting shopping list — top 20"


def _load_top(path: str, n: int) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    rows.sort(key=lambda r: int(r["buy_more"]), reverse=True)
    return rows[:n]


def _build_description(rows: list[dict], bounty_id: int) -> str:
    lines = [
        "The logistician needs gear. Pick anything from this list, "
        "craft it, and drop it off in the guild bank.",
        "",
        "**Top crafting deficits (from recent member deaths):**",
        "```",
        f"{'Need':>4}  {'Q':>1}  {'E':>1}  {'Payout':>10}  Item",
    ]
    for r in rows:
        unit, items_total, fee = estimate_line_reward(
            r["item_id"],
            count=int(r.get("buy_more") or 1),
            quality=int(r.get("avg_quality") or 1),
            enchant=int(r.get("enchant") or 0),
        )
        payout = items_total + fee
        lines.append(
            f"{int(r['buy_more']):4d}  {r['avg_quality']:>1}  "
            f"{r['enchant']:>1}  {payout:>10,}  {r['name']}"
        )
    lines.append("```")
    lines.append(
        "Payout per row = **unit price × quantity + service fee "
        f"({SERVICE_FEE:,} silver)**.  Quality (Q) and Enchant (E) shown "
        "are the *average* members actually died wearing — match those "
        "where possible.\n\n"
        "👉 Hit **📋 Items & Materials** below to pick a row, see a "
        "material estimate + your payout, and call dibs so two crafters "
        "don't double up."
    )
    return "\n".join(lines)


def _build_components(bounty_id: int) -> list[dict]:
    return [{
        "type": 1,
        "components": [{
            "type": 2,
            "style": 1,
            "label": "Items & Materials",
            "emoji": {"name": "📋"},
            "custom_id": f"shopping:open:{bounty_id}",
        }],
    }]


def _build_embed(
    bounty_id: int, description: str, deadline: str, reward_total: int,
) -> dict:
    return {
        "title": f"Bounty #{bounty_id} — {TITLE}",
        "description": description,
        "color": 0xE5A100,
        "fields": [
            {"name": "Total payout pool",
             "value": f"🪙 **{reward_total:,}** silver",
             "inline": True},
            {"name": "Deadline",
             "value": f"`{deadline} UTC`",
             "inline": True},
            {"name": "Status",
             "value": f"🟢 Open — claim individual items below for their "
                      f"own payout, or take the whole bounty via "
                      f"`/bounty claim id:{bounty_id}`.",
             "inline": False},
        ],
        "footer": {
            "text": "Auto-generated from recent guild deaths · prices are "
                    "heuristic; logistician may tip more for rares.",
        },
    }


def _http_json(
    url: str, token: str, payload: dict, *, method: str = "POST",
) -> dict:
    req = urlrequest.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "UnionBot (shopping-list, 1.1)",
        },
        method=method,
    )
    try:
        with urlrequest.urlopen(req, timeout=20) as resp:
            data = resp.read().decode("utf-8")
            return json.loads(data) if data else {}
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(
            f"Discord API {method} {url} -> {exc.code}: {body}"
        ) from exc


def _seed_shopping_items(
    db: Database, bounty_id: int, rows: list[dict],
) -> int:
    """Replace the *unclaimed* line items for this bounty with the
    current TSV rows. Returns the total reward pool (silver)."""
    db.cursor.execute(
        "DELETE FROM bounty_shopping_items "
        "WHERE bounty_id = ? AND (claimed_by IS NULL OR claimed_by = '')",
        (bounty_id,),
    )
    total_pool = 0
    for idx, r in enumerate(rows):
        needed = int(r.get("buy_more") or 1)
        quality = int(r.get("avg_quality") or 1)
        enchant = int(r.get("enchant") or 0)
        unit, items_total, fee = estimate_line_reward(
            r["item_id"], count=needed, quality=quality, enchant=enchant,
            service_fee=SERVICE_FEE,
        )
        total_pool += items_total + fee
        try:
            db.cursor.execute(
                "INSERT OR IGNORE INTO bounty_shopping_items "
                "(bounty_id, line_index, item_id, name, quality, enchant, "
                " needed, unit_reward, service_fee) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    bounty_id, idx,
                    (r["item_id"] or "").upper(),
                    r["name"],
                    quality, enchant, needed, unit, fee,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            print(f"WARN: failed to seed line {idx}: {exc!r}", file=sys.stderr)
    db.connection.commit()
    return total_pool


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--bounty-id", type=int, default=None,
        help="Backfill an existing bounty (edit its message in place).",
    )
    args = parser.parse_args()

    load_dotenv()
    token = os.getenv("DISCORD_TOKEN") or os.getenv("token") or ""
    if not token:
        print("ERROR: DISCORD_TOKEN missing from .env", file=sys.stderr)
        return 1

    db = Database("data/database.db")
    db.connect()
    # Make sure the line-item table exists even if the bot hasn't run yet.
    db.initialize_bounty_shopping_items_table()

    rows = _load_top(SHOPPING_TSV, TOP_N)
    if not rows:
        print(f"ERROR: no rows in {SHOPPING_TSV}", file=sys.stderr)
        return 1

    deadline = (
        dt.datetime.now(dt.UTC) + dt.timedelta(days=DEADLINE_DAYS)
    ).strftime("%Y-%m-%d %H:%M:%S")

    if args.bounty_id is None:
        channel_id = db.get_config("bounty_board_channel_id")
        if not channel_id:
            print("ERROR: bounty_board_channel_id not configured.",
                  file=sys.stderr)
            return 1

        bot_user_id = db.get_config("bot_user_id") or "system"
        db.cursor.execute(
            "INSERT INTO bounties "
            "(title, description, reward_points, posted_by, deadline, status) "
            "VALUES (?, ?, ?, ?, ?, 'open')",
            (TITLE, "(placeholder)",
             0, str(bot_user_id), deadline),
        )
        db.connection.commit()
        bounty_id = int(db.cursor.lastrowid or 0)
        if not bounty_id:
            print("ERROR: failed to insert bounty row.", file=sys.stderr)
            return 1
        print(f"Inserted bounty #{bounty_id}")

        reward_pool = _seed_shopping_items(db, bounty_id, rows)
        description = _build_description(rows, bounty_id)
        db.cursor.execute(
            "UPDATE bounties SET description = ?, reward_points = ? "
            "WHERE id = ?",
            (description, reward_pool, bounty_id),
        )
        db.connection.commit()

        payload = {
            "embeds": [_build_embed(bounty_id, description, deadline,
                                    reward_pool)],
            "components": _build_components(bounty_id),
        }
        url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
        resp = _http_json(url, token, payload, method="POST")
        message_id = resp.get("id")
        if message_id:
            print(f"Posted to channel {channel_id}, message {message_id}")
            db.cursor.execute(
                "UPDATE bounties SET channel_id = ?, message_id = ? "
                "WHERE id = ?",
                (str(channel_id), str(message_id), bounty_id),
            )
            db.connection.commit()
        else:
            print("WARN: no message id returned:", resp, file=sys.stderr)

    else:
        bounty_id = args.bounty_id
        db.cursor.execute(
            "SELECT id, channel_id, message_id, deadline "
            "FROM bounties WHERE id = ?",
            (bounty_id,),
        )
        row = db.cursor.fetchone()
        if not row:
            print(f"ERROR: no bounty #{bounty_id}", file=sys.stderr)
            return 1
        b = dict(row)
        channel_id = b["channel_id"]
        message_id = b["message_id"]
        if not channel_id or not message_id:
            print("ERROR: bounty has no channel_id/message_id; cannot edit.",
                  file=sys.stderr)
            return 1
        deadline = b["deadline"] or deadline

        reward_pool = _seed_shopping_items(db, bounty_id, rows)

        description = _build_description(rows, bounty_id)
        db.cursor.execute(
            "UPDATE bounties SET description = ?, reward_points = ? "
            "WHERE id = ?",
            (description, reward_pool, bounty_id),
        )
        db.connection.commit()

        payload = {
            "embeds": [_build_embed(bounty_id, description, deadline,
                                    reward_pool)],
            "components": _build_components(bounty_id),
        }
        url = (
            f"https://discord.com/api/v10/channels/{channel_id}/"
            f"messages/{message_id}"
        )
        _http_json(url, token, payload, method="PATCH")
        print(f"Patched bounty #{bounty_id} message {message_id} "
              f"in channel {channel_id} with {len(rows)} items.")

    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
