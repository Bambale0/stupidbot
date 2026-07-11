from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import httpx


class KieApiError(RuntimeError):
    pass


@dataclass(slots=True)
class KieUploadReference:
    content: bytes
    mime_type: str
    filename: str | None = None


@dataclass(slots=True)
class KieClient:
    api_key: str | None
    base_url: str = "https://api.kie.ai"
    upload_base_url: str = "https://kieai.redpandaai.co"
    timeout: float = 60.0

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise KieApiError("KIE_API_KEY is not configured")
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def upload_base64_image(self, image: KieUploadReference) -> str:
        return await self.upload_base64_file(image)

    async def upload_base64_file(self, file: KieUploadReference) -> str:
        extension = _extension_for_mime_type(file.mime_type)
        filename = file.filename or f"banana-{uuid4().hex}.{extension}"
        encoded = base64.b64encode(file.content).decode("ascii")
        payload = {
            "base64Data": f"data:{file.mime_type};base64,{encoded}",
            "uploadPath": "banana",
            "fileName": filename,
        }
        async with httpx.AsyncClient(base_url=self.upload_base_url, timeout=self.timeout) as client:
            response = await client.post("/api/file-base64-upload", headers=self._headers(), json=payload)
        data = self._decode_response(response, provider="KIE Upload")
        file_data = data.get("data") if isinstance(data.get("data"), dict) else {}
        file_url = file_data.get("fileUrl") or file_data.get("downloadUrl")
        if not file_url:
            raise KieApiError(f"KIE upload response does not contain fileUrl: {data}")
        return str(file_url)

    async def create_image_task(
        self,
        *,
        model: str,
        prompt: str,
        image_urls: list[str] | None = None,
        aspect_ratio: str | None = None,
        resolution: str | None = None,
        output_format: str | None = None,
        callback_url: str | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": model,
            "input": {
                "prompt": prompt,
                "image_input": image_urls or [],
                "aspect_ratio": aspect_ratio or "auto",
                "resolution": resolution or "2K",
                "output_format": output_format or "png",
            },
        }
        if callback_url:
            payload["callBackUrl"] = callback_url

        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
            response = await client.post("/api/v1/jobs/createTask", headers=self._headers(), json=payload)
        data = self._decode_response(response, provider="KIE")
        task_data = data.get("data") if isinstance(data.get("data"), dict) else {}
        task_id = task_data.get("taskId") or task_data.get("task_id")
        if not task_id:
            raise KieApiError(f"KIE createTask response does not contain taskId: {data}")
        return str(task_id)

    async def create_kling_image_to_video_task(
        self,
        *,
        model: str,
        prompt: str,
        image_urls: list[str],
        mode: str | None = None,
        duration: str | None = None,
        aspect_ratio: str | None = None,
        sound: bool = False,
        callback_url: str | None = None,
    ) -> str:
        payload_input: dict[str, Any] = {
            "prompt": prompt,
            "image_urls": image_urls,
            "sound": sound,
            "duration": duration or "5",
            "mode": mode or "pro",
        }
        if aspect_ratio:
            payload_input["aspect_ratio"] = aspect_ratio
        payload: dict[str, Any] = {
            "model": model,
            "input": payload_input,
        }
        if callback_url:
            payload["callBackUrl"] = callback_url

        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
            response = await client.post("/api/v1/jobs/createTask", headers=self._headers(), json=payload)
        data = self._decode_response(response, provider="KIE")
        task_data = data.get("data") if isinstance(data.get("data"), dict) else {}
        task_id = task_data.get("taskId") or task_data.get("task_id")
        if not task_id:
            raise KieApiError(f"KIE Kling createTask response does not contain taskId: {data}")
        return str(task_id)

    async def create_kling_motion_control_task(
        self,
        *,
        model: str,
        prompt: str,
        input_urls: list[str],
        video_urls: list[str],
        mode: str = "720p",
        character_orientation: str = "video",
        background_source: str | None = None,
        callback_url: str | None = None,
    ) -> str:
        payload_input: dict[str, Any] = {
            "prompt": prompt,
            "input_urls": input_urls,
            "video_urls": video_urls,
            "mode": mode,
            "character_orientation": character_orientation,
        }
        if background_source:
            payload_input["background_source"] = background_source
        payload: dict[str, Any] = {
            "model": model,
            "input": payload_input,
        }
        if callback_url:
            payload["callBackUrl"] = callback_url

        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
            response = await client.post("/api/v1/jobs/createTask", headers=self._headers(), json=payload)
        data = self._decode_response(response, provider="KIE")
        task_data = data.get("data") if isinstance(data.get("data"), dict) else {}
        task_id = task_data.get("taskId") or task_data.get("task_id")
        if not task_id:
            raise KieApiError(f"KIE Kling Motion Control createTask response does not contain taskId: {data}")
        return str(task_id)

    async def create_seedance_video_task(
        self,
        *,
        model: str,
        prompt: str,
        first_frame_url: str | None = None,
        last_frame_url: str | None = None,
        reference_image_urls: list[str] | None = None,
        resolution: str | None = None,
        aspect_ratio: str | None = None,
        duration: str | int | None = None,
        generate_audio: bool = False,
        return_last_frame: bool = False,
        web_search: bool = False,
        callback_url: str | None = None,
    ) -> str:
        payload_input: dict[str, Any] = {
            "prompt": prompt,
            "return_last_frame": return_last_frame,
            "generate_audio": generate_audio,
            "resolution": resolution or "720p",
            "aspect_ratio": aspect_ratio or "16:9",
            "duration": int(duration or 5),
            "web_search": web_search,
        }
        if first_frame_url:
            payload_input["first_frame_url"] = first_frame_url
        if last_frame_url:
            payload_input["last_frame_url"] = last_frame_url
        if reference_image_urls:
            payload_input["reference_image_urls"] = reference_image_urls

        payload: dict[str, Any] = {
            "model": model,
            "input": payload_input,
        }
        if callback_url:
            payload["callBackUrl"] = callback_url

        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
            response = await client.post("/api/v1/jobs/createTask", headers=self._headers(), json=payload)
        data = self._decode_response(response, provider="KIE")
        task_data = data.get("data") if isinstance(data.get("data"), dict) else {}
        task_id = task_data.get("taskId") or task_data.get("task_id")
        if not task_id:
            raise KieApiError(f"KIE Seedance createTask response does not contain taskId: {data}")
        return str(task_id)

    async def query_task(self, task_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
            response = await client.get(
                "/api/v1/jobs/recordInfo",
                headers=self._headers(),
                params={"taskId": task_id},
            )
        data = self._decode_response(response, provider="KIE")
        task_data = data.get("data") if isinstance(data.get("data"), dict) else {}
        if not task_data:
            raise KieApiError(f"KIE recordInfo response does not contain data: {data}")
        normalized = dict(task_data)
        normalized["state"] = _normalize_kie_state(normalized.get("state"))
        return normalized

    @staticmethod
    def _decode_response(response: httpx.Response, *, provider: str) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise KieApiError(f"{provider} returned non-JSON response: {response.text[:500]}") from exc
        if response.is_error:
            raise KieApiError(f"{provider} HTTP {response.status_code}: {data}")
        code = data.get("code")
        success = data.get("success")
        message = str(data.get("msg") or "").strip().lower()
        if code not in (None, 200) and success is not True and message != "success":
            raise KieApiError(f"{provider} API error {code}: {data.get('msg') or data}")
        return data


def _extension_for_mime_type(mime_type: str) -> str:
    normalized = mime_type.lower()
    if normalized in {"image/jpeg", "image/jpg"}:
        return "jpg"
    if normalized == "image/webp":
        return "webp"
    if normalized in {"video/mp4", "video/mpeg"}:
        return "mp4"
    if normalized in {"video/quicktime", "video/mov"}:
        return "mov"
    if normalized in {"video/x-matroska", "video/matroska"}:
        return "mkv"
    return "png"


def _normalize_kie_state(value: Any) -> str:
    state = str(value or "").strip().lower()
    if state in {"waiting", "queued", "queue"}:
        return "waiting"
    if state == "queuing":
        return "queuing"
    if state in {"generating", "processing", "running"}:
        return "generating"
    if state in {"success", "succeeded", "completed", "complete"}:
        return "success"
    if state in {"fail", "failed", "error"}:
        return "fail"
    return state or "waiting"
