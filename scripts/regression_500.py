from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from html import escape
from typing import Any
from uuid import uuid4

if __package__ in {None, ""}:
    from _bootstrap import add_project_root_to_path

    add_project_root_to_path()

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import get_settings
from app.context import AppContext
from app.db import build_engine, init_db, session_scope
from app.models import (
    AffiliateWithdrawal,
    CreditPackage,
    GalleryItem,
    GenerationModel,
    GenerationTask,
    PartnerLink,
    Payment,
    User,
)
from app.plugins.admin import plugin as admin_plugin
from app.plugins.common import user_is_blocked
from app.plugins.feed import plugin as feed_plugin
from app.plugins.gallery import plugin as gallery_plugin
from app.plugins.generation import plugin as generation_plugin
from app.plugins.partners import plugin as partners_plugin
from app.repositories import (
    apply_affiliate_commission,
    bind_referral,
    credit_spend_from_payload,
    ensure_defaults,
    ensure_partner_code,
    get_feed_tasks,
    increment_feed_share,
    like_feed_task,
    normalize_ref_code,
    package_grants_value,
    package_is_technical,
    package_is_user_visible,
    remove_task_from_feed,
    refund_credits,
    share_task_to_feed,
    spend_user_credits,
    user_credit_balance,
)
from app.services.generation_catalog import (
    DEFAULT_MODELS,
    IMAGE_ASPECT_RATIOS,
    normalize_image_aspect_ratio,
    normalize_image_resolution,
)
from app.services.comet import _seedance_size
from app.services.kie import KieClient
from app.services.payments import (
    FAILED_STATUSES,
    PAID_STATUSES,
    PaymentPackageUnavailable,
    create_custom_credit_payment,
    create_package_payment,
    custom_credit_package_snapshot,
    handle_tbank_notification,
)
from app.services.task_tracker import TaskTracker, _extract_result_urls, _status_text_for_task
from app.services.tbank import TBankClient
from app.ui import main_menu


@dataclass
class Regression:
    scenarios: int = 0
    checks: int = 0
    failures: list[str] = field(default_factory=list)

    def scenario(self, name: str) -> str:
        self.scenarios += 1
        return name

    def check(self, scenario: str, condition: bool, detail: str = "") -> None:
        self.checks += 1
        if not condition:
            suffix = f" :: {detail}" if detail else ""
            self.failures.append(f"{scenario}{suffix}")

    def finish(self) -> None:
        if self.failures:
            preview = "\n".join(self.failures[:25])
            raise AssertionError(f"{len(self.failures)} regression failures:\n{preview}")


def _keyboard_callbacks(markup: Any) -> list[str]:
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]


def _keyboard_texts(markup: Any) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def _expected_commission(amount_kopecks: int, rate_bps: int) -> int:
    rate_bps = max(0, min(10000, rate_bps))
    return max(0, amount_kopecks * rate_bps // 10000)


def _signed_payload(
    client: TBankClient,
    *,
    order_id: str,
    amount_kopecks: int,
    status: str,
    success: bool,
    terminal_key: str = "terminal",
    payment_id: str | None = None,
    error_code: str = "0",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "TerminalKey": terminal_key,
        "OrderId": order_id,
        "Amount": amount_kopecks,
        "Status": status,
        "Success": success,
        "ErrorCode": error_code,
    }
    if payment_id is not None:
        payload["PaymentId"] = payment_id
    payload["Token"] = client._token(payload)
    return payload


def _check_static_logic(regression: Regression) -> None:
    settings = get_settings()
    for is_admin in (False, True):
        name = regression.scenario(f"main menu compact admin={is_admin}")
        texts = _keyboard_texts(
            main_menu(is_admin=is_admin, mini_app_url="https://example.com/miniapp")
        )
        regression.check(name, len(texts) == 5, str(texts))
        regression.check(
            name, texts == ["BANANA", "Создать фото", "AI Video", "Лента", "Еще"], str(texts)
        )

    name = regression.scenario("comet first kie fallback provider model separation")
    regression.check(name, settings.comet_image_simple_model == "gemini-3.1-flash-image-preview")
    regression.check(name, settings.comet_image_pro_model == "gemini-3-pro-image-preview")
    regression.check(name, settings.comet_image_2_model == "gemini-3.1-flash-image-preview")
    regression.check(name, settings.comet_kling_2_6_model == "kling-v2-6")
    regression.check(name, settings.comet_kling_3_0_model == "kling-v2-master")
    regression.check(name, settings.comet_seedance_2_model == "doubao-seedance-2-0")
    regression.check(name, settings.kie_kling_2_6_model == "kling-2.6/image-to-video")
    regression.check(name, settings.kie_kling_3_0_model == "kling-3.0/video")
    regression.check(
        name, settings.kie_kling_2_6_motion_control_model == "kling-2.6/motion-control"
    )
    regression.check(
        name, settings.kie_kling_3_0_motion_control_model == "kling-3.0/motion-control"
    )
    regression.check(name, settings.kie_seedance_2_model == "bytedance/seedance-2")

    name = regression.scenario("credit package public visibility filters technical and empty")
    public_package = CreditPackage(
        code="public-package",
        title="Публичный пакет",
        credits=10,
        price_rub=Decimal("100.00"),
        is_enabled=True,
    )
    technical_package = CreditPackage(
        code="scenario-package-static",
        title="Scenario Package Static",
        credits=10,
        price_rub=Decimal("100.00"),
        is_enabled=True,
    )
    empty_package = CreditPackage(
        code="empty-package",
        title="Пустой пакет",
        price_rub=Decimal("100.00"),
        is_enabled=True,
    )
    disabled_package = CreditPackage(
        code="disabled-package",
        title="Выключенный пакет",
        credits=10,
        price_rub=Decimal("100.00"),
        is_enabled=False,
    )
    regression.check(name, package_grants_value(public_package) is True)
    regression.check(name, package_is_user_visible(public_package) is True)
    regression.check(name, package_is_technical(technical_package) is True)
    regression.check(name, package_is_user_visible(technical_package) is False)
    regression.check(name, package_grants_value(empty_package) is False)
    regression.check(name, package_is_user_visible(empty_package) is False)
    regression.check(name, package_is_user_visible(disabled_package) is False)

    name = regression.scenario("kie success message response decode")
    decoded = KieClient._decode_response(
        httpx.Response(200, json={"code": 505, "msg": "success", "data": {"taskId": "task_1"}}),
        provider="KIE",
    )
    regression.check(name, decoded["data"]["taskId"] == "task_1")

    name = regression.scenario("motion control docs defaults")
    regression.check(name, generation_plugin.MOTION_CONTROL_MODE == "720p")
    regression.check(name, generation_plugin.MOTION_CONTROL_CHARACTER_ORIENTATION == "video")
    regression.check(name, generation_plugin.MOTION_CONTROL_IMAGE_ORIENTATION_MAX_SECONDS == 10)
    regression.check(
        name,
        KieClient.create_kling_motion_control_task.__kwdefaults__["character_orientation"]
        == "video",
    )

    for mime_type, filename, expected in [
        ("image/jpeg", "person.jpg", "image/jpeg"),
        ("image/jpg", "person.jpg", "image/jpeg"),
        ("image/png", "person.png", "image/png"),
        ("image/webp", "person.webp", None),
        ("", "person.png", "image/png"),
    ]:
        name = regression.scenario(f"motion image mime {mime_type!r} {filename!r}")
        regression.check(
            name,
            generation_plugin._normalize_motion_image_mime_type(mime_type, filename) == expected,
        )

    for mime_type, filename, expected in [
        ("video/mp4", "motion.mp4", "video/mp4"),
        ("video/quicktime", "motion.mov", "video/quicktime"),
        ("video/x-matroska", "motion.mkv", "video/x-matroska"),
        ("video/webm", "motion.webm", None),
        ("", "motion.mkv", "video/x-matroska"),
    ]:
        name = regression.scenario(f"motion video mime {mime_type!r} {filename!r}")
        regression.check(
            name,
            generation_plugin._normalize_motion_video_mime_type(mime_type, filename) == expected,
        )

    for price, seconds, free_generation, expected in [
        (12, 3, False, 36),
        (12, 7, False, 84),
        (16, 30, False, 480),
        (16, 30, True, 0),
        (0, 10, False, 0),
    ]:
        name = regression.scenario(
            f"motion control cost price={price} seconds={seconds} free={free_generation}"
        )
        regression.check(
            name,
            generation_plugin._motion_control_cost_credits(
                price_per_second=price,
                billable_seconds=seconds,
                free_generation=free_generation,
            )
            == expected,
        )

    name = regression.scenario("combined credits spend specific before universal")
    combo_user = User(
        telegram_id=100001,
        credits_balance=5,
        photo_credits_balance=2,
        video_credits_balance=1,
    )
    regression.check(name, user_credit_balance(combo_user, "photo") == 7)
    regression.check(name, user_credit_balance(combo_user, "video") == 6)
    photo_spend = spend_user_credits(combo_user, credit_type="photo", amount=4)
    regression.check(name, photo_spend == {"common": 2, "photo": 2}, str(photo_spend))
    regression.check(name, combo_user.photo_credits_balance == 0)
    regression.check(name, combo_user.credits_balance == 3)
    video_spend = spend_user_credits(combo_user, credit_type="video", amount=4)
    regression.check(name, video_spend == {"common": 3, "video": 1}, str(video_spend))
    regression.check(name, combo_user.video_credits_balance == 0)
    regression.check(name, combo_user.credits_balance == 0)
    regression.check(name, spend_user_credits(combo_user, credit_type="photo", amount=1) is None)

    name = regression.scenario("combined credits spend payload normalization")
    spend_payload = credit_spend_from_payload(
        {"credit_spend": {"image": "2", "video": 1, "common": 3, "photo": -5}}
    )
    regression.check(
        name, spend_payload == {"photo": 2, "video": 1, "common": 3}, str(spend_payload)
    )

    for raw, expected in [
        ("ref_uabc", "uabc"),
        ("ref-UABC", "uabc"),
        ("REF_uABC", "uabc"),
        (" ref-u123 ", "u123"),
        ("u999", "u999"),
        ("", ""),
        (None, ""),
    ]:
        name = regression.scenario(f"normalize ref code {raw!r}")
        regression.check(name, normalize_ref_code(raw) == expected)

    blocked_context = AppContext(
        settings=settings,
        session_factory=None,
        redis=None,
        comet=None,
        kie=KieClient(None),
        tbank=TBankClient(None, None),
        bot=None,
        dispatcher=None,
    )
    for user, expected in [
        (User(telegram_id=1, is_blocked=False, is_admin=False), False),
        (User(telegram_id=2, is_blocked=True, is_admin=False), True),
        (User(telegram_id=3, is_blocked=True, is_admin=True), False),
    ]:
        name = regression.scenario(f"blocked user gate tg={user.telegram_id}")
        regression.check(name, user_is_blocked(user, blocked_context) is expected)

    for value in [None, "", "2K", "1K", "4K", "bad", 0, " 2K "]:
        name = regression.scenario(f"resolution normalization {value!r}")
        regression.check(name, normalize_image_resolution(value) == (value.strip() if isinstance(value, str) and value.strip() in {"2K", "4K"} else "2K"))

    for value in [None, "", "1:1", "4:3", "16:9", "9:16", "3:2", 0]:
        name = regression.scenario(f"aspect normalization {value!r}")
        expected = value if value in IMAGE_ASPECT_RATIOS else "9:16"
        regression.check(name, normalize_image_aspect_ratio(value) == expected)

    name = regression.scenario("image aspect order prefers vertical then wide")
    regression.check(name, IMAGE_ASPECT_RATIOS[:2] == ["9:16", "16:9"], str(IMAGE_ASPECT_RATIOS))

    for aspect_ratio, resolution, expected in [
        ("16:9", "720p", "1280x720"),
        ("9:16", "720p", "720x1280"),
        ("1:1", "1080p", "1440x1440"),
        ("1280x720", "720p", "1280x720"),
        ("3:2", "720p", "3:2"),
        ("", "720p", "1280x720"),
    ]:
        name = regression.scenario(f"comet seedance size {aspect_ratio!r} {resolution!r}")
        regression.check(name, _seedance_size(aspect_ratio, resolution) == expected)

    for item in DEFAULT_MODELS:
        name = regression.scenario(f"default model catalog {item['code']}")
        regression.check(name, bool(item["code"]))
        regression.check(name, item["price_credits"] >= 0)
        if item["category"] == "image":
            regression.check(name, item["config"].get("resolutions") == ["2K", "4K"])
            regression.check(
                name, str(item["config"].get("provider_model", "")).endswith("-preview")
            )
            expected_max_images = {
                "nano-banana": 1,
                "nano-banana-pro": 8,
                "nano-banana-2": 14,
            }[item["code"]]
            regression.check(name, item["config"].get("max_images") == expected_max_images)
        if item["code"].startswith("kling"):
            regression.check(name, item["config"].get("provider_family") == "kling-motion-control")
            regression.check(name, item["config"].get("price_unit") == "second")
            regression.check(
                name, str(item["config"].get("provider_model", "")).endswith("/motion-control")
            )
            regression.check(name, item["config"].get("character_orientation") == "video")

    for rate_bps, expected_percent in [(None, 30), (0, 0), (3000, 30), (5000, 50)]:
        name = regression.scenario(f"admin user detail affiliate rate {rate_bps}")
        user = User(
            id=1,
            telegram_id=1001,
            username="tester",
            credits_balance=0,
            affiliate_commission_rate_bps=rate_bps,
        )
        text = admin_plugin._user_detail_text(
            user,
            env_admin=False,
            gallery_count=0,
            payments_count=0,
            payments_paid=0,
        )
        regression.check(name, f"Ставка партнерки: <b>{expected_percent}%</b>" in text)

    for links_count in range(5):
        links = [
            PartnerLink(
                id=index + 1, code=f"link-{index}", title=f"Link {index}", url="https://x.test"
            )
            for index in range(links_count)
        ]
        for can_withdraw in [False, True]:
            name = regression.scenario(
                f"partner keyboard links={links_count} withdraw={can_withdraw}"
            )
            callbacks = _keyboard_callbacks(partners_plugin._partner_keyboard(links, can_withdraw))
            regression.check(name, ("partner:withdraw" in callbacks) is can_withdraw)
            regression.check(
                name,
                len([item for item in callbacks if item.startswith("partner:open:")])
                == links_count,
            )

    for withdrawal_id in [1, 100, 999999]:
        name = regression.scenario(f"withdrawal admin keyboard {withdrawal_id}")
        callbacks = _keyboard_callbacks(partners_plugin._withdrawal_admin_keyboard(withdrawal_id))
        regression.check(name, f"partner:withdrawal:paid:{withdrawal_id}" in callbacks)
        regression.check(name, f"partner:withdrawal:reject:{withdrawal_id}" in callbacks)

    for details in ["card 12345", "<b>card</b>", "phone & telegram"]:
        name = regression.scenario(f"withdrawal details escaping {details}")
        regression.check(name, "<" not in escape(details) or "&lt;" in escape(details))

    name = regression.scenario("gallery caption html escaping")
    caption = gallery_plugin._gallery_caption(
        GalleryItem(title="<b>Title</b>", prompt="prompt & <script>alert(1)</script>")
    )
    regression.check(name, "<b>Title</b>" not in caption)
    regression.check(name, "&lt;b&gt;Title&lt;/b&gt;" in caption)
    regression.check(name, "&lt;script&gt;" in caption)

    name = regression.scenario("provider status html escaping")
    generation_status = generation_plugin._status_text("Ошибка", 100, "<bad & value>")
    tracker_status = _status_text_for_task(1, "fail", 100, {"error": "<provider & error>"})
    regression.check(name, "&lt;bad &amp; value&gt;" in generation_status)
    regression.check(name, "&lt;provider &amp; error&gt;" in tracker_status)

    name = regression.scenario("feed repeat keyboard labels and callback")
    image_callbacks = _keyboard_callbacks(
        feed_plugin._feed_keyboard(
            GenerationTask(id=123, user_id=1, model_code="nano-banana", likes_count=0),
            viewer_user_id=2,
            index=0,
            total=1,
        )
    )
    image_texts = _keyboard_texts(
        feed_plugin._feed_keyboard(
            GenerationTask(id=123, user_id=1, model_code="nano-banana", likes_count=0),
            viewer_user_id=2,
            index=0,
            total=1,
        )
    )
    video_texts = _keyboard_texts(
        feed_plugin._feed_keyboard(
            GenerationTask(id=124, user_id=1, model_code="seedance-2/video", likes_count=0),
            viewer_user_id=2,
            index=0,
            total=1,
        )
    )
    regression.check(name, "feed:repeat:123" in image_callbacks, str(image_callbacks))
    regression.check(name, "Повторить фото" in image_texts, str(image_texts))
    regression.check(name, "Повторить видео" in video_texts, str(video_texts))

    for raw, expected in [("123", 123), (123, 123), ("0", None), ("bad", None), (None, None)]:
        name = regression.scenario(f"miniapp source feed task id {raw!r}")
        regression.check(
            name,
            generation_plugin._mini_app_source_feed_task_id({"source_feed_task_id": raw})
            == expected,
        )

    for payload, expected in [
        (
            {"task_result": {"videos": [{"url": "https://cdn.test/a.mp4"}]}},
            ["https://cdn.test/a.mp4"],
        ),
        (
            {"resultJson": {"videos": [{"video_url": "https://cdn.test/b.mp4"}]}},
            ["https://cdn.test/b.mp4"],
        ),
        (
            {"resultJson": {"images": [{"image_url": "https://cdn.test/c.png"}]}},
            ["https://cdn.test/c.png"],
        ),
        ({"resultJson": {"resultUrls": ["https://cdn.test/d.mp4"]}}, ["https://cdn.test/d.mp4"]),
        (
            {"resultJson": '{"videos":[{"download_url":"https://cdn.test/e.mp4"}]}'},
            ["https://cdn.test/e.mp4"],
        ),
    ]:
        name = regression.scenario(f"task tracker extracts provider urls {expected[0]}")
        regression.check(name, _extract_result_urls(payload) == expected, str(payload))


class _FakeResultBot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        self.calls.append(("message", text))

    async def send_video(self, chat_id: int, video: str, **kwargs) -> None:
        self.calls.append(("video", video))

    async def send_photo(self, chat_id: int, photo: str, **kwargs) -> None:
        self.calls.append(("photo", photo))

    async def send_document(self, chat_id: int, document: str, **kwargs) -> None:
        self.calls.append(("document", document))


async def _check_result_delivery(regression: Regression) -> None:
    context = AppContext(
        settings=get_settings(),
        session_factory=None,
        redis=None,
        comet=None,
        kie=KieClient(None),
        tbank=TBankClient(None, None),
        bot=None,
        dispatcher=None,
    )
    for model_code, preview_call in [("nano-banana", "photo"), ("kling-3.0/video", "video")]:
        name = regression.scenario(f"result delivery preview and document {model_code}")
        bot = _FakeResultBot()
        tracker = TaskTracker(context, bot)  # type: ignore[arg-type]
        task = GenerationTask(
            id=99,
            chat_id=12345,
            user_id=1,
            model_code=model_code,
            result_urls=["https://cdn.test/result"],
        )
        await tracker._notify_success(task, task.result_urls)
        call_names = [item[0] for item in bot.calls]
        regression.check(name, preview_call in call_names, str(bot.calls))
        regression.check(name, "document" in call_names, str(bot.calls))
        regression.check(
            name, call_names.index(preview_call) < call_names.index("document"), str(bot.calls)
        )


async def _check_seeded_models(regression: Regression, session_factory) -> None:
    expected_codes = ["nano-banana", "nano-banana-pro", "nano-banana-2", "seedance-2/video"]
    async with session_factory() as session:
        models = list(
            await session.scalars(
                select(GenerationModel).where(GenerationModel.code.in_(expected_codes))
            )
        )
    by_code = {model.code: model for model in models}
    for code in expected_codes:
        name = regression.scenario(f"seeded generation model {code}")
        model = by_code.get(code)
        regression.check(name, model is not None, "model missing")
        if model:
            if code == "seedance-2/video":
                regression.check(name, model.category == "video")
                regression.check(name, model.config.get("provider_family") == "seedance")
                regression.check(name, model.config.get("fallback_model") == "bytedance/seedance-2")
            else:
                regression.check(name, model.category == "image")
                regression.check(name, model.config.get("resolutions") == ["2K", "4K"])
                regression.check(
                    name, str(model.config.get("provider_model", "")).endswith("-preview")
                )
                expected_max_images = {
                    "nano-banana": 1,
                    "nano-banana-pro": 8,
                    "nano-banana-2": 14,
                }[code]
                regression.check(name, model.config.get("max_images") == expected_max_images)


async def _check_affiliate_commissions(
    regression: Regression, session_factory, base_id: int
) -> int:
    rates = [-500, 0, 1, 999, 1000, 2999, 3000, 3333, 5000, 7500, 10000, 12000, 25000]
    amounts = [0, 1, 2, 9, 10, 99, 100, 101, 333, 999, 1000, 1001, 12345, 99999, 123456789]
    index = 0
    async with session_factory() as session:
        for rate_bps in rates:
            for amount in amounts:
                name = regression.scenario(f"affiliate commission rate={rate_bps} amount={amount}")
                referrer = User(
                    telegram_id=base_id + index * 3,
                    affiliate_commission_rate_bps=rate_bps,
                )
                session.add(referrer)
                await session.flush()
                buyer = User(
                    telegram_id=base_id + index * 3 + 1,
                    referred_by_user_id=referrer.id,
                )
                session.add(buyer)
                await session.flush()
                payment = Payment(
                    user_id=buyer.id,
                    provider="scenario",
                    order_id=f"affiliate-{base_id}-{index}",
                    amount_kopecks=amount,
                    status="paid",
                )
                session.add(payment)
                await session.flush()

                expected = _expected_commission(amount, rate_bps)
                actual = await apply_affiliate_commission(session, payment=payment, buyer=buyer)
                regression.check(name, actual == expected, f"expected={expected} actual={actual}")
                regression.check(name, referrer.affiliate_balance_kopecks == expected)
                regression.check(name, referrer.affiliate_earned_kopecks == expected)
                regression.check(name, payment.affiliate_commission_kopecks == expected)
                if referrer:
                    regression.check(name, payment.affiliate_commission_user_id == referrer.id)
                else:
                    regression.check(name, payment.affiliate_commission_user_id is None)

                second = await apply_affiliate_commission(session, payment=payment, buyer=buyer)
                regression.check(name, second == 0)
                regression.check(name, referrer.affiliate_balance_kopecks == expected)
                if expected == 0:
                    referrer.affiliate_commission_rate_bps = 5000
                    third = await apply_affiliate_commission(session, payment=payment, buyer=buyer)
                    regression.check(name, third == 0)
                    regression.check(name, referrer.affiliate_balance_kopecks == expected)
                index += 1

        for amount in amounts:
            name = regression.scenario(f"affiliate commission without referrer amount={amount}")
            buyer = User(telegram_id=base_id + index * 3 + 1)
            session.add(buyer)
            await session.flush()
            payment = Payment(
                user_id=buyer.id,
                provider="scenario",
                order_id=f"affiliate-no-ref-{base_id}-{index}",
                amount_kopecks=amount,
                status="paid",
            )
            session.add(payment)
            await session.flush()
            actual = await apply_affiliate_commission(session, payment=payment, buyer=buyer)
            regression.check(name, actual == 0)
            regression.check(name, payment.affiliate_commission_kopecks == 0)
            regression.check(name, payment.affiliate_commission_user_id is None)
            index += 1
    return index


async def _check_referral_binding(regression: Regression, session_factory, base_id: int) -> int:
    async with session_factory() as session:
        referrer = User(telegram_id=base_id, username="referrer")
        buyer = User(telegram_id=base_id + 1, username="buyer")
        blocked_referrer = User(telegram_id=base_id + 2, username="blocked", is_blocked=True)
        existing_referrer = User(telegram_id=base_id + 3, username="existing")
        session.add_all([referrer, buyer, blocked_referrer, existing_referrer])
        await session.flush()
        for user in (referrer, buyer, blocked_referrer, existing_referrer):
            await ensure_partner_code(session, user)
        await session.flush()

        name = regression.scenario("referral bind normalizes code")
        found = await admin_plugin._find_user(session, f"REF_{referrer.partner_code.upper()}")
        regression.check(name, found is not None)
        regression.check(name, found.id == referrer.id if found else False)
        bound = await bind_referral(
            session, user=buyer, ref_code=f"REF_{referrer.partner_code.upper()}"
        )
        regression.check(name, bound is not None)
        regression.check(name, bound.id == referrer.id if bound else False)
        regression.check(name, buyer.referred_by_user_id == referrer.id)

        name = regression.scenario("referral bind is immutable once set")
        rebound = await bind_referral(session, user=buyer, ref_code=existing_referrer.partner_code)
        regression.check(name, rebound is None)
        regression.check(name, buyer.referred_by_user_id == referrer.id)

        name = regression.scenario("referral rejects self referral")
        self_bound = await bind_referral(session, user=referrer, ref_code=referrer.partner_code)
        regression.check(name, self_bound is None)
        regression.check(name, referrer.referred_by_user_id is None)

        name = regression.scenario("referral rejects blocked referrer")
        blocked_buyer = User(telegram_id=base_id + 4, username="blocked-buyer")
        session.add(blocked_buyer)
        await session.flush()
        await ensure_partner_code(session, blocked_buyer)
        blocked_bound = await bind_referral(
            session,
            user=blocked_buyer,
            ref_code=blocked_referrer.partner_code,
        )
        regression.check(name, blocked_bound is None)
        regression.check(name, blocked_buyer.referred_by_user_id is None)

        name = regression.scenario("referral rejects direct cycle")
        cycle_a = User(telegram_id=base_id + 5, username="cycle-a")
        cycle_b = User(telegram_id=base_id + 6, username="cycle-b")
        session.add_all([cycle_a, cycle_b])
        await session.flush()
        await ensure_partner_code(session, cycle_a)
        await ensure_partner_code(session, cycle_b)
        cycle_b.referred_by_user_id = cycle_a.id
        cycle_bound = await bind_referral(session, user=cycle_a, ref_code=cycle_b.partner_code)
        regression.check(name, cycle_bound is None)
        regression.check(name, cycle_a.referred_by_user_id is None)

        name = regression.scenario("referral rejects longer cycle")
        chain_a = User(telegram_id=base_id + 7, username="chain-a")
        chain_b = User(telegram_id=base_id + 8, username="chain-b")
        chain_c = User(telegram_id=base_id + 9, username="chain-c")
        session.add_all([chain_a, chain_b, chain_c])
        await session.flush()
        for user in (chain_a, chain_b, chain_c):
            await ensure_partner_code(session, user)
        chain_b.referred_by_user_id = chain_a.id
        chain_c.referred_by_user_id = chain_b.id
        chain_bound = await bind_referral(session, user=chain_a, ref_code=chain_c.partner_code)
        regression.check(name, chain_bound is None)
        regression.check(name, chain_a.referred_by_user_id is None)
    return 10


async def _check_withdrawals(regression: Regression, session_factory, base_id: int) -> int:
    balances = [1, 99, 100, 101, 999, 1000, 12345, 50000, 100000, 250000]
    details_list = [
        "card 12345",
        "bank phone +79990000000",
        "sbp tester",
        "telegram @tester",
        "<b>html card</b>",
        "long details " + "x" * 200,
    ]
    outcomes = ["paid", "rejected"]
    index = 0
    async with session_factory() as session:
        for balance in balances:
            for details in details_list:
                for outcome in outcomes:
                    name = regression.scenario(
                        f"affiliate withdrawal balance={balance} details={len(details)} outcome={outcome}"
                    )
                    user = User(
                        telegram_id=base_id + index,
                        affiliate_balance_kopecks=balance,
                        affiliate_earned_kopecks=balance,
                    )
                    session.add(user)
                    await session.flush()
                    amount = int(user.affiliate_balance_kopecks or 0)
                    user.affiliate_balance_kopecks = 0
                    withdrawal = AffiliateWithdrawal(
                        user_id=user.id,
                        amount_kopecks=amount,
                        status="pending",
                        details=details[:1200],
                    )
                    session.add(withdrawal)
                    await session.flush()

                    regression.check(name, withdrawal.amount_kopecks == balance)
                    regression.check(name, user.affiliate_balance_kopecks == 0)
                    regression.check(name, withdrawal.status == "pending")
                    regression.check(name, withdrawal.details == details[:1200])

                    if outcome == "rejected":
                        withdrawal.status = "rejected"
                        user.affiliate_balance_kopecks += withdrawal.amount_kopecks
                        regression.check(name, user.affiliate_balance_kopecks == balance)
                    else:
                        withdrawal.status = "paid"
                        regression.check(name, user.affiliate_balance_kopecks == 0)
                    regression.check(name, withdrawal.status == outcome)
                    index += 1
    return index


async def _check_feed_workflows(regression: Regression, session_factory, base_id: int) -> int:
    statuses = ["draft", "submitted", "generating", "fail", "success"]
    result_sets = [[], ["https://example.com/feed.jpg"]]
    index = 0
    async with session_factory() as session:
        owner = User(telegram_id=base_id)
        other = User(telegram_id=base_id + 1)
        session.add_all([owner, other])
        await session.flush()

        foreign_source = GenerationTask(
            user_id=other.id,
            model_code="nano-banana",
            status="success",
            prompt="foreign source",
            result_urls=["https://example.com/source.jpg"],
        )
        session.add(foreign_source)
        await session.flush()

        for status in statuses:
            for result_urls in result_sets:
                name = regression.scenario(f"feed publish status={status} urls={len(result_urls)}")
                task = GenerationTask(
                    user_id=owner.id,
                    model_code="nano-banana",
                    status=status,
                    prompt="feed prompt",
                    result_urls=list(result_urls),
                    input_payload={"resolution": "2K"},
                )
                session.add(task)
                await session.flush()
                ok, reason = await share_task_to_feed(session, task_id=task.id, user_id=owner.id)
                expected_ok = status == "success" and bool(result_urls)
                regression.check(name, ok is expected_ok, f"reason={reason}")
                regression.check(name, task.is_public_feed is expected_ok)
                if expected_ok:
                    regression.check(name, task.feed_status == "approved")
                    regression.check(name, task.published_at is not None)
                    likes, is_new = await like_feed_task(session, task_id=task.id, user_id=owner.id)
                    regression.check(name, likes == 1 and is_new)
                    likes, is_new = await like_feed_task(session, task_id=task.id, user_id=owner.id)
                    regression.check(name, likes == 1 and not is_new)
                    shares = await increment_feed_share(session, task.id)
                    regression.check(name, shares == 1)
                    feed_items = await get_feed_tasks(session, limit=20)
                    regression.check(name, any(item.id == task.id for item in feed_items))
                    removed = await remove_task_from_feed(
                        session, task_id=task.id, user_id=owner.id
                    )
                    regression.check(name, removed)
                    regression.check(name, task.is_public_feed is False)
                index += 1

        derivative = GenerationTask(
            user_id=owner.id,
            model_code="nano-banana",
            status="success",
            prompt="derivative",
            result_urls=["https://example.com/derivative.jpg"],
            source_feed_task_id=foreign_source.id,
        )
        session.add(derivative)
        await session.flush()
        name = regression.scenario("feed blocks foreign derivative")
        ok, reason = await share_task_to_feed(session, task_id=derivative.id, user_id=owner.id)
        regression.check(name, ok is False)
        regression.check(name, reason == "foreign_source")
        index += 1
    return index


async def _check_payment_creation(regression: Regression, session_factory, base_id: int) -> int:
    async with session_scope(session_factory) as session:
        user = User(telegram_id=base_id + 1)
        package = CreditPackage(
            code=f"miniapp-package-{base_id}",
            title="Mini App Package",
            terms="Тестовые условия",
            credits=12,
            photo_credits=3,
            video_credits=1,
            price_rub=Decimal("199.90"),
            is_enabled=True,
        )
        disabled_package = CreditPackage(
            code=f"miniapp-disabled-package-{base_id}",
            title="Disabled Mini App Package",
            credits=1,
            price_rub=Decimal("10.00"),
            is_enabled=False,
        )
        empty_package = CreditPackage(
            code=f"miniapp-empty-package-{base_id}",
            title="Empty Mini App Package",
            price_rub=Decimal("99.00"),
            is_enabled=True,
        )
        empty_unlimited_package = CreditPackage(
            code=f"miniapp-empty-unlimited-package-{base_id}",
            title="Empty Unlimited Mini App Package",
            price_rub=Decimal("199.00"),
            is_unlimited=True,
            is_enabled=True,
        )
        unlimited_package = CreditPackage(
            code=f"miniapp-unlimited-package-{base_id}",
            title="Unlimited Mini App Package",
            price_rub=Decimal("299.00"),
            is_unlimited=True,
            duration_days=30,
            is_enabled=True,
        )
        technical_package = CreditPackage(
            code=f"scenario-package-{base_id}-payment",
            title="Scenario Package Payment",
            credits=1,
            price_rub=Decimal("10.00"),
            is_enabled=True,
        )
        session.add_all(
            [
                user,
                package,
                disabled_package,
                empty_package,
                empty_unlimited_package,
                unlimited_package,
                technical_package,
            ]
        )
        await session.flush()
        user_id = user.id
        package_id = package.id
        disabled_package_id = disabled_package.id
        empty_package_id = empty_package.id
        empty_unlimited_package_id = empty_unlimited_package.id
        unlimited_package_id = unlimited_package.id
        technical_package_id = technical_package.id

    context = AppContext(
        settings=get_settings(),
        session_factory=session_factory,
        redis=None,
        comet=None,
        kie=KieClient(None),
        tbank=TBankClient(None, None),
        bot=None,
        dispatcher=None,
    )

    name = regression.scenario("miniapp payment creation manual pending")
    result = await create_package_payment(
        context,
        user_id=user_id,
        package_id=package_id,
        customer_key=str(base_id + 1),
        source="miniapp",
    )
    regression.check(name, result.status == "manual_pending")
    regression.check(name, result.payment_url is None)
    regression.check(name, result.amount_kopecks == 19990)
    regression.check(name, result.package_snapshot["package_id"] == package_id)
    regression.check(name, result.package_snapshot["title"] == "Mini App Package")
    async with session_factory() as session:
        payment = await session.get(Payment, result.payment_id)
        regression.check(name, payment is not None)
        if payment:
            raw_payload = dict(payment.raw_payload or {})
            regression.check(name, payment.status == "manual_pending")
            regression.check(name, payment.order_id == result.order_id)
            regression.check(name, payment.amount_kopecks == 19990)
            regression.check(name, raw_payload.get("source") == "miniapp")
            regression.check(
                name, raw_payload.get("package_snapshot", {}).get("package_id") == package_id
            )

    name = regression.scenario("miniapp custom credit payment creation manual pending")
    custom_result = await create_custom_credit_payment(
        context,
        user_id=user_id,
        credits=25,
        customer_key=str(base_id + 1),
        source="miniapp",
    )
    regression.check(name, custom_result.status == "manual_pending")
    regression.check(name, custom_result.payment_url is None)
    regression.check(name, custom_result.amount_kopecks == 2500)
    regression.check(name, custom_result.package_snapshot["package_id"] is None)
    regression.check(name, custom_result.package_snapshot["credits"] == 25)
    async with session_factory() as session:
        payment = await session.get(Payment, custom_result.payment_id)
        regression.check(name, payment is not None)
        if payment:
            raw_payload = dict(payment.raw_payload or {})
            regression.check(name, payment.package_id is None)
            regression.check(name, payment.status == "manual_pending")
            regression.check(name, raw_payload.get("source") == "miniapp")
            regression.check(name, raw_payload.get("package_snapshot", {}).get("credits") == 25)

    name = regression.scenario("miniapp payment creation rejects disabled package")
    try:
        await create_package_payment(
            context,
            user_id=user_id,
            package_id=disabled_package_id,
            customer_key=str(base_id + 1),
            source="miniapp",
        )
    except PaymentPackageUnavailable:
        regression.check(name, True)
    else:
        regression.check(name, False, "disabled package was accepted")

    for scenario_name, unavailable_package_id in [
        ("miniapp payment creation rejects empty package", empty_package_id),
        ("miniapp payment creation rejects empty unlimited package", empty_unlimited_package_id),
        ("miniapp payment creation rejects technical package", technical_package_id),
    ]:
        name = regression.scenario(scenario_name)
        try:
            await create_package_payment(
                context,
                user_id=user_id,
                package_id=unavailable_package_id,
                customer_key=str(base_id + 1),
                source="miniapp",
            )
        except PaymentPackageUnavailable:
            regression.check(name, True)
        else:
            regression.check(name, False, "empty package was accepted")

    name = regression.scenario("miniapp payment creation accepts duration unlimited package")
    unlimited_result = await create_package_payment(
        context,
        user_id=user_id,
        package_id=unlimited_package_id,
        customer_key=str(base_id + 1),
        source="miniapp",
    )
    regression.check(name, unlimited_result.status == "manual_pending")
    regression.check(name, unlimited_result.package_snapshot["is_unlimited"] is True)
    regression.check(name, unlimited_result.package_snapshot["duration_days"] == 30)

    name = regression.scenario("combined credits refund allocation restores exact buckets")
    async with session_scope(session_factory) as session:
        refund_user = User(telegram_id=base_id + 2)
        session.add(refund_user)
        await session.flush()
        refund_user_id = refund_user.id
        await refund_credits(
            session,
            user_id=refund_user_id,
            credits=4,
            credit_type="photo",
            allocation={"photo": 2, "common": 2},
        )
    async with session_factory() as session:
        refund_user = await session.get(User, refund_user_id)
        regression.check(name, refund_user.photo_credits_balance == 2)
        regression.check(name, refund_user.credits_balance == 2)
        regression.check(name, refund_user.video_credits_balance == 0)

    return 8


async def _create_payment_case(
    session_factory,
    *,
    base_id: int,
    index: int,
    amount_kopecks: int,
    credits: int,
    ref_rate_bps: int | None,
    provider_payment_id: str | None = None,
) -> tuple[int, int | None, int, str]:
    async with session_scope(session_factory) as session:
        referrer = None
        if ref_rate_bps is not None:
            referrer = User(
                telegram_id=base_id + index * 4,
                affiliate_commission_rate_bps=ref_rate_bps,
            )
            session.add(referrer)
            await session.flush()
        user = User(
            telegram_id=base_id + index * 4 + 1,
            referred_by_user_id=referrer.id if referrer else None,
        )
        package = CreditPackage(
            code=f"scenario-package-{base_id}-{index}",
            title=f"Scenario Package {index}",
            credits=credits,
            price_rub=Decimal(amount_kopecks) / Decimal(100),
            is_enabled=False,
        )
        session.add_all([user, package])
        await session.flush()
        order_id = f"scenario-order-{base_id}-{index}"
        payment = Payment(
            user_id=user.id,
            package_id=package.id,
            provider="tbank",
            provider_payment_id=provider_payment_id,
            order_id=order_id,
            amount_kopecks=amount_kopecks,
            status="created",
        )
        session.add(payment)
        await session.flush()
        return user.id, referrer.id if referrer else None, payment.id, order_id


async def _check_payments(regression: Regression, session_factory, base_id: int) -> int:
    tbank = TBankClient("terminal", "secret")
    context = AppContext(
        settings=get_settings(),
        session_factory=session_factory,
        redis=None,
        comet=None,
        kie=KieClient(None),
        tbank=tbank,
        bot=None,
        dispatcher=None,
    )
    statuses = [("CONFIRMED", True), *[(status, False) for status in sorted(FAILED_STATUSES)]]
    statuses.extend([("NEW", False), ("AUTHORIZED", False), ("FORM_SHOWED", False)])
    amounts = [100, 999, 10000, 12345]
    referral_rates = [None, 3000, 5000]
    index = 0

    for status, success in statuses:
        for amount in amounts:
            for ref_rate_bps in referral_rates:
                name = regression.scenario(
                    f"tbank callback status={status} amount={amount} ref={ref_rate_bps}"
                )
                user_id, referrer_id, payment_id, order_id = await _create_payment_case(
                    session_factory,
                    base_id=base_id,
                    index=index,
                    amount_kopecks=amount,
                    credits=7,
                    ref_rate_bps=ref_rate_bps,
                )
                payload = _signed_payload(
                    tbank,
                    order_id=order_id,
                    amount_kopecks=amount,
                    status=status,
                    success=success,
                    payment_id=f"pid-{index}",
                )
                accepted = await handle_tbank_notification(context, payload)
                async with session_factory() as session:
                    user = await session.get(User, user_id)
                    payment = await session.get(Payment, payment_id)
                    referrer = await session.get(User, referrer_id) if referrer_id else None
                    is_paid = success and status in PAID_STATUSES
                    expected_commission = (
                        _expected_commission(amount, ref_rate_bps)
                        if is_paid and ref_rate_bps is not None
                        else 0
                    )
                    regression.check(name, accepted is True)
                    regression.check(name, payment is not None)
                    if payment and user:
                        regression.check(name, payment.provider_payment_id == f"pid-{index}")
                        regression.check(name, user.credits_balance == (7 if is_paid else 0))
                        regression.check(
                            name,
                            payment.status == ("paid" if is_paid else status.lower()),
                            f"status={payment.status}",
                        )
                        regression.check(
                            name, payment.affiliate_commission_kopecks == expected_commission
                        )
                    if referrer:
                        regression.check(
                            name, referrer.affiliate_balance_kopecks == expected_commission
                        )

                if success and status in PAID_STATUSES:
                    duplicate = await handle_tbank_notification(context, payload)
                    async with session_factory() as session:
                        user = await session.get(User, user_id)
                        payment = await session.get(Payment, payment_id)
                        referrer = await session.get(User, referrer_id) if referrer_id else None
                        regression.check(name, duplicate is True)
                        regression.check(name, user.credits_balance == 7)
                        regression.check(
                            name, payment.affiliate_commission_kopecks == expected_commission
                        )
                        if referrer:
                            regression.check(
                                name,
                                referrer.affiliate_balance_kopecks == expected_commission,
                            )
                index += 1

    invalidators = [
        "bad_token",
        "amount_mismatch",
        "terminal_mismatch",
        "payment_id_mismatch",
        "success_error_code",
    ]
    for invalidator in invalidators:
        for amount in amounts:
            for ref_rate_bps in [None, 3000]:
                name = regression.scenario(
                    f"tbank invalid callback {invalidator} amount={amount} ref={ref_rate_bps}"
                )
                provider_payment_id = "known-pid" if invalidator == "payment_id_mismatch" else None
                user_id, referrer_id, payment_id, order_id = await _create_payment_case(
                    session_factory,
                    base_id=base_id,
                    index=index,
                    amount_kopecks=amount,
                    credits=11,
                    ref_rate_bps=ref_rate_bps,
                    provider_payment_id=provider_payment_id,
                )
                payload_amount = amount + 1 if invalidator == "amount_mismatch" else amount
                terminal = "other-terminal" if invalidator == "terminal_mismatch" else "terminal"
                payment_id_value = (
                    "other-pid" if invalidator == "payment_id_mismatch" else f"pid-{index}"
                )
                error_code = "5" if invalidator == "success_error_code" else "0"
                payload = _signed_payload(
                    tbank,
                    order_id=order_id,
                    amount_kopecks=payload_amount,
                    status="CONFIRMED",
                    success=True,
                    terminal_key=terminal,
                    payment_id=payment_id_value,
                    error_code=error_code,
                )
                if invalidator == "bad_token":
                    payload["Token"] = "bad-token"
                accepted = await handle_tbank_notification(context, payload)
                async with session_factory() as session:
                    user = await session.get(User, user_id)
                    payment = await session.get(Payment, payment_id)
                    referrer = await session.get(User, referrer_id) if referrer_id else None
                    regression.check(name, accepted is (invalidator != "bad_token"))
                    regression.check(name, user.credits_balance == 0)
                    regression.check(name, payment.affiliate_commission_kopecks == 0)
                    if invalidator == "bad_token":
                        regression.check(name, payment.status == "created")
                    else:
                        regression.check(name, payment.status == "invalid_callback")
                        regression.check(name, "_validation_error" in payment.raw_payload)
                    if referrer:
                        regression.check(name, referrer.affiliate_balance_kopecks == 0)
                index += 1

    name = regression.scenario("tbank callback custom credits without package")
    custom_amount_kopecks = 2500
    custom_credits = 25
    async with session_scope(session_factory) as session:
        user = User(telegram_id=base_id + index * 4 + 1)
        session.add(user)
        await session.flush()
        user_id = user.id
        order_id = f"custom-credit-order-{base_id}-{index}"
        payment = Payment(
            user_id=user.id,
            package_id=None,
            provider="tbank",
            order_id=order_id,
            amount_kopecks=custom_amount_kopecks,
            status="created",
            raw_payload={
                "package_snapshot": custom_credit_package_snapshot(custom_credits),
                "source": "test",
            },
        )
        session.add(payment)
        await session.flush()
        payment_id = payment.id
    payload = _signed_payload(
        tbank,
        order_id=order_id,
        amount_kopecks=custom_amount_kopecks,
        status="CONFIRMED",
        success=True,
        payment_id=f"custom-pid-{index}",
    )
    accepted = await handle_tbank_notification(context, payload)
    async with session_factory() as session:
        user = await session.get(User, user_id)
        payment = await session.get(Payment, payment_id)
        regression.check(name, accepted is True)
        regression.check(name, payment.package_id is None)
        regression.check(name, payment.status == "paid")
        regression.check(name, user.credits_balance == custom_credits)
        regression.check(name, user.photo_credits_balance == 0)
        regression.check(name, user.video_credits_balance == 0)
    duplicate = await handle_tbank_notification(context, payload)
    async with session_factory() as session:
        user = await session.get(User, user_id)
        regression.check(name, duplicate is True)
        regression.check(name, user.credits_balance == custom_credits)
    index += 1
    return index


async def amain() -> None:
    regression = Regression()
    _check_static_logic(regression)
    await _check_result_delivery(regression)

    settings = get_settings()
    engine = build_engine(settings)
    await init_db(engine)
    async with engine.connect() as conn:
        transaction = await conn.begin()
        session_factory = async_sessionmaker(conn, expire_on_commit=False, autoflush=False)
        try:
            suffix = int(uuid4().hex[:8], 16)
            base_id = 7_000_000_000_000 + suffix
            async with session_scope(session_factory) as session:
                await ensure_defaults(session, settings.admin_ids)
            await _check_seeded_models(regression, session_factory)
            used = await _check_affiliate_commissions(regression, session_factory, base_id)
            used += await _check_referral_binding(regression, session_factory, base_id + 50_000)
            used += await _check_withdrawals(regression, session_factory, base_id + 100_000)
            used += await _check_feed_workflows(regression, session_factory, base_id + 200_000)
            used += await _check_payment_creation(
                regression, session_factory, base_id + 300_000 + used
            )
            await _check_payments(regression, session_factory, base_id + 350_000 + used)
        finally:
            await transaction.rollback()
    await engine.dispose()
    regression.finish()
    print(f"regression scenarios passed: {regression.scenarios}")
    print(f"regression checks passed: {regression.checks}")


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
