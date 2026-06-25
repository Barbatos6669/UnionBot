"""Weapon-tree self-assign role helpers.

This module keeps the Albion weapon-tree picker separate from the content
ping-role picker. The public cog can expose commands, while this helper owns
the weapon role mappings, panel embed, and persistent Discord views.
"""
from __future__ import annotations

import discord

from debug import error_log
from utils import error_embed, info_embed, success_embed

CFG_WEAPON_PANEL_CHANNEL = "weapon_roles_channel_id"
CFG_WEAPON_PANEL_MESSAGE = "weapon_roles_message_id"
CFG_WEAPON_ROLE_PREFIX = "weapon_role_"

# Weapon tree roles intentionally stay at tree/line granularity. Per-weapon
# roles would be too noisy and would go stale every balance patch.
WEAPON_TREES: dict[str, tuple[str, str, str]] = {
    "sword": ("⚔️", "Sword", "Weapon: Sword"),
    "axe": ("🪓", "Axe", "Weapon: Axe"),
    "mace": ("🔨", "Mace", "Weapon: Mace"),
    "hammer": ("🔨", "Hammer", "Weapon: Hammer"),
    "spear": ("🔱", "Spear", "Weapon: Spear"),
    "dagger": ("🗡️", "Dagger", "Weapon: Dagger"),
    "quarterstaff": ("🦯", "Quarterstaff", "Weapon: Quarterstaff"),
    "war_gloves": ("🥊", "War Gloves", "Weapon: War Gloves"),
    "bow": ("🏹", "Bow", "Weapon: Bow"),
    "crossbow": ("🎯", "Crossbow", "Weapon: Crossbow"),
    "fire": ("🔥", "Fire Staff", "Weapon: Fire Staff"),
    "frost": ("❄️", "Frost Staff", "Weapon: Frost Staff"),
    "cursed": ("☠️", "Cursed Staff", "Weapon: Cursed Staff"),
    "arcane": ("✨", "Arcane Staff", "Weapon: Arcane Staff"),
    "holy": ("🌟", "Holy Staff", "Weapon: Holy Staff"),
    "nature": ("🌿", "Nature Staff", "Weapon: Nature Staff"),
    "shapeshifter": ("🐾", "Shapeshifter Staff", "Weapon: Shapeshifter Staff"),
}

WEAPON_CATEGORIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("frontline", "🛡️ Frontline & Melee",
     ("sword", "axe", "mace", "hammer", "spear", "dagger", "quarterstaff", "war_gloves")),
    ("ranged", "🏹 Ranged Damage",
     ("bow", "crossbow", "fire", "frost", "cursed")),
    ("support", "✨ Healing & Support",
     ("holy", "nature", "arcane", "shapeshifter")),
)

MAX_OPTIONS_PER_SELECT = 25


def resolve_weapon_roles(
    guild: discord.Guild, db, weapon_keys: tuple[str, ...],
) -> list[tuple[str, discord.Role]]:
    out: list[tuple[str, discord.Role]] = []
    for key in weapon_keys:
        rid = db.get_config(CFG_WEAPON_ROLE_PREFIX + key)
        role: discord.Role | None = None
        if rid:
            try:
                role = guild.get_role(int(rid))
            except (TypeError, ValueError):
                role = None
        if role is None:
            role_name = WEAPON_TREES.get(key, ("", key, ""))[2]
            role = discord.utils.get(guild.roles, name=role_name) if role_name else None
        if role is not None:
            out.append((key, role))
    return out


def weapon_tree_label(key: str) -> tuple[str, str]:
    emoji, label, _role_name = WEAPON_TREES.get(key, ("📌", key, ""))
    return emoji, label


async def ensure_weapon_roles(guild: discord.Guild, db) -> tuple[list[discord.Role], list[discord.Role]]:
    """Create missing weapon-tree roles and save their config mappings."""
    existing: list[discord.Role] = []
    created: list[discord.Role] = []
    for key, (_emoji, _label, role_name) in WEAPON_TREES.items():
        role: discord.Role | None = None
        rid = db.get_config(CFG_WEAPON_ROLE_PREFIX + key)
        if rid:
            try:
                role = guild.get_role(int(rid))
            except (TypeError, ValueError):
                role = None
        if role is None:
            role = discord.utils.get(guild.roles, name=role_name)
        if role is None:
            role = await guild.create_role(
                name=role_name,
                mentionable=False,
                reason="Create weapon-tree self-assign role",
            )
            created.append(role)
        else:
            existing.append(role)
        db.set_config(CFG_WEAPON_ROLE_PREFIX + key, str(role.id))
    return existing, created


def weapon_panel_embed(guild: discord.Guild, db) -> discord.Embed:
    embed = discord.Embed(
        title="⚔️ Weapon Tree Roles",
        description=(
            "Pick the weapon trees you can realistically bring to organized content. "
            "This helps shotcallers build comps, find swaps, and see who can fill "
            "frontline, DPS, healing, and support roles.\n\n"
            "Choose trees, not one-off weapons. You can update this any time as your "
            "spec or comfort changes."
        ),
        color=discord.Color.dark_teal(),
    )
    for _key, label, weapon_keys in WEAPON_CATEGORIES:
        pairs = resolve_weapon_roles(guild, db, weapon_keys)
        if not pairs:
            continue
        names = ", ".join(role.name for _k, role in pairs)
        embed.add_field(
            name=f"{label}  ({len(pairs)})",
            value=names if len(names) <= 1024 else names[:1020] + "…",
            inline=False,
        )
    embed.set_footer(text="Tip: pick trees you are willing to show up on, not every tree you own.")
    return embed


class WeaponCategoryButton(discord.ui.Button):
    def __init__(self, category_key: str, label: str) -> None:
        super().__init__(
            label=label,
            style=discord.ButtonStyle.primary,
            custom_id=f"weapon_roles:cat:{category_key}",
        )
        self.category_key = category_key

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                embed=error_embed("Guild only", "Use this inside the server."),
                ephemeral=True,
            )
            return

        cat = next(
            (c for c in WEAPON_CATEGORIES if c[0] == self.category_key), None,
        )
        if cat is None:
            await interaction.response.send_message(
                embed=error_embed("Unknown category", "This button is stale; please ask staff to repost the panel."),
                ephemeral=True,
            )
            return

        _key, label, weapon_keys = cat
        pairs = resolve_weapon_roles(guild, bot.db, weapon_keys)
        if not pairs:
            await interaction.response.send_message(
                embed=info_embed(
                    "No weapon roles configured",
                    f"There are no weapon-tree roles set up under **{label}** yet. "
                    "Officers can run `/content-roles ensure-weapon-roles`.",
                ),
                ephemeral=True,
            )
            return

        pairs = pairs[:MAX_OPTIONS_PER_SELECT]
        view = WeaponPickerView(self.category_key, label, pairs, interaction.user)
        await interaction.response.send_message(
            embed=info_embed(
                f"Configure: {label}",
                "Select every weapon tree you can play for organized content, then click **Save**.",
            ),
            view=view,
            ephemeral=True,
        )


class WeaponRoleSelect(discord.ui.Select):
    def __init__(
        self,
        category_key: str,
        label: str,
        pairs: list[tuple[str, discord.Role]],
        member: discord.Member,
    ) -> None:
        member_role_ids = {r.id for r in member.roles}
        options: list[discord.SelectOption] = []
        for weapon_key, role in pairs:
            emoji, label_text = weapon_tree_label(weapon_key)
            options.append(discord.SelectOption(
                label=label_text[:100],
                value=str(role.id),
                emoji=emoji,
                default=role.id in member_role_ids,
            ))
        super().__init__(
            placeholder=f"Pick your {label} weapon trees…",
            min_values=0,
            max_values=len(options),
            options=options,
            custom_id=f"weapon_roles:select:{category_key}",
        )
        self.role_ids = [role.id for _k, role in pairs]

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()


class WeaponSaveButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Save",
            style=discord.ButtonStyle.success,
            emoji="✅",
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, WeaponPickerView):
            return
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message(
                embed=error_embed("Guild only", "Use this inside the server."),
                ephemeral=True,
            )
            return

        select = view.select
        wanted_ids = {int(v) for v in select.values}
        category_role_ids = set(select.role_ids)
        current_role_ids = {r.id for r in member.roles}
        to_add: list[discord.Role] = []
        to_remove: list[discord.Role] = []
        for rid in category_role_ids:
            role = interaction.guild.get_role(rid) if interaction.guild else None
            if role is None:
                continue
            if rid in wanted_ids and rid not in current_role_ids:
                to_add.append(role)
            elif rid not in wanted_ids and rid in current_role_ids:
                to_remove.append(role)

        try:
            if to_add:
                await member.add_roles(*to_add, reason="Self-assigned via weapon-roles panel")
            if to_remove:
                await member.remove_roles(*to_remove, reason="Self-removed via weapon-roles panel")
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=error_embed(
                    "Bot is missing permissions",
                    "I can't manage one of those roles — make sure my role is above all weapon-tree roles.",
                ),
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            error_log(f"weapon_roles save failed for {member}: {exc!r}")
            await interaction.response.send_message(
                embed=error_embed("Couldn't update roles", "Try again in a moment."),
                ephemeral=True,
            )
            return

        added_names = ", ".join(r.name for r in to_add) or "—"
        removed_names = ", ".join(r.name for r in to_remove) or "—"
        unchanged = len(category_role_ids) - len(to_add) - len(to_remove)
        embed = success_embed(
            f"{view.category_label} updated",
            f"**Added:** {added_names}\n**Removed:** {removed_names}\n"
            f"_Unchanged: {unchanged} role(s) in this category._",
        )
        for child in view.children:
            child.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(embed=embed, view=view)


class WeaponPickerView(discord.ui.View):
    def __init__(
        self,
        category_key: str,
        category_label: str,
        pairs: list[tuple[str, discord.Role]],
        member: discord.Member,
    ) -> None:
        super().__init__(timeout=180)
        self.category_label = category_label
        self.select = WeaponRoleSelect(category_key, category_label, pairs, member)
        self.add_item(self.select)
        self.add_item(WeaponSaveButton())


class WeaponRolesPanelView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        for cat_key, label, _types in WEAPON_CATEGORIES:
            self.add_item(WeaponCategoryButton(cat_key, label))
