from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

import httpx


class CometApiError(RuntimeError):
    pass


@dataclass(slots=True)
class CometImageReference:
    content: bytes
    mime_type: str


@dataclass(slots=True)
class CometGeneratedImage:
    content: bytes
    mime_type: str


@dataclass(slots=True)
class CometImageResult:
    images: list[CometGeneratedImage]
    text_parts: list[str]
    metadata: dict[str, Any]


SEEDANCE_2_SIZES: dict[str, dict[str, str]] = {
    "480p": {
        "16:9": "864x496",
        "4:3": "752x560",
        "1:1": "640x640",
        "3:4": "560x752",
        "9:16": "496x864",
        "21:9": "992x432",
    },
    "720p": {
        "16:9": "1280x720",
        "4:3": "1112x834",
        "1:1": "960x960",
        "3:4": "834x1112",
        "9:16": "720x1280",
        "21:9": "1470x630",
    },
    "1080p": {
        "16:9": "1920x1080",
        "4:3": "1664x1248",
        "1:1": "1440x1440",
        "3:4": "1248x1664",
        "9:16": "1080x1920",
        "21:9": "2206x946",
    },
}
DEFAULT_SEEDANCE_SIZE = SEEDANCE_2_SIZES["720p"]["16:9"]


@dataclass(slots=True)
class CometClient:
    api_key: str | None
    base_url: str = "https://api.cometapi.com"
    timeout: float = 180.0

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise CometApiError("COMET_API_KEY is not configured")
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _auth_headers(self) -> dict[str, str]:
        if not self.api_key:
            raise CometApiError("COMET_API_KEY is not configured")
        return {"Authorization": f"Bearer {self.api_key}"}

    async def generate_image(
        self,
        *,
        model: str,
        prompt: str,
        reference_images: list[CometImageReference] | None = None,
        aspect_ratio: str | None = None,
        image_size: str | None = None,
        output_mime_type: str | None = None,
    ) -> CometImageResult:
        payload = self._build_banana_image_payload(
            prompt=prompt,
            reference_images=reference_images or [],
            aspect_ratio=aspect_ratio,
            image_size=image_size,
        )
        response_data = await self._post_json(f"/v1beta/models/{model}:generateContent", payload)
        return self._extract_image_result(
            response_data=response_data,
            model=model,
            output_mime_type=output_mime_type,
        )

    async def create_seedance_video_task(
        self,
        *,
        model: str,
        prompt: str,
        image: bytes | None = None,
        image_mime_type: str = "image/jpeg",
        image_filename: str = "seedance-reference.jpg",
        duration: str = "5",
        aspect_ratio: str = "16:9",
        resolution: str = "720p",
    ) -> str:
        fields: dict[str, Any] = {
            "prompt": (None, prompt),
            "model": (None, model),
            "seconds": (None, str(duration)),
            "size": (None, _seedance_size(aspect_ratio, resolution)),
        }
        if image:
            fields["input_reference"] = (image_filename, image, image_mime_type)

        response_data = await self._post_form("/v1/videos", fields)
        task_id = response_data.get("id") or response_data.get("task_id")
        data = response_data.get("data") if isinstance(response_data.get("data"), dict) else {}
        task_id = task_id or data.get("id") or data.get("task_id") or data.get("taskId")
        if not task_id:
            raise CometApiError(f"Comet Seedance response does not contain task id: {response_data}")
        return str(task_id)

    async def query_seedance_video_task(self, task_id: str) -> dict[str, Any]:
        response_data = await self._get_json(f"/v1/videos/{task_id}")
        data = response_data.get("data") if isinstance(response_data.get("data"), dict) else response_data
        normalized = dict(data)
        raw_state = str(
            normalized.get("status")
            or normalized.get("state")
            or normalized.get("task_status")
            or ""
        )
        normalized["state"] = _normalize_comet_video_state(raw_state)
        urls = _extract_seedance_urls(normalized)
        if urls:
            normalized["resultUrls"] = urls
        if normalized["state"] not in {"success", "fail"} and urls and _progress_complete(normalized.get("progress")):
            normalized["state"] = "success"
        return normalized

    async def create_kling_image_to_video_task(
        self,
        *,
        model_name: str,
        image: str,
        prompt: str,
        mode: str = "pro",
        duration: str = "5",
        callback_url: str | None = None,
        negative_prompt: str | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "model_name": model_name,
            "image": image,
            "prompt": prompt,
            "mode": mode,
            "duration": duration,
        }
        if callback_url:
            payload["callback_url"] = callback_url
        if negative_prompt:
            payload["negative_prompt"] = negative_prompt[:200]
        response_data = await self._post_json("/kling/v1/videos/image2video", payload)
        data = response_data.get("data") or {}
        task_id = data.get("task_id") or data.get("taskId")
        if not task_id:
            raise CometApiError(f"Comet Kling response does not contain task_id: {response_data}")
        return str(task_id)

    async def query_kling_image_to_video_task(self, task_id: str) -> dict[str, Any]:
        response_data = await self._get_json(f"/kling/v1/videos/image2video/{task_id}")
        data = response_data.get("data") if isinstance(response_data.get("data"), dict) else {}
        normalized = dict(data)
        raw_state = str(normalized.get("task_status") or normalized.get("status") or "")
        normalized["state"] = _normalize_kling_state(raw_state)
        urls = _extract_kling_urls(normalized)
        if urls:
            normalized["resultUrls"] = urls
        return normalized

    async def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
            response = await client.post(path, headers=self._headers(), json=payload)
        return self._decode_response(response, provider="Comet")

    async def _post_form(self, path: str, fields: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
            response = await client.post(path, headers=self._auth_headers(), files=fields)
        return self._decode_response(response, provider="Comet")

    async def _get_json(self, path: str) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
            response = await client.get(path, headers=self._headers())
        return self._decode_response(response, provider="Comet")

    @staticmethod
    def _decode_response(response: httpx.Response, *, provider: str) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise CometApiError(f"{provider} returned non-JSON response: {response.text[:500]}") from exc
        if response.is_error:
            raise CometApiError(f"{provider} HTTP {response.status_code}: {data}")
        code = data.get("code")
        if code not in (None, 0, 200):
            raise CometApiError(f"{provider} API error {code}: {data.get('message') or data}")
        return data

    @staticmethod
    def _build_banana_image_payload(
        *,
        prompt: str,
        reference_images: list[CometImageReference],
        aspect_ratio: str | None,
        image_size: str | None,
    ) -> dict[str, Any]:
        parts: list[dict[str, Any]] = [{"text": prompt}]
        for image in reference_images:
            parts.append(
                {
                    "inline_data": {
                        "mime_type": image.mime_type,
                        "data": base64.b64encode(image.content).decode("ascii"),
                    }
                }
            )

        generation_config: dict[str, Any] = {"responseModalities": ["TEXT", "IMAGE"]}
        image_config: dict[str, str] = {}
        if aspect_ratio and aspect_ratio != "auto":
            image_config["aspectRatio"] = aspect_ratio
        if image_size:
            image_config["imageSize"] = image_size
        if image_config:
            generation_config["imageConfig"] = image_config

        return {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": generation_config,
        }

    @staticmethod
    def _extract_image_result(
        *,
        response_data: dict[str, Any],
        model: str,
        output_mime_type: str | None,
    ) -> CometImageResult:
        candidates = response_data.get("candidates") or []
        text_parts: list[str] = []
        images: list[CometGeneratedImage] = []
        for candidate in candidates:
            content = candidate.get("content") if isinstance(candidate, dict) else None
            parts = content.get("parts") if isinstance(content, dict) else None
            if not isinstance(parts, list):
                continue
            for part in parts:
                if not isinstance(part, dict):
                    continue
                text = part.get("text")
                if isinstance(text, str) and text:
                    text_parts.append(text)
                inline_data = part.get("inlineData") or part.get("inline_data")
                if isinstance(inline_data, dict):
                    images.append(_generated_image_from_inline_data(inline_data, output_mime_type))

        metadata = {
            "provider": "comet",
            "model": model,
            "image_count": len(images),
            "text_parts": text_parts,
        }
        if response_data.get("usageMetadata") is not None:
            metadata["usage"] = response_data["usageMetadata"]

        if not images:
            details = "\n".join(text_parts).strip()
            message = "Comet response did not contain an image"
            if details:
                message = f"{message}: {details[:500]}"
            raise CometApiError(message)

        return CometImageResult(images=images, text_parts=text_parts, metadata=metadata)


def _generated_image_from_inline_data(
    inline_data: dict[str, Any],
    fallback_mime_type: str | None,
) -> CometGeneratedImage:
    mime_type = (
        str(inline_data.get("mimeType") or inline_data.get("mime_type") or "")
        or fallback_mime_type
        or "image/png"
    )
    encoded = inline_data.get("data")
    if not isinstance(encoded, str) or not encoded:
        raise CometApiError("Comet returned empty inline image data")
    try:
        content = base64.b64decode(encoded)
    except Exception as exc:
        raise CometApiError("Comet returned invalid base64 image data") from exc
    return CometGeneratedImage(content=content, mime_type=mime_type)


def _normalize_kling_state(raw_state: str) -> str:
    normalized = raw_state.strip().lower()
    return {
        "submitted": "submitted",
        "processing": "generating",
        "succeed": "success",
        "success": "success",
        "failed": "fail",
        "fail": "fail",
    }.get(normalized, normalized or "submitted")


def _normalize_comet_video_state(raw_state: str) -> str:
    normalized = raw_state.strip().lower()
    return {
        "created": "submitted",
        "pending": "submitted",
        "submitted": "submitted",
        "queued": "waiting",
        "queue": "waiting",
        "waiting": "waiting",
        "running": "generating",
        "processing": "generating",
        "generating": "generating",
        "succeed": "success",
        "succeeded": "success",
        "success": "success",
        "completed": "success",
        "complete": "success",
        "done": "success",
        "failed": "fail",
        "fail": "fail",
        "error": "fail",
        "canceled": "fail",
        "cancelled": "fail",
    }.get(normalized, normalized or "submitted")


def _extract_kling_urls(data: dict[str, Any]) -> list[str]:
    task_result = data.get("task_result")
    if isinstance(task_result, dict):
        videos = task_result.get("videos")
        if isinstance(videos, list):
            urls = []
            for item in videos:
                if isinstance(item, dict) and item.get("url"):
                    urls.append(str(item["url"]))
                elif isinstance(item, str):
                    urls.append(item)
            if urls:
                return urls
        for key in ("url", "video_url", "result_url"):
            value = task_result.get(key)
            if isinstance(value, str) and value:
                return [value]
    return []


def _extract_seedance_urls(data: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for key in ("video_url", "download_url", "url", "result_url", "output_url"):
        value = data.get(key)
        if isinstance(value, str) and value:
            urls.append(value)

    for container_key in ("result", "output", "task_result"):
        container = data.get(container_key)
        if isinstance(container, dict):
            for key in ("video_url", "download_url", "url", "result_url", "output_url"):
                value = container.get(key)
                if isinstance(value, str) and value:
                    urls.append(value)
            for key in ("videos", "urls", "resultUrls"):
                value = container.get(key)
                if isinstance(value, list):
                    urls.extend(str(item) for item in value if item)
                elif isinstance(value, str) and value:
                    urls.append(value)

    return list(dict.fromkeys(urls))


def _seedance_size(aspect_ratio: str | None, resolution: str | None) -> str:
    size = str(aspect_ratio or "").strip()
    if _looks_like_exact_size(size):
        return size
    resolution_key = str(resolution or "720p").strip().lower()
    return SEEDANCE_2_SIZES.get(resolution_key, {}).get(size) or size or DEFAULT_SEEDANCE_SIZE


def _looks_like_exact_size(value: str) -> bool:
    width, separator, height = value.lower().partition("x")
    return bool(separator and width.isdecimal() and height.isdecimal())


def _progress_complete(value: Any) -> bool:
    if isinstance(value, int | float):
        return value >= 100
    if isinstance(value, str):
        try:
            return float(value.strip().removesuffix("%")) >= 100
        except ValueError:
            return False
    return False
