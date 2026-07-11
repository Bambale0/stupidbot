from __future__ import annotations

from dataclasses import dataclass

from aiogram import Bot, Dispatcher
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.services.comet import CometClient
from app.services.kie import KieClient
from app.services.tbank import TBankClient


@dataclass(slots=True)
class AppContext:
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    redis: Redis
    comet: CometClient
    kie: KieClient
    tbank: TBankClient
    bot: Bot | None = None
    dispatcher: Dispatcher | None = None
