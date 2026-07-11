from __future__ import annotations

from functools import lru_cache
from urllib.parse import quote

from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: str = "local"
    log_level: str = "INFO"
    host: str = "0.0.0.0"
    port: int = 8000

    telegram_bot_token: str = ""
    telegram_secret_token: str | None = None
    telegram_webhook_path: str = "/telegram/webhook"
    telegram_set_webhook: bool = False
    telegram_bot_username: str = "eva_nana_bot"
    public_base_url: str = "https://example.com"
    mini_app_path: str = "/miniapp"
    admin_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)

    database_url: str = "postgresql+asyncpg://stupidbot:stupidbot@127.0.0.1:5432/stupidbot"
    redis_url: str = "redis://127.0.0.1:6379/0"
    auto_create_db: bool = False

    comet_api_key: str | None = None
    comet_base_url: str = "https://api.cometapi.com"
    comet_image_simple_model: str = "gemini-3.1-flash-image-preview"
    comet_image_pro_model: str = "gemini-3-pro-image-preview"
    comet_image_2_model: str = "gemini-3.1-flash-image-preview"
    comet_kling_2_6_model: str = "kling-v2-6"
    comet_kling_3_0_model: str = "kling-v2-master"
    comet_seedance_2_model: str = "doubao-seedance-2-0"
    comet_callback_secret: str | None = None

    kie_api_key: str | None = None
    kie_base_url: str = "https://api.kie.ai"
    kie_upload_base_url: str = "https://kieai.redpandaai.co"
    kie_image_simple_model: str = "nano-banana-2"
    kie_image_pro_model: str = "nano-banana-pro"
    kie_image_2_model: str = "nano-banana-2"
    kie_kling_2_6_model: str = "kling-2.6/image-to-video"
    kie_kling_3_0_model: str = "kling-3.0/video"
    kie_kling_2_6_motion_control_model: str = "kling-2.6/motion-control"
    kie_kling_3_0_motion_control_model: str = "kling-3.0/motion-control"
    kie_seedance_2_model: str = "bytedance/seedance-2"

    tbank_terminal_key: str | None = None
    tbank_password: str | None = None
    tbank_success_url: str | None = None
    tbank_fail_url: str | None = None

    enabled_plugins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "core",
            "generation",
            "gallery",
            "feed",
            "partners",
            "payments",
            "admin",
        ],
    )

    @field_validator("admin_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, value: object) -> list[int]:
        if value in (None, ""):
            return []
        if isinstance(value, list):
            return [int(item) for item in value]
        if isinstance(value, str):
            return [int(item.strip()) for item in value.split(",") if item.strip()]
        raise TypeError("ADMIN_IDS must be a comma-separated string or a list")

    @field_validator("enabled_plugins", mode="before")
    @classmethod
    def parse_plugins(cls, value: object) -> list[str]:
        if value in (None, ""):
            return []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        raise TypeError("ENABLED_PLUGINS must be a comma-separated string or a list")

    @property
    def telegram_webhook_url(self) -> str:
        return f"{self.public_base_url.rstrip('/')}{self.telegram_webhook_path}"

    @property
    def mini_app_route(self) -> str:
        path = self.mini_app_path.strip() or "/miniapp"
        if not path.startswith("/"):
            path = f"/{path}"
        return path.rstrip("/") or "/miniapp"

    @property
    def mini_app_url(self) -> str:
        return f"{self.public_base_url.rstrip('/')}{self.mini_app_route}/"

    @property
    def comet_callback_url(self) -> str:
        url = f"{self.public_base_url.rstrip('/')}/comet/callback"
        if self.comet_callback_secret:
            return f"{url}?token={quote(self.comet_callback_secret, safe='')}"
        return url

    @property
    def tbank_callback_url(self) -> str:
        return f"{self.public_base_url.rstrip('/')}/payments/tbank/callback"


@lru_cache
def get_settings() -> Settings:
    return Settings()
