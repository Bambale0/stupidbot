from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any

import httpx


class TBankError(RuntimeError):
    pass


@dataclass(slots=True)
class TBankClient:
    terminal_key: str | None
    password: str | None
    success_url: str | None = None
    fail_url: str | None = None
    base_url: str = "https://securepay.tinkoff.ru/v2"

    @property
    def is_configured(self) -> bool:
        return bool(self.terminal_key and self.password)

    async def init_payment(
        self,
        *,
        order_id: str,
        amount_kopecks: int,
        description: str,
        notification_url: str,
        customer_key: str,
    ) -> dict[str, Any]:
        if not self.is_configured:
            raise TBankError("TBANK_TERMINAL_KEY and TBANK_PASSWORD are not configured")

        payload: dict[str, Any] = {
            "TerminalKey": self.terminal_key,
            "Amount": amount_kopecks,
            "OrderId": order_id,
            "Description": description[:250],
            "NotificationURL": notification_url,
            "CustomerKey": customer_key,
        }
        if self.success_url:
            payload["SuccessURL"] = self.success_url
        if self.fail_url:
            payload["FailURL"] = self.fail_url
        payload["Token"] = self._token(payload)

        async with httpx.AsyncClient(base_url=self.base_url, timeout=30.0) as client:
            response = await client.post("/Init", json=payload)
        return self._decode(response)

    def verify_notification(self, payload: dict[str, Any]) -> bool:
        if not self.password:
            return False
        expected = payload.get("Token")
        if not expected:
            return False
        actual = self._token({key: value for key, value in payload.items() if key != "Token"})
        return hmac.compare_digest(str(expected).lower(), actual.lower())

    def _token(self, payload: dict[str, Any]) -> str:
        if not self.password:
            raise TBankError("TBANK_PASSWORD is not configured")
        token_payload: dict[str, Any] = {"Password": self.password}
        for key, value in payload.items():
            if key == "Token" or isinstance(value, (dict, list)) or value is None:
                continue
            token_payload[key] = value
        raw = "".join(self._token_value(token_payload[key]) for key in sorted(token_payload))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _token_value(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    @staticmethod
    def _decode(response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise TBankError(f"T-Bank returned non-JSON response: {response.text[:500]}") from exc
        if response.is_error or data.get("Success") is False:
            raise TBankError(f"T-Bank payment error: {data}")
        return data
