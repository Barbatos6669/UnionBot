"""
Minimal `config.py` placeholders — add real values as you need them.
This file contains lightweight defaults so old imports don't crash.
"""
import datetime
import os

guild_name = os.getenv("HOME_GUILD_NAME", "HomeGuild")
home_guild_role_name = os.getenv("HOME_GUILD_ROLE_NAME", guild_name)
home_guild_nick_tag = os.getenv("HOME_GUILD_NICK_TAG", "HG")
home_alliance_tag = os.getenv("HOME_ALLIANCE_TAG", "ALLY")

# Uppercase aliases are easier to import from cogs without implying mutability.
HOME_GUILD_NAME = guild_name
HOME_GUILD_ROLE_NAME = home_guild_role_name
HOME_GUILD_NICK_TAG = home_guild_nick_tag
HOME_ALLIANCE_TAG = home_alliance_tag

# Optional: server/guild identifiers (set via env vars instead when possible).
# Prefer setting GUILD_DISCORD_ID in .env; leave this at None for production.
guild_discord_id = None
alliance_name = None
alliance_id = None

# Paths / logging
config_database_path = "data/database.db"
log_file_path = "data/bot.log"
log_level = "INFO"
debug_mode = False

# Channel / feature placeholders
public_chat = None
rules = None
announcements = None
guild_info = None
faq = None
recruitment = None
content_pings = None
events = None
Lobby = None

# ── Role constants ────────────────────────────────────────────────────────────
# Roles managed automatically by the lifecycle system.
#   Recruit/Probationary/Member/Veteran/Inactive — earned by being in the home guild
#   Alumni   — was in the home guild, no longer is
#   Alliance — never in the home guild but currently in a guild that shares the home alliance
#   Guest    — verified in Discord but neither in the home guild nor the home alliance
LIFECYCLE_ROLES = ["Recruit", "Probationary", "Member", "Veteran", "Inactive", "Alumni", "Alliance", "Guest"]

# Roles assigned by the staff-application system (officer review + auto rebalance).
# Order matters: highest-authority first. Used for display and demotion priority.
STAFF_ROLES = ["Captain", "Officer", "Steward", "Holdmaster", "Logistician", "Crafter", "Refiner", "Guild Farmer", "Gatherer", "Senior Shotcaller", "Shotcaller", "Recruiter"]

# Per-rank rules:
#   eligible      — lifecycle roles allowed to apply
#   per_slot      — default "guild members per slot" (1 slot per N members)
#   max_cap       — default hard cap on slot count regardless of guild size
#   prereq_role   — (optional) staff rank the applicant must currently hold
#   prereq_days   — (optional) minimum days they must have served in prereq_role
# These defaults can be overridden at runtime via /staff config.
STAFF_TIERS = {
    "Captain":           {"eligible": ("Veteran",),          "per_slot": 50, "max_cap": 4,
                          "prereq_role": "Officer", "prereq_days": 30},
    "Officer":           {"eligible": ("Veteran",),          "per_slot": 25, "max_cap": 8},
    "Steward":           {"eligible": ("Veteran",),          "per_slot": 40, "max_cap": 4},
    "Holdmaster":        {"eligible": ("Member", "Veteran"), "per_slot": 1, "max_cap": 5},
    "Logistician":       {"eligible": ("Member", "Veteran"), "per_slot": 1, "max_cap": 2},
    "Crafter":           {"eligible": ("Member", "Veteran"), "per_slot": 1, "max_cap": 4},
    "Refiner":           {"eligible": ("Member", "Veteran"), "per_slot": 1, "max_cap": 4},
    "Guild Farmer":      {"eligible": ("Member", "Veteran"), "per_slot": 1, "max_cap": 6},
    "Gatherer":          {"eligible": ("Member", "Veteran"), "per_slot": 1, "max_cap": 5},
    "Senior Shotcaller": {"eligible": ("Veteran",),          "per_slot": 40, "max_cap": 5,
                          "prereq_role": "Shotcaller", "prereq_days": 30},
    # Shotcaller is open to any lifecycle — call-out ability is independent of tenure.
    "Shotcaller":        {"eligible": tuple(LIFECYCLE_ROLES), "per_slot": 15, "max_cap": 10},
    "Recruiter":         {"eligible": ("Member", "Veteran"), "per_slot": 30, "max_cap": 6},
}

# Public-facing job descriptions used by the staff application board.
STAFF_DESCRIPTIONS = {
    "Captain": {
        "purpose": "Senior leadership / department head. Helps run the guild under the Commander and manages large areas of responsibility.",
        "responsibilities": [
            "Leads a major department (PvP, PvE, Economy, Recruitment, or Membership).",
            "Manages Officers under their area.",
            "Reports problems and progress to the Commander.",
            "Helps enforce rules and guild standards.",
            "Can make leadership decisions when the Commander is unavailable.",
            "Helps train future leaders.",
        ],
        "expected": "Reliable, active, mature, and able to manage people without power-tripping.",
    },
    "Officer": {
        "purpose": "Guild manager / operational leader. Handles day-to-day guild needs and supports Captains.",
        "responsibilities": [
            "Helps organize events and guild activity.",
            "Enforces guild rules.",
            "Assists with member questions, problems, and disputes.",
            "Supports recruitment, onboarding, content, or logistics.",
            "Reports inactive, toxic, or problematic members to leadership.",
            "Helps keep Discord and in-game guild systems organized.",
        ],
        "expected": "Helpful, consistent, respectful, and active in guild operations.",
    },
    "Steward": {
        "purpose": "Roster custodian. Keeps the guild membership healthy by pruning inactive members and maintaining clean records.",
        "responsibilities": [
            "Reviews the inactive-member list weekly and prunes per guild policy.",
            "Coordinates with Officers before kicking long-tenured or borderline cases.",
            "Maintains accurate nicknames, Albion names, and lifecycle role assignments.",
            "Flags suspicious or duplicate accounts to leadership.",
            "Handles re-entry requests from Alumni and former members.",
            "Keeps the audit log and strike history tidy.",
        ],
        "expected": "Detail-oriented, fair, communicates clearly before removals, and never prunes in anger.",
    },
    "Holdmaster": {
        "purpose": "Keeper of one of the guild's five islands. Assigned a specific island to build up, maintain, and optimize for the guild's economy.",
        "responsibilities": [
            "Builds and upgrades structures on the assigned island (laborer housing, refining stations, crafting stations).",
            "Plans the island's layout for the guild's current focus (laborer cycles, refining throughput, etc.).",
            "Keeps laborer journals fed and rotates them on schedule.",
            "Coordinates with other Holdmasters so the five islands complement each other instead of duplicating effort.",
            "Reports silver/material needs to Officers before placing major builds.",
            "Hands over the island in good order if reassigned or stepping down.",
        ],
        "expected": "Patient, consistent, plans ahead, and treats the island as guild property \u2014 not personal storage.",
    },    "Logistician": {
        "purpose": "Supply-chain coordinator. Owns the full pipeline from raw gather → refined material → crafted item, and makes sure the guild has what it needs for upcoming content.",
        "responsibilities": [
            "Tracks projected demand for guild content (regear sets, comp gear, consumables).",
            "Issues focus orders to Gatherers, Refiners, and Crafters based on what's missing.",
            "Owns the guild stockpile count and reports shortages to Officers before content.",
            "Coordinates with Holdmasters on what laborers/refining the islands should prioritize.",
            "Approves bulk material withdrawals and crafting drives.",
            "Closes the loop — confirms finished gear actually lands in regear/guild bank.",
        ],
        "expected": "Organized, communicative, comfortable with spreadsheets, plans a week ahead.",
    },
    "Crafter": {
        "purpose": "Approved guild crafter. Runs crafting drives for the orders the Logistician assigns.",
        "responsibilities": [
            "Picks up assigned crafting orders from the Logistician.",
            "Uses guild focus / crafting bonuses responsibly (no wasting on personal gear).",
            "Reports finished item counts back to the Logistician.",
            "Flags missing materials early so Refiners can catch up.",
            "Maintains a crafting spec the guild actually needs (regear staples first, niche specs second).",
            "Hands finished items to the guild bank, not personal storage.",
        ],
        "expected": "Reliable, doesn't hoard focus, communicates progress, and follows the order list.",
    },
    "Refiner": {
        "purpose": "Refines the guild's raw resources and helps members level their own refining with guild rss.",
        "responsibilities": [
            "Refines incoming raw materials on a regular cadence.",
            "Coordinates with members who want to spec refining — lends guild rss in return for refined product.",
            "Tracks refining return rates and reports notable bonuses/losses.",
            "Keeps refined stock organized in the guild bank.",
            "Flags material shortages to the Logistician and Gatherers.",
            "Doesn't refine personal gear with guild rss without approval.",
        ],
        "expected": "Honest with focus/rss usage, consistent, willing to share spec-up opportunities.",
    },
    "Guild Farmer": {
        "purpose": "Guild island farmer. Maintains farms, animals, herbs, and food/potion inputs that support guild economy and content.",
        "responsibilities": [
            "Runs assigned guild island farming cycles on schedule.",
            "Grows crops, herbs, or animals prioritized by the Logistician and crafting needs.",
            "Tracks seed, baby animal, nutrition, and output counts clearly.",
            "Deposits finished farm output into the correct guild storage instead of personal storage.",
            "Flags shortages early so Crafters, Cooks, and Logistician can plan around them.",
            "Helps keep food, potion, and mount-related supply chains moving for guild content.",
        ],
        "expected": "Consistent, honest with guild assets, comfortable with routine upkeep, and willing to report numbers.",
    },
    "Gatherer": {
        "purpose": "Guild gatherer. Directs gathering focus toward what the Refiners actually need.",
        "responsibilities": [
            "Gathers materials prioritized by the Logistician / Refiners (not whatever is convenient).",
            "Reports map zones, hotspots, and competition to the gathering channel.",
            "Donates gathered raws to the guild bank on a regular cadence.",
            "Helps organize group gather runs when bulk material is needed.",
            "Avoids dying to gankers with full bags — use mules and travel smart.",
            "Reports threats / red-zone activity to leadership.",
        ],
        "expected": "Active, situationally aware, follows the priority list instead of free-styling.",
    },    "Senior Shotcaller": {
        "purpose": "Trusted combat leader. Leads important fights and trains other shotcallers.",
        "responsibilities": [
            "Leads major PvP content (BZ roams, faction warfare, outposts, objectives, defenses).",
            "Controls comms during fights.",
            "Makes engage, reset, retreat, and target calls.",
            "Reviews fights after content.",
            "Helps train Shotcallers.",
            "Works with leadership to improve guild comps and combat standards.",
        ],
        "expected": "Calm under pressure, clear on comms, accepts mistakes, and does not rage-blame members.",
    },
    "Shotcaller": {
        "purpose": "Approved content caller. Leads guild groups in PvP or structured content.",
        "responsibilities": [
            "Hosts and leads approved content.",
            "Calls movement, engages, retreats, and regroups.",
            "Keeps the party organized.",
            "Follows guild comp and comms standards.",
            "Reports results, issues, and member performance when needed.",
            "Works toward becoming a Senior Shotcaller.",
        ],
        "expected": "Clear voice, controlled attitude, good communication, and willingness to learn.",
    },
    "Recruiter": {
        "purpose": "Growth and onboarding support. Brings new members into the guild and helps them get settled.",
        "responsibilities": [
            "Recruits players who fit the guild's standards.",
            "Explains basic rules, Discord setup, and guild expectations.",
            "Helps new members find content and roles.",
            "Tracks trial members or new recruits.",
            "Reports promising members to Officers.",
            "Reports red flags, drama, or inactive recruits.",
        ],
        "expected": "Friendly, honest, patient, and careful with who they invite.",
    },
}


def derive_lifecycle(since_date_str, probationary_days: int = 30, member_days: int = 90) -> str:
    """Return the lifecycle role a member should hold based on time elapsed since `since_date_str`.

    Callers typically pass the Discord `member.joined_at` so lifecycle reflects time in the server.
    """
    if not since_date_str:
        return "Probationary"
    since = datetime.datetime.fromisoformat(since_date_str)
    if since.tzinfo is None:
        since = since.replace(tzinfo=datetime.UTC)
    days = (datetime.datetime.now(datetime.UTC) - since).days
    if days >= member_days:
        return "Veteran"
    if days >= probationary_days:
        return "Member"
    return "Probationary"
