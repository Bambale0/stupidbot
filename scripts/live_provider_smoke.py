from __future__ import annotations

import argparse
import asyncio
import base64
from io import BytesIO
from urllib.parse import urlparse

if __package__ in {None, ""}:
    from _bootstrap import add_project_root_to_path

    add_project_root_to_path()

from PIL import Image, ImageDraw

from app.config import get_settings
from app.services.comet import CometApiError, CometClient, CometImageReference
from app.services.kie import KieClient, KieUploadReference


def _reference_png() -> bytes:
    image = Image.new("RGB", (256, 256), "#1b1b1f")
    draw = ImageDraw.Draw(image)
    draw.rectangle((28, 28, 228, 228), outline="#dfff1d", width=8)
    draw.ellipse((76, 58, 180, 194), fill="#f05fb8", outline="#dfff1d", width=5)
    draw.text((82, 112), "BANANA", fill="#ffffff")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _image_size(content: bytes) -> tuple[int, int]:
    with Image.open(BytesIO(content)) as image:
        return image.size


async def _smoke_comet_image(settings, reference: bytes) -> None:
    client = CometClient(api_key=settings.comet_api_key, base_url=settings.comet_base_url)
    result = await client.generate_image(
        model=settings.comet_image_2_model,
        prompt=(
            "Create a clean square BANANA mini app poster based on the reference image. "
            "Keep neon lime and pink accents, no readable text except BANANA."
        ),
        reference_images=[CometImageReference(content=reference, mime_type="image/png")],
        aspect_ratio="1:1",
        image_size="2K",
        output_mime_type="image/png",
    )
    first = result.images[0]
    print("COMET_IMAGE_MODEL=", result.metadata.get("model"))
    print("COMET_IMAGE_COUNT=", len(result.images))
    print("COMET_IMAGE_MIME=", first.mime_type)
    print("COMET_IMAGE_SIZE=", f"{_image_size(first.content)[0]}x{_image_size(first.content)[1]}")


async def _smoke_kie_upload(settings, reference: bytes) -> None:
    client = KieClient(
        api_key=settings.kie_api_key,
        base_url=settings.kie_base_url,
        upload_base_url=settings.kie_upload_base_url,
    )
    url = await client.upload_base64_image(
        KieUploadReference(content=reference, mime_type="image/png", filename="banana-live-smoke.png")
    )
    parsed = urlparse(url)
    print("KIE_UPLOAD_HOST=", parsed.netloc)
    print("KIE_UPLOAD_PATH_OK=", bool(parsed.path))


async def _smoke_comet_video(settings, reference: bytes, *, poll: bool) -> None:
    client = CometClient(api_key=settings.comet_api_key, base_url=settings.comet_base_url, timeout=90.0)
    models = [
        ("kling-2.6/video", settings.comet_kling_2_6_model),
        ("kling-3.0/video", settings.comet_kling_3_0_model),
    ]
    for code, provider_model in models:
        try:
            task_id = await client.create_kling_image_to_video_task(
                model_name=provider_model,
                image=base64.b64encode(reference).decode("ascii"),
                prompt="Subtle camera push-in on the BANANA neon poster, smooth motion.",
                mode="std",
                duration="5",
            )
            print("COMET_VIDEO_TASK=", code, provider_model, task_id)
            if poll:
                data = await client.query_kling_image_to_video_task(task_id)
                print("COMET_VIDEO_STATE=", code, data.get("state"))
        except CometApiError as exc:
            print("COMET_VIDEO_ERROR=", code, provider_model, str(exc)[:500])
    try:
        task_id = await client.create_seedance_video_task(
            model=settings.comet_seedance_2_model,
            prompt="Subtle camera push-in on the BANANA neon poster, smooth motion.",
            image=reference,
            image_mime_type="image/png",
            image_filename="banana-seedance-smoke.png",
            duration="5",
            aspect_ratio="16:9",
            resolution="720p",
        )
        print("COMET_VIDEO_TASK=", "seedance-2/video", settings.comet_seedance_2_model, task_id)
        if poll:
            data = await client.query_seedance_video_task(task_id)
            print("COMET_VIDEO_STATE=", "seedance-2/video", data.get("state"))
    except CometApiError as exc:
        print("COMET_VIDEO_ERROR=", "seedance-2/video", settings.comet_seedance_2_model, str(exc)[:500])


async def amain() -> None:
    parser = argparse.ArgumentParser(description="Run live provider smoke calls.")
    parser.add_argument("--skip-image", action="store_true")
    parser.add_argument("--skip-kie-upload", action="store_true")
    parser.add_argument("--skip-video", action="store_true")
    parser.add_argument("--poll-video", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    reference = _reference_png()
    print("REFERENCE_IMAGE_SIZE=", f"{_image_size(reference)[0]}x{_image_size(reference)[1]}")
    if not args.skip_image:
        await _smoke_comet_image(settings, reference)
    if not args.skip_kie_upload:
        await _smoke_kie_upload(settings, reference)
    if not args.skip_video:
        await _smoke_comet_video(settings, reference, poll=args.poll_video)


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
