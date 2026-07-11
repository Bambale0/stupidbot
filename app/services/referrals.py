from __future__ import annotations

from aiogram import Bot


async def build_ref_link(bot: Bot | None, partner_code: str | None) -> str | None:
    if not bot or not partner_code:
        return None
    me = await bot.get_me()
    username = me.username
    if not username:
        return None
    return f"https://t.me/{username}?start=ref_{partner_code}"
