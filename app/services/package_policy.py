from __future__ import annotations

import importlib
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable

from fastapi import FastAPI

PACKAGE_CATALOG_PATH = "/api/tma/app/packages"
_NO_STORE_HEADERS = (
    (b"cache-control", b"no-store, max-age=0"),
    (b"pragma", b"no-cache"),
    (b"expires", b"0"),
)
_INSTALLED = False


def _positive_price(value: Any) -> bool:
    try:
        return Decimal(str(value or "0")) > 0
    except (InvalidOperation, ValueError, TypeError):
        return False


def strict_package_is_user_visible(package: Any) -> bool:
    """Public sale policy shared by Telegram, Mini App and payment creation."""

    from app import repositories

    original = getattr(
        repositories,
        "_package_policy_original_is_user_visible",
        repositories.package_is_user_visible,
    )
    return bool(original(package)) and _positive_price(getattr(package, "price_rub", None))


def _install_no_store_headers() -> None:
    current = FastAPI.__call__
    if getattr(current, "_dynamic_package_policy_installed", False):
        return

    original_call = current

    async def wrapped_call(
        self: FastAPI,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http" or scope.get("path") != PACKAGE_CATALOG_PATH:
            await original_call(self, scope, receive, send)
            return

        async def send_no_store(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                blocked = {b"cache-control", b"pragma", b"expires"}
                headers = [
                    (key, value)
                    for key, value in list(message.get("headers") or [])
                    if key.lower() not in blocked
                ]
                copied = dict(message)
                copied["headers"] = [*headers, *_NO_STORE_HEADERS]
                message = copied
            await send(message)

        await original_call(self, scope, receive, send_no_store)

    setattr(wrapped_call, "_dynamic_package_policy_installed", True)
    setattr(wrapped_call, "_dynamic_package_policy_original", original_call)
    FastAPI.__call__ = wrapped_call


def install_package_policy() -> None:
    """Install fail-closed public package and custom-credit sales rules once."""

    global _INSTALLED
    if _INSTALLED:
        return

    repositories = importlib.import_module("app.repositories")
    payments = importlib.import_module("app.services.payments")

    original_visible = getattr(
        repositories,
        "_package_policy_original_is_user_visible",
        repositories.package_is_user_visible,
    )
    setattr(repositories, "_package_policy_original_is_user_visible", original_visible)
    repositories.package_is_user_visible = strict_package_is_user_visible

    # payments.py imports this function by value, so patch its local binding too.
    payments.package_is_user_visible = strict_package_is_user_visible

    async def custom_credit_sales_disabled(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise payments.PaymentCreditAmountInvalid("custom_credit_sales_disabled")

    setattr(custom_credit_sales_disabled, "_dynamic_package_policy_installed", True)
    payments.create_custom_credit_payment = custom_credit_sales_disabled
    payments.CUSTOM_CREDIT_PRICE_RUB = Decimal("0")
    payments.CUSTOM_CREDIT_MIN_AMOUNT = 0
    payments.CUSTOM_CREDIT_MAX_AMOUNT = 0

    _install_no_store_headers()
    _INSTALLED = True
