"""Tests for misc pure helpers across cogs:

* ``cogs.regear._split_enchant`` — Albion ``T7_HEAD@3`` → (``T7_HEAD``, 3)
* ``cogs.applications._compute_fame`` — total/combat fame breakdown
* ``cogs._bounties_config.fmt_silver`` — compact silver formatting
* ``cogs.duties._period_key`` — daily / weekly / once dedup keys
* ``cogs.dashboard`` health helpers — defensive scoring for guild dashboards
* ``cogs._lfg_helpers`` — prime-slot date math
* ``cogs._primetime_claims`` — prime-slot dashboard window helpers
* ``cogs._content_config`` — content planning helper labels
* ``cogs.market`` — route-risk labels for transport recommendations
* ``cogs.ai_assistant`` — cheap Albion glossary answers
* ``cogs.help`` — Discord embed size guardrails
"""

from __future__ import annotations

import datetime as dt

import discord

from cogs._bounties_config import bounty_needs_payment, fmt_silver
from cogs._bounties_roads import (
    ROAD_CORE_REWARDS,
    image_attachment_url,
    normalize_road_core_color,
    parse_road_core_price,
    parse_road_core_proof,
    road_core_proof_text,
    road_core_title,
)
from cogs._content_config import (
    availability_recommendation_keys,
    availability_content_recommendations,
    daily_timer_availability_due,
    daily_timer_slot_windows,
    daily_timer_target_date,
    daily_timer_vote_due,
    parse_availability_slots,
    ranked_available_timer_indexes,
    season_point_focus_recommendation_keys,
)
from cogs.content_roles import _parse_config_channel_id
from cogs._content_views import _availability_slot_heading, _availability_timer_window
from cogs._event_report_regear import suppressed_auto_regear_lines
from cogs._lfg_config import PrimeSlot, display_slot_label, prime_slot_display_label
from cogs._lfg_helpers import (
    _event_access_role_name,
    _event_voice_channel_name,
    _event_voice_overwrites,
    _extract_ip_requirement,
    _normalize_ip_requirement,
    _next_occurrence,
    _slot_occurrence_on_date,
)
from cogs.lfg import _config_enabled_from_db
from cogs._lfg_views import _claim_fields_for_schedule, _parse_general_lfg_schedule
from cogs._nickname_tags import (
    extract_tagged_nickname_name,
    strip_managed_nickname_tag,
    tagged_nickname_for_profile,
)
from cogs._primetime_claims import (
    _claim_window_bounds,
    _format_day_field_name,
    _lfg_message_url,
    _linked_event_title,
    _slot_display_label,
    _slot_key_from_label,
    normalize_claim_window,
)
from cogs.ai_assistant import (
    KNOWLEDGE_FILE_HINTS,
    _discord_timestamp,
    _knowledge_phrases,
    _knowledge_retrieval_preview,
    _knowledge_source_tier_weight,
    _knowledge_tokens,
    _make_knowledge_section,
    _markdown_knowledge_sections,
    _question_contains,
    _quick_albion_answer,
    _quick_workflow_answer,
    _rank_knowledge_sections,
    _score_knowledge_section,
    _utc_hour_window,
    _weekday_name_sqlite,
)
from cogs.automation import (
    _collect_stale_unverified_role_members,
    _collect_unverified_kick_targets,
)
from cogs.utc_clock import _utc_clock_name
from cogs.applications import _compute_fame
from cogs.dashboard import _health_emoji, _pct, _queue_score, _safe_int, _score_from_pct
from cogs.duties import _period_key
from cogs.help import _can_add_field, _embed_text_size
from cogs.market import _route_risk
from cogs.openai_moderation import _moderation_threshold_decision
from cogs.regear import _split_enchant
from cogs.voice import _member_has_registered_voice_access


class _NickTagDb:
    def __init__(self) -> None:
        self.config = {
            "home_alliance_id": "home-alliance",
            "home_alliance_tag": "UoT",
            "member_nickname_home_tag": "TU",
            "member_nickname_guild_tags": '{"custom-guild": "CG"}',
        }
        self.guilds = {
            "guild-home": {
                "guild_id": "guild-home",
                "guild_name": "HomeGuild",
                "alliance_id": "home-alliance",
                "alliance_tag": "UoT",
            },
            "guild-divine": {
                "guild_id": "guild-divine",
                "guild_name": "Divine Departure",
                "alliance_id": "home-alliance",
                "alliance_tag": "UoT",
            },
            "custom-guild": {
                "guild_id": "custom-guild",
                "guild_name": "Some Complicated Guild Name",
                "alliance_id": "home-alliance",
                "alliance_tag": "UoT",
            },
            "guild-other": {
                "guild_id": "guild-other",
                "guild_name": "Burnr Friends",
                "alliance_id": "other-alliance",
                "alliance_tag": "BURNR",
            },
        }

    def get_config(self, key: str) -> str | None:
        return self.config.get(key)

    def fetch_guild(self, guild_id: str):
        return self.guilds.get(guild_id)

    def fetch_all_guilds(self):
        return list(self.guilds.values())


class _Role:
    def __init__(self, name: str) -> None:
        self.name = name


class _Perms:
    administrator = False
    manage_guild = False
    move_members = False


class _Member:
    def __init__(self, *role_names: str) -> None:
        self.roles = [_Role(name) for name in role_names]
        self.guild_permissions = _Perms()


class _VoiceAccessDb:
    def __init__(self, extra_roles: str = "") -> None:
        self.extra_roles = extra_roles

    def get_config(self, key: str) -> str | None:
        if key == "voice_extra_access_roles":
            return self.extra_roles
        return None


class _KickRole:
    def __init__(self, name: str) -> None:
        self.name = name
        self.members = []


class _KickMember:
    bot = False

    def __init__(
        self,
        *roles: _KickRole,
        joined_days_ago: int = 8,
        name: str = "member",
    ) -> None:
        self.name = name
        self.roles = list(roles)
        self.guild_permissions = _Perms()
        self.joined_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
            days=joined_days_ago,
        )
        for role in roles:
            role.members.append(self)

    def __str__(self) -> str:
        return self.name


class _KickGuild:
    owner = object()

    def __init__(self, roles: list[_KickRole]) -> None:
        self.roles = roles


# ── ai assistant quick answers ────────────────────────────────────────────


def test_quick_albion_answers_common_terms_without_model_call() -> None:
    assert "anti-dogpile" in (_quick_albion_answer("what is focus fire?") or "")
    assert "overcharge" in (_quick_albion_answer("what does oc mean?") or "").lower()
    assert "Resource Return Rate" in (_quick_albion_answer("what does rrr mean?") or "")
    assert "Item Power" in (_quick_albion_answer("what is IP?") or "")
    tank_build = _quick_albion_answer("What's a good ZvZ build for a frontline tank right now?") or ""
    assert "ZvZ frontline" in tank_build
    assert "Judicator" in tank_build
    assert "posted comp" in tank_build


def test_quick_albion_answer_ignores_bot_workflow_questions() -> None:
    assert _quick_albion_answer("how do i register?") is None
    assert _quick_albion_answer("how do i create an lfg?") is None


def test_quick_workflow_answer_routes_registration_and_lfg() -> None:
    channels = {
        "registration": "<#register>",
        "event_board": "<#event-board>",
        "lfg_posts": "<#lfg>",
    }
    registration = _quick_workflow_answer("how do i register?", channels) or ""
    assert "<#register>" in registration
    assert "screenshot" in registration

    lfg = _quick_workflow_answer("how do i create an lfg?", channels) or ""
    assert "<#event-board>" in lfg
    assert "<#lfg>" in lfg
    assert "SSO" not in lfg


def test_quick_workflow_answer_routes_weapon_roles() -> None:
    channels = {
        "content_roles": "<#content-roles>",
        "weapon_roles": "<#weapon-roles>",
    }

    answer = _quick_workflow_answer("where do i pick weapon roles?", channels) or ""
    assert "<#weapon-roles>" in answer
    assert "comp" in answer.lower()
    assert "pings" in answer.lower()


def test_quick_workflow_answer_ignores_albion_terms() -> None:
    assert _quick_workflow_answer("what is focus fire?", {}) is None


def test_ai_knowledge_sections_score_specific_heading_over_intro() -> None:
    content = """# Guide

Generic intro that talks about the server and nothing important.

## Registration rescue

If a member posts a screenshot early, tell them to click Register again and
upload the character screen when the bot asks.

## Market

Use buy orders and sell orders.
"""
    sections = _markdown_knowledge_sections("registration.md", content)
    query_tokens = _knowledge_tokens("I posted my screenshot before registering")
    query_phrases = _knowledge_phrases("I posted my screenshot before registering")
    scored = sorted(
        (
            _score_knowledge_section(
                filename=filename,
                heading=heading,
                text=text,
                query_tokens=query_tokens,
                query_phrases=query_phrases,
            ),
            heading,
            text,
        )
        for filename, heading, text in sections
    )
    assert scored[-1][1] == "Registration rescue"
    assert "click Register again" in scored[-1][2]


def test_ai_knowledge_source_tier_is_only_a_relevance_tiebreaker() -> None:
    official = """Source tier: A

Official patch notes say to verify current values before answering exact
weapon numbers.
"""
    weak = """Source tier: D

Someone said exact weapon numbers are always the same.
"""
    unrelated = """Source tier: A

Registration screenshots go in the registration channel.
"""
    query_tokens = _knowledge_tokens("current weapon numbers patch notes")
    query_phrases = _knowledge_phrases("current weapon numbers patch notes")

    official_score = _score_knowledge_section(
        filename="official.md",
        heading="Patch notes",
        text=official,
        query_tokens=query_tokens,
        query_phrases=query_phrases,
    )
    weak_score = _score_knowledge_section(
        filename="weak.md",
        heading="Patch notes",
        text=weak,
        query_tokens=query_tokens,
        query_phrases=query_phrases,
    )
    unrelated_score = _score_knowledge_section(
        filename="registration.md",
        heading="Registration",
        text=unrelated,
        query_tokens=query_tokens,
        query_phrases=query_phrases,
    )

    assert _knowledge_source_tier_weight(official) > _knowledge_source_tier_weight(weak)
    assert official_score > weak_score
    assert unrelated_score == 0


def test_ai_knowledge_ranker_boosts_rare_exact_albion_terms() -> None:
    sections = [
        _make_knowledge_section(
            "albion_member_field_manual.md",
            "Fishing basics",
            "Fishing is a gathering profession. Bring bait and bank loot safely.",
        ),
        _make_knowledge_section(
            "albion_fishing.md",
            "Deadwater Eel",
            "Deadwater Eel is a T7 rare freshwater fish. It is found in T7-T8 freshwater and rare fish are RNG.",
        ),
    ]

    ranked = _rank_knowledge_sections(
        "how do i catch deadriver eel level 7?",
        sections=sections,
    )

    assert ranked[0][1] == "albion_fishing.md"
    assert ranked[0][2] == "Deadwater Eel"


def test_ai_knowledge_retrieval_preview_is_compact_and_ranked() -> None:
    sections = [
        _make_knowledge_section(
            "generic.md",
            "Fishing basics",
            "Fishing is a gathering profession. Bring bait and bank loot safely.",
        ),
        _make_knowledge_section(
            "albion_fishing.md",
            "Deadwater Eel",
            "Deadwater Eel is a T7 rare freshwater fish. It is found in T7-T8 freshwater and rare fish are RNG.",
        ),
    ]

    preview = _knowledge_retrieval_preview(
        "how do i catch deadriver eel level 7?",
        sections=sections,
        limit=1,
    )

    assert len(preview) == 1
    assert preview[0]["filename"] == "albion_fishing.md"
    assert preview[0]["heading"] == "Deadwater Eel"
    assert "T7 rare freshwater fish" in str(preview[0]["preview"])


def test_ai_knowledge_terms_cover_event_weapon_and_inactivity_workflows() -> None:
    tokens = _knowledge_tokens("why does the scorecard need vc attendance for reconsile and regear?")
    assert {"scorecard", "attendance", "analytics", "regear"} <= tokens

    phrases = _knowledge_phrases("where do i pick weapon roles and why join event voice?")
    assert "weapon roles" in phrases
    assert "event voice" in phrases

    assert "weapon_roles_and_content_pings.md" in KNOWLEDGE_FILE_HINTS
    assert "event_attendance_analytics_regear.md" in KNOWLEDGE_FILE_HINTS
    assert "inactivity_lifecycle_policy.md" in KNOWLEDGE_FILE_HINTS


def test_ai_live_context_helpers_format_time_and_intent() -> None:
    assert _question_contains("When are we busiest?", "busiest", "best time")
    assert not _question_contains("what is focus fire?", "busiest", "best time")
    assert _weekday_name_sqlite(3) == "Wed"
    assert _utc_hour_window(23) == "23:00-00:00 UTC"
    assert _discord_timestamp("2026-06-12T20:00:00+00:00", "R") == "<t:1781294400:R>"


def test_voice_access_requires_registered_or_approved_role() -> None:
    assert not _member_has_registered_voice_access(_Member("Unverified"), _VoiceAccessDb())
    assert _member_has_registered_voice_access(_Member("Verified"), _VoiceAccessDb())
    assert _member_has_registered_voice_access(_Member("HomeGuild"), _VoiceAccessDb())
    assert _member_has_registered_voice_access(_Member("Guest"), _VoiceAccessDb())
    assert _member_has_registered_voice_access(
        _Member("Approved Voice Guest"),
        _VoiceAccessDb("Approved Voice Guest"),
    )


def test_unverified_kick_targets_skip_stale_unverified_on_registered_members() -> None:
    unverified = _KickRole("Unverified")
    verified = _KickRole("Verified")
    member = _KickRole("Member")

    eligible = _KickMember(unverified, joined_days_ago=9)
    _KickMember(unverified, verified, joined_days_ago=90)
    _KickMember(unverified, member, joined_days_ago=90)
    _KickMember(unverified, joined_days_ago=2)

    targets = _collect_unverified_kick_targets(_KickGuild([unverified, verified, member]), 7)

    assert targets == [(eligible, 9)]


def test_stale_unverified_role_members_collects_registered_members_only() -> None:
    unverified = _KickRole("Unverified")
    verified = _KickRole("Verified")
    member = _KickRole("Member")

    stale_verified = _KickMember(unverified, verified, joined_days_ago=90, name="zeta")
    stale_member = _KickMember(unverified, member, joined_days_ago=90, name="alpha")
    _KickMember(unverified, joined_days_ago=90, name="plain")

    stale = _collect_stale_unverified_role_members(_KickGuild([unverified, verified, member]))

    assert stale == [stale_member, stale_verified]


def test_event_report_regear_requests_are_consolidated_not_fanned_out() -> None:
    lines = suppressed_auto_regear_lines(
        [
            {"estimated_value": 1_500_000},
            {"estimated_value": 0},
            {"estimated_value": 250_000},
        ]
    )
    text = "\n".join(lines)

    assert "not** auto-created" in text
    assert "Regear Review" in text
    assert "Deaths listed: **3**" in text
    assert "Manual pricing needed: **1**" in text


def test_quick_workflow_answer_routes_server_categories() -> None:
    channels = {
        "server_guide": "<#guide>",
        "rules": "<#rules>",
        "registration": "<#register>",
        "content_roles": "<#roles>",
        "event_board": "<#event-board>",
        "martlock_lfg": "<#martlock-lfg>",
        "martlock_info": "<#martlock-info>",
        "faction_chat": "<#faction-chat>",
        "alliance_events": "<#alliance-events>",
        "alliance_info": "<#alliance-info>",
        "alliance_chat": "<#alliance-chat>",
        "comps": "<#comps>",
        "content_planning": "<#planning>",
    }

    start = _quick_workflow_answer("where do i start?", channels) or ""
    assert "<#guide>" in start
    assert "<#rules>" in start

    faction = _quick_workflow_answer("where do faction lfg posts go?", channels) or ""
    assert "<#martlock-lfg>" in faction

    alliance = _quick_workflow_answer("where do alliance events go?", channels) or ""
    assert "<#alliance-events>" in alliance

    builds = _quick_workflow_answer("where do builds and comps go?", channels) or ""
    assert "<#comps>" in builds
    assert "<#planning>" in builds


def test_quick_workflow_answer_does_not_hijack_build_advice() -> None:
    channels = {
        "comps": "<#comps>",
        "content_planning": "<#planning>",
    }

    assert _quick_workflow_answer("What's a good ZvZ build for a frontline tank right now?", channels) is None


# ── openai moderation thresholds ──────────────────────────────────────────


def test_moderation_threshold_ignores_low_confidence_soft_flags() -> None:
    should_alert, reason = _moderation_threshold_decision(
        categories=["harassment"],
        category_scores={"harassment": 0.51},
        alert_threshold=0.82,
        severe_alert_threshold=0.35,
    )
    assert not should_alert
    assert "below threshold" in reason


def test_moderation_threshold_keeps_severe_categories_sensitive() -> None:
    should_alert, reason = _moderation_threshold_decision(
        categories=["harassment/threatening"],
        category_scores={"harassment/threatening": 0.42},
        alert_threshold=0.82,
        severe_alert_threshold=0.35,
    )
    assert should_alert
    assert "severe category" in reason


def test_moderation_threshold_alerts_high_confidence_normal_flags() -> None:
    should_alert, reason = _moderation_threshold_decision(
        categories=["harassment"],
        category_scores={"harassment": 0.91},
        alert_threshold=0.82,
        severe_alert_threshold=0.35,
    )
    assert should_alert
    assert "harassment" in reason


# ── help embed size guardrails ─────────────────────────────────────────────


def test_help_embed_size_counts_fields_and_footer() -> None:
    embed = discord.Embed(title="Help", description="Commands")
    embed.add_field(name="General", value="x" * 10, inline=False)
    embed.set_footer(text="Tip")
    assert _embed_text_size(embed) == len("Help") + len("Commands") + len("General") + 10 + len("Tip")


def test_help_embed_refuses_fields_that_would_exceed_safe_limit() -> None:
    embed = discord.Embed(title="Help", description="x" * 5550)
    assert not _can_add_field(embed, name="More", value="x" * 100)


def test_help_embed_allows_small_fields() -> None:
    embed = discord.Embed(title="Help", description="Commands")
    assert _can_add_field(embed, name="General", value="`/ping` - pong")


# ── _split_enchant ────────────────────────────────────────────────────────


def test_split_enchant_no_suffix() -> None:
    assert _split_enchant("T7_HEAD_PLATE_SET2") == ("T7_HEAD_PLATE_SET2", 0)


def test_split_enchant_with_level() -> None:
    assert _split_enchant("T7_HEAD_PLATE_SET2@3") == ("T7_HEAD_PLATE_SET2", 3)


def test_split_enchant_garbage_suffix_returns_zero() -> None:
    assert _split_enchant("T7_HEAD_PLATE_SET2@oops") == ("T7_HEAD_PLATE_SET2", 0)


def test_split_enchant_empty_and_none() -> None:
    assert _split_enchant("") == ("", 0)
    assert _split_enchant(None) == ("", 0)  # type: ignore[arg-type]


# ── nickname alliance tags ─────────────────────────────────────────────────


def test_home_member_nickname_uses_tu_override() -> None:
    db = _NickTagDb()
    nick = tagged_nickname_for_profile(
        db,
        "ExamplePlayer",
        {"guild_id": "guild-home", "alliance_id": "home-alliance", "alliance_tag": "UoT"},
        home_member=True,
    )
    assert nick == "[TU] ExamplePlayer"


def test_home_alliance_member_uses_guild_initials_tag() -> None:
    db = _NickTagDb()
    nick = tagged_nickname_for_profile(
        db,
        "RoadsFriend",
        {"guild_id": "guild-divine", "guild_name": "Divine Departure"},
        home_member=False,
    )
    assert nick == "[DD] RoadsFriend"


def test_home_alliance_member_can_use_configured_guild_tag_override() -> None:
    db = _NickTagDb()
    nick = tagged_nickname_for_profile(
        db,
        "GuildFriend",
        {"guild_id": "custom-guild", "guild_name": "Some Complicated Guild Name"},
        home_member=False,
    )
    assert nick == "[CG] GuildFriend"


def test_other_alliance_member_uses_their_alliance_tag() -> None:
    db = _NickTagDb()
    nick = tagged_nickname_for_profile(
        db,
        "Visitor",
        {"guild_id": "guild-other"},
        home_member=False,
    )
    assert nick == "[BURNR] Visitor"


def test_short_alliance_name_falls_back_as_tag_when_tag_missing() -> None:
    db = _NickTagDb()
    nick = tagged_nickname_for_profile(
        db,
        "MozzyFriend",
        {"alliance_id": "mozzy-id", "alliance_name": "MOZZY", "alliance_tag": ""},
        home_member=False,
    )
    assert nick == "[MOZZY] MozzyFriend"


def test_full_alliance_name_without_tag_does_not_become_prefix() -> None:
    db = _NickTagDb()
    nick = tagged_nickname_for_profile(
        db,
        "NoShortTag",
        {"alliance_id": "long-id", "alliance_name": "Long Alliance Name", "alliance_tag": ""},
        home_member=False,
    )
    assert nick == "NoShortTag"


def test_unallied_member_has_no_nickname_tag() -> None:
    db = _NickTagDb()
    assert tagged_nickname_for_profile(db, "Solo", {}, home_member=False) == "Solo"


def test_tagged_nickname_extraction_accepts_any_tag() -> None:
    assert extract_tagged_nickname_name("[UOT] ExamplePlayer") == "ExamplePlayer"
    assert extract_tagged_nickname_name("[BURNR] Visitor") == "Visitor"
    assert extract_tagged_nickname_name("NoTag") is None


def test_strip_managed_nickname_tag_keeps_unknown_tags() -> None:
    db = _NickTagDb()
    assert strip_managed_nickname_tag(db, "[TU] OldName") == "OldName"
    assert strip_managed_nickname_tag(db, "[UOT] NewName") == "NewName"
    assert strip_managed_nickname_tag(db, "[BURNR] Visitor") == "Visitor"
    assert strip_managed_nickname_tag(db, "[XYZ] Stranger") is None


# ── market route risk ─────────────────────────────────────────────────────


def test_market_route_risk_flags_caerleon_as_lethal() -> None:
    risk = _route_risk("Fort Sterling", "Caerleon")
    assert risk.emoji == "☠️"
    assert "Lethal" in risk.label
    assert "red-zone" in risk.detail


def test_market_route_risk_flags_brecilien_as_unstable() -> None:
    risk = _route_risk("Brecilien", "Martlock")
    assert risk.emoji == "🌀"
    assert "Brecilien" in risk.label


def test_market_route_risk_flags_royal_city_as_lower_risk() -> None:
    risk = _route_risk("Bridgewatch", "Martlock")
    assert risk.emoji == "🟢"
    assert risk.label == "Royal-city haul"


def test_lfg_access_role_name_is_discord_safe_and_bounded() -> None:
    name = _event_access_role_name({
        "id": 123,
        "title": "@everyone <script> Very Long Event " * 8,
    })
    assert name.startswith("LFG 123 - ")
    assert name.endswith(" Access")
    assert "@" not in name
    assert "<" not in name
    assert len(name) <= 100


# ── _compute_fame ─────────────────────────────────────────────────────────


def test_compute_fame_combat_only() -> None:
    combat, total = _compute_fame({"kill_fame": 5_000_000})
    assert combat == 5_000_000
    assert total == 5_000_000


def test_compute_fame_sums_all_categories() -> None:
    combat, total = _compute_fame({
        "kill_fame": 1_000_000,
        "pve_total": 500_000,
        "gather_all": 200_000,
        "crafting_fame": 100_000,
        "fishing_fame": 50_000,
        "farming_fame": 25_000,
    })
    assert combat == 1_000_000
    assert total == 1_875_000


def test_compute_fame_handles_missing_keys() -> None:
    combat, total = _compute_fame({})
    assert combat == 0
    assert total == 0


def test_compute_fame_handles_none_values() -> None:
    combat, total = _compute_fame({"kill_fame": None, "pve_total": None})
    assert combat == 0
    assert total == 0


# ── fmt_silver ────────────────────────────────────────────────────────────


def test_fmt_silver_preserves_quarter_millions() -> None:
    assert fmt_silver(2_250_000) == "2.25M"
    assert fmt_silver(1_500_000) == "1.5M"


def test_fmt_silver_compacts_thousands() -> None:
    assert fmt_silver(187_500) == "187.5k"
    assert fmt_silver(375_000) == "375k"


def test_bounty_needs_payment_only_for_completed_unpaid_rewards() -> None:
    assert bounty_needs_payment({
        "status": "completed",
        "reward_points": 1_000_000,
        "claimed_by": "123",
        "paid_at": None,
    })
    assert not bounty_needs_payment({
        "status": "completed",
        "reward_points": 1_000_000,
        "claimed_by": "123",
        "paid_at": "2026-06-05 12:00:00",
    })
    assert not bounty_needs_payment({
        "status": "submitted",
        "reward_points": 1_000_000,
        "claimed_by": "123",
    })


def test_roads_core_color_aliases_and_titles() -> None:
    assert normalize_road_core_color("green") == ("green", None)
    assert normalize_road_core_color("T6") == ("blue", None)
    assert normalize_road_core_color("purp") == ("purple", None)
    assert normalize_road_core_color("gold") == ("gold", None)
    assert normalize_road_core_color("yellow") == ("gold", None)
    assert normalize_road_core_color("orange")[0] is None
    assert ROAD_CORE_REWARDS["green"] == 1_000_000
    assert ROAD_CORE_REWARDS["gold"] == 10_000_000
    assert road_core_title("blue").startswith("[Roads Core]")


def test_roads_core_price_parser_accepts_compact_silver_values() -> None:
    assert parse_road_core_price("10m") == (10_000_000, None)
    assert parse_road_core_price("3 million") == (3_000_000, None)
    assert parse_road_core_price("1,250k") == (1_250_000, None)
    assert parse_road_core_price("500000") == (500_000, None)
    assert parse_road_core_price("nope")[0] is None


def test_roads_core_proof_round_trip() -> None:
    proof = road_core_proof_text(
        color="purple",
        screenshot="https://cdn.discordapp.com/core.png",
        party="@A @B",
        note="won fight on exit",
    )
    parsed = parse_road_core_proof(proof)

    assert parsed["color"] == "purple"
    assert parsed["screenshot"] == "https://cdn.discordapp.com/core.png"
    assert parsed["party"] == "@A @B"
    assert parsed["note"] == "won fight on exit"


def test_roads_core_image_attachment_url_accepts_pasted_images() -> None:
    class Attachment:
        def __init__(self, filename: str, url: str, content_type: str | None = None) -> None:
            self.filename = filename
            self.url = url
            self.content_type = content_type

    class Message:
        attachments = [
            Attachment("notes.txt", "https://cdn.discordapp.com/notes.txt", "text/plain"),
            Attachment("core-proof.png", "https://cdn.discordapp.com/core-proof.png", None),
        ]

    assert image_attachment_url(Message()) == "https://cdn.discordapp.com/core-proof.png"


# ── _period_key ───────────────────────────────────────────────────────────


def test_period_key_daily() -> None:
    now = dt.datetime(2025, 6, 15, 12, 0, 0)
    assert _period_key("daily", now) == "D-2025-06-15"


def test_period_key_weekly_uses_iso_week() -> None:
    # 2025-01-01 is a Wednesday → ISO week 1 of 2025.
    now = dt.datetime(2025, 1, 1)
    assert _period_key("weekly", now) == "W-2025-01"


def test_period_key_daily_changes_per_day() -> None:
    d1 = dt.datetime(2025, 6, 15)
    d2 = dt.datetime(2025, 6, 16)
    assert _period_key("daily", d1) != _period_key("daily", d2)


def test_period_key_once_is_static() -> None:
    a = _period_key("once", dt.datetime(2025, 1, 1))
    b = _period_key("once", dt.datetime(2030, 12, 31))
    assert a == b == "ONCE"


def test_period_key_unknown_falls_through_to_once() -> None:
    assert _period_key("nonsense", dt.datetime(2025, 6, 15)) == "ONCE"


# ── _safe_int ─────────────────────────────────────────────────────────────


def test_safe_int_passthrough() -> None:
    assert _safe_int(42) == 42
    assert _safe_int("99") == 99


def test_safe_int_none_and_garbage() -> None:
    assert _safe_int(None) == 0
    assert _safe_int("") == 0
    assert _safe_int("not a number") == 0
    assert _safe_int([]) == 0


# ── dashboard health helpers ──────────────────────────────────────────────


def test_pct_handles_empty_denominators() -> None:
    assert _pct(3, 0) == 0
    assert _pct(3, None) == 0  # type: ignore[arg-type]


def test_pct_rounds_whole_numbers() -> None:
    assert _pct(2, 3) == 67
    assert _pct(1, 4) == 25


def test_score_from_pct_thresholds() -> None:
    assert _score_from_pct(80, green=80, yellow=55) == 100
    assert _score_from_pct(60, green=80, yellow=55) == 65
    assert _score_from_pct(20, green=80, yellow=55) == 35


def test_queue_score_empty_is_best() -> None:
    assert _queue_score(0) == 100
    assert _queue_score(1, warn=1, bad=5) == 75
    assert _queue_score(3, warn=1, bad=5) == 50
    assert _queue_score(9, warn=1, bad=5) == 25


def test_health_emoji_thresholds() -> None:
    assert _health_emoji(80) == "🟢"
    assert _health_emoji(55) == "🟡"
    assert _health_emoji(54) == "🔴"


# ── LFG prime-slot helpers ────────────────────────────────────────────────


def test_slot_occurrence_on_date_uses_chosen_utc_date() -> None:
    slot = PrimeSlot(20, 21)
    start, end = _slot_occurrence_on_date(slot, dt.date(2026, 6, 3))
    assert start == dt.datetime(2026, 6, 3, 20, tzinfo=dt.timezone.utc)
    assert end == dt.datetime(2026, 6, 3, 21, tzinfo=dt.timezone.utc)


def test_slot_occurrence_on_date_handles_midnight_rollover() -> None:
    slot = PrimeSlot(23, 0)
    start, end = _slot_occurrence_on_date(slot, dt.date(2026, 6, 3))
    assert start == dt.datetime(2026, 6, 3, 23, tzinfo=dt.timezone.utc)
    assert end == dt.datetime(2026, 6, 4, 0, tzinfo=dt.timezone.utc)


def test_next_occurrence_keeps_next_upcoming_default() -> None:
    slot = PrimeSlot(20, 21)
    now = dt.datetime(2026, 6, 3, 20, 1, tzinfo=dt.timezone.utc)
    start, end = _next_occurrence(slot, now)
    assert start == dt.datetime(2026, 6, 4, 20, tzinfo=dt.timezone.utc)
    assert end == dt.datetime(2026, 6, 4, 21, tzinfo=dt.timezone.utc)


def test_extract_ip_requirement_from_free_text() -> None:
    assert _extract_ip_requirement("ava roads clap comp, 1500 min ip") == "1500 IP"
    assert _extract_ip_requirement("IP req: 1400+") == "1400 IP"
    assert _extract_ip_requirement("no requirement") is None


def test_normalize_ip_requirement_accepts_dedicated_bare_field() -> None:
    assert _normalize_ip_requirement("1500", allow_bare=True) == "1500 IP"
    assert _normalize_ip_requirement("1500 IP", allow_bare=True) == "1500 IP"
    assert _normalize_ip_requirement("5000", allow_bare=True) is None


def test_event_voice_channel_name_stitches_ip_to_title() -> None:
    name = _event_voice_channel_name({
        "title": "Ava Roads",
        "comp_notes": "ava road clap comp, 1500 min ip",
    })
    assert name == "🎙️ Ava Roads - 1500 IP"


def test_event_voice_channel_name_prefers_dedicated_ip_field() -> None:
    name = _event_voice_channel_name({
        "title": "Ava Roads",
        "ip_requirement": "1500",
        "comp_notes": "old comp note, 1200 min ip",
    })
    assert name == "🎙️ Ava Roads - 1500 IP"


class _FakeSnowflake:
    def __init__(self, snowflake_id: int) -> None:
        self.id = snowflake_id


class _FakeGuild:
    def __init__(self, default_role: _FakeSnowflake) -> None:
        self.default_role = default_role
        self.me = None


class _FakeCategory:
    def __init__(self, overwrites: dict[_FakeSnowflake, discord.PermissionOverwrite]) -> None:
        self.overwrites = overwrites


def test_event_voice_overwrites_are_visible_but_roster_gated() -> None:
    everyone = _FakeSnowflake(1)
    content_role = _FakeSnowflake(2)
    access_role = _FakeSnowflake(3)
    guild = _FakeGuild(everyone)
    category = _FakeCategory({
        everyone: discord.PermissionOverwrite(view_channel=False, connect=False),
        content_role: discord.PermissionOverwrite(view_channel=True, connect=True),
    })

    overwrites = _event_voice_overwrites(guild, access_role, category)  # type: ignore[arg-type]

    assert overwrites[everyone].view_channel is False
    assert overwrites[everyone].connect is False
    assert overwrites[content_role].view_channel is True
    assert overwrites[content_role].connect is False
    assert overwrites[access_role].view_channel is True
    assert overwrites[access_role].connect is True


def test_parse_general_lfg_schedule_combines_start_and_duration() -> None:
    starts, ends = _parse_general_lfg_schedule("2026-06-04 20:00, 90m")
    assert starts == dt.datetime(2026, 6, 4, 20, tzinfo=dt.timezone.utc)
    assert ends == dt.datetime(2026, 6, 4, 21, 30, tzinfo=dt.timezone.utc)


def test_claim_fields_promote_exact_prime_window() -> None:
    starts, ends = _parse_general_lfg_schedule("2026-06-06 04:00, 60m")
    assert _claim_fields_for_schedule(starts, ends) == {
        "slot_label": "PRIME 04:00-05:00",
        "is_prime": 1,
    }


def test_claim_fields_keep_custom_window_general() -> None:
    starts, ends = _parse_general_lfg_schedule("2026-06-06 04:30, 60m")
    assert _claim_fields_for_schedule(starts, ends) == {
        "slot_label": "GENERAL",
        "is_prime": 0,
    }


def test_prime_slot_display_labels_use_compact_utc_range() -> None:
    assert prime_slot_display_label(PrimeSlot(18, 19)) == "UTC 18-19"
    assert display_slot_label("PRIME 02:00-03:00") == "UTC 02-03"
    assert display_slot_label("GENERAL") == "General LFG"


# ── primetime claim helpers ────────────────────────────────────────────────


def test_normalize_claim_window_defaults_to_today() -> None:
    assert normalize_claim_window(None) == "today"
    assert normalize_claim_window("bad") == "today"
    assert normalize_claim_window("week") == "week"


def test_claim_window_bounds_today_uses_albion_timer_day() -> None:
    now = dt.datetime(2026, 5, 28, 5, 30, tzinfo=dt.timezone.utc)
    start, end = _claim_window_bounds("today", now)
    assert start == dt.datetime(2026, 5, 28, 18, tzinfo=dt.timezone.utc)
    assert end == dt.datetime(2026, 5, 29, 5, tzinfo=dt.timezone.utc)


def test_claim_window_bounds_before_rollover_uses_previous_timer_day() -> None:
    now = dt.datetime(2026, 5, 28, 4, 30, tzinfo=dt.timezone.utc)
    start, end = _claim_window_bounds("today", now)
    assert start == dt.datetime(2026, 5, 27, 18, tzinfo=dt.timezone.utc)
    assert end == dt.datetime(2026, 5, 28, 5, tzinfo=dt.timezone.utc)


def test_claim_window_bounds_week_is_seven_days() -> None:
    now = dt.datetime(2026, 5, 28, 23, 59, tzinfo=dt.timezone.utc)
    start, end = _claim_window_bounds("week", now)
    assert start == dt.datetime(2026, 5, 28, 18, tzinfo=dt.timezone.utc)
    assert end == dt.datetime(2026, 6, 4, 5, tzinfo=dt.timezone.utc)


def test_slot_key_from_label_detects_prime_slot() -> None:
    assert _slot_key_from_label("PRIME 20:00-21:00") == "20:00-21:00"
    assert _slot_key_from_label("GENERAL") is None


def test_format_day_field_name_includes_local_start_time() -> None:
    assert _format_day_field_name(dt.date(2026, 5, 30)) == (
        "Sat May 30 timer day • starts <t:1780164000:t> local"
    )


def test_slot_display_label_uses_compact_timer_range() -> None:
    assert _slot_display_label(dt.date(2026, 5, 31), PrimeSlot(2, 3)) == "UTC 02-03"


def test_lfg_message_url_builds_jump_link() -> None:
    event = {"channel_id": "1502194750298787860", "message_id": "1509651280132313159"}
    assert _lfg_message_url(event, "111111111111111111") == (
        "https://discord.com/channels/"
        "111111111111111111/1502194750298787860/1509651280132313159"
    )


def test_lfg_message_url_skips_cleaned_posts() -> None:
    event = {
        "channel_id": "1502194750298787860",
        "message_id": "1509651280132313159",
        "lfg_cleaned_at": "2026-06-01T03:15:00+00:00",
    }
    assert _lfg_message_url(event, "111111111111111111") is None


def test_linked_event_title_links_when_message_ids_exist() -> None:
    event = {
        "title": "Ganking - Timer claim test",
        "channel_id": "1502194750298787860",
        "message_id": "1509651280132313159",
    }
    assert _linked_event_title(event, "111111111111111111") == (
        "**[Ganking - Timer claim test]"
        "(https://discord.com/channels/"
        "111111111111111111/1502194750298787860/1509651280132313159)**"
    )


def test_utc_clock_name_uses_prime_emoji_and_ten_minute_bucket() -> None:
    now = dt.datetime(2026, 5, 30, 20, 17, tzinfo=dt.timezone.utc)
    assert _utc_clock_name(now) == "🟧 UTC 20:10 (10m) 🟧"


def test_utc_clock_name_uses_waiting_emoji_between_timers() -> None:
    now = dt.datetime(2026, 5, 30, 21, 7, tzinfo=dt.timezone.utc)
    assert _utc_clock_name(now) == "⏳ UTC 21:00 (10m) ⏳"


def test_utc_clock_name_uses_sleep_emoji_outside_prime_block() -> None:
    now = dt.datetime(2026, 5, 31, 5, 37, tzinfo=dt.timezone.utc)
    assert _utc_clock_name(now) == "💤 UTC 05:30 (10m) 💤"


def test_utc_clock_name_uses_after_midnight_timer_colors() -> None:
    now = dt.datetime(2026, 5, 31, 2, 59, tzinfo=dt.timezone.utc)
    assert _utc_clock_name(now) == "🟦 UTC 02:50 (10m) 🟦"


# ── content planning helpers ───────────────────────────────────────────────


def test_parse_availability_slots_splits_and_deduplicates() -> None:
    raw = "Fri 22:00 UTC, Sat 20:00 UTC\nfri 22:00 utc; Sun 02:00 UTC"
    assert parse_availability_slots(raw) == [
        "Fri 22:00 UTC",
        "Sat 20:00 UTC",
        "Sun 02:00 UTC",
    ]


def test_availability_recommendations_scale_with_headcount() -> None:
    seven = " ".join(availability_content_recommendations(7))
    assert "Ava" in seven
    assert "ZvZ" not in seven

    sixteen = " ".join(availability_content_recommendations(16))
    assert "ZvZ" in sixteen


def test_availability_recommendation_keys_are_event_type_keys() -> None:
    assert "roads" in availability_recommendation_keys(7)
    assert "zvz" not in availability_recommendation_keys(7)
    assert "zvz" in availability_recommendation_keys(16)


def test_season_point_focus_recommendations_exclude_casual_options() -> None:
    keys = season_point_focus_recommendation_keys(None, 20, limit=25)
    assert "zvz" in keys
    assert "roads" in keys
    assert "faction" not in keys
    assert "crystal_arena" not in keys
    assert "economy" not in keys
    assert "transport" not in keys


def test_daily_timer_slot_windows_span_evening_cycle() -> None:
    windows = daily_timer_slot_windows(dt.date(2026, 5, 31))
    assert windows[0]["label"] == "🟥 Sun May 31 · UTC 18-19"
    assert windows[-1]["label"] == "🟪 Mon Jun 01 · UTC 04-05"
    assert [w["slot_label"] for w in windows] == [
        "PRIME 18:00-19:00",
        "PRIME 20:00-21:00",
        "PRIME 22:00-23:00",
        "PRIME 00:00-01:00",
        "PRIME 02:00-03:00",
        "PRIME 04:00-05:00",
    ]
    assert windows[0]["starts_at"] == dt.datetime(2026, 5, 31, 18, tzinfo=dt.timezone.utc)
    assert windows[-1]["starts_at"] == dt.datetime(2026, 6, 1, 4, tzinfo=dt.timezone.utc)


def test_daily_timer_availability_label_parses_compact_timer() -> None:
    poll = {"closes_at": "2026-05-31T10:00:00+00:00"}
    label = "🟦 Mon Jun 01 · UTC 02-03"
    assert _availability_slot_heading(label) == "🟦 **Mon Jun 01** · `UTC 02-03`"
    assert _availability_timer_window(poll, label) == (
        dt.datetime(2026, 6, 1, 2, tzinfo=dt.timezone.utc),
        dt.datetime(2026, 6, 1, 3, tzinfo=dt.timezone.utc),
    )


def test_daily_timer_target_date_uses_utc_date() -> None:
    now = dt.datetime(2026, 5, 31, 5, 10, tzinfo=dt.timezone.utc)
    assert daily_timer_target_date(now) == dt.date(2026, 5, 31)


def test_daily_timer_due_windows() -> None:
    now = dt.datetime(2026, 5, 31, 5, 10, tzinfo=dt.timezone.utc)
    assert daily_timer_availability_due(now, hour=5, minute=5)
    assert not daily_timer_availability_due(
        dt.datetime(2026, 5, 31, 6, 0, tzinfo=dt.timezone.utc),
        hour=5,
        minute=5,
    )
    assert daily_timer_vote_due(
        dt.datetime(2026, 5, 31, 15, 0, tzinfo=dt.timezone.utc),
        hour=15,
        minute=0,
    )
    assert not daily_timer_vote_due(
        dt.datetime(2026, 5, 31, 14, 59, tzinfo=dt.timezone.utc),
        hour=15,
        minute=0,
    )


def test_ranked_available_timer_indexes_prefers_headcount_then_early_slot() -> None:
    assert ranked_available_timer_indexes(
        {0: 3, 1: 5, 2: 5, 3: 1},
        window_count=4,
        min_available=2,
    ) == [(5, 1), (5, 2), (3, 0)]


class _ConfigDb:
    def __init__(self, values: dict[str, str | None] | None = None) -> None:
        self.values = values or {}

    def get_config(self, key: str) -> str | None:
        return self.values.get(key)


def test_recurring_cta_config_is_opt_in() -> None:
    assert not _config_enabled_from_db(_ConfigDb(), "lfg_recurring_02_cta_enabled")
    assert _config_enabled_from_db(
        _ConfigDb({"lfg_recurring_02_cta_enabled": "enabled"}),
        "lfg_recurring_02_cta_enabled",
    )


def test_content_role_panel_channel_id_parser_rejects_bad_config() -> None:
    assert _parse_config_channel_id(None) == (None, None)
    assert _parse_config_channel_id("12345") == (12345, None)

    parsed, error = _parse_config_channel_id("not-a-channel")

    assert parsed is None
    assert error == "Stored channel ID is invalid."
