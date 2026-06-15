"""Shared typing helpers for cog modules.

Cogs receive an instance of :class:`bot.UnionBot` (a subclass of
``discord.ext.commands.Bot``) which adds ``.db`` and a few other helpers.
Type checkers see only ``commands.Bot`` unless we tell them otherwise, which
produces hundreds of false-positive "Cannot access attribute db" diagnostics.

Cogs that touch ``self.bot.db`` should annotate ``self.bot: Bot = bot`` and
import :data:`Bot` from this module. At runtime the alias is just
``commands.Bot`` so nothing changes; only static analysis is improved.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot import UnionBot as Bot
else:
    from discord.ext.commands import Bot  # noqa: F401

__all__ = ["Bot"]
