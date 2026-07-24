# Контракты моделей и провайдеров

Проверено: 24 июля 2026 года.

Этот документ фиксирует только параметры, которые реально принимает текущий provider endpoint. UI, FSM, Mini App payload и server-side validation должны брать значения из `app/services/generation_catalog.py`, а не из отдельных статических списков.

## Nano Banana 2 Lite

- Модель: `gemini-3.1-flash-lite-image`.
- Основной провайдер: CometAPI-compatible Gemini Generate Content.
- Резолюция: только `1K`.
- Aspect ratio: `1:1`, `3:2`, `2:3`, `3:4`, `4:3`, `4:5`, `5:4`, `9:16`, `16:9`, `21:9`.
- Референсы: от 0 до 14 изображений.
- KIE fallback: `nano-banana-2-lite`; его payload использует `image_urls` и не получает поля `resolution`, `output_format` или `image_input`.

Источники:

- https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-lite-image
- https://ai.google.dev/gemini-api/docs/image-generation

## Nano Banana 2

- Модель: `gemini-3.1-flash-image`.
- Резолюции: `512`, `1K`, `2K`, `4K`.
- Aspect ratio: `1:1`, `1:4`, `1:8`, `2:3`, `3:2`, `3:4`, `4:1`, `4:3`, `4:5`, `5:4`, `8:1`, `9:16`, `16:9`, `21:9`.
- Референсы: от 0 до 14 изображений. Внутри лимита модель документирует до 10 объектных и до 4 character-reference изображений.
- KIE fallback: `nano-banana-2` с полями `image_input`, `aspect_ratio`, `resolution`, `output_format`.

Источники:

- https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-image
- https://ai.google.dev/gemini-api/docs/image-generation
- https://docs.kie.ai/market/google/nanobanana2

## Nano Banana Pro

- Модель: `gemini-3-pro-image`.
- Резолюции: `1K`, `2K`, `4K`.
- Aspect ratio: `1:1`, `2:3`, `3:2`, `3:4`, `4:3`, `4:5`, `5:4`, `9:16`, `16:9`, `21:9`.
- Референсы: от 0 до 14 изображений. Документированы до 6 объектных, до 5 character-reference и до 3 style-reference изображений в рамках общего лимита.

Источник:

- https://ai.google.dev/gemini-api/docs/image-generation

## Kling 2.6 Motion Control

- Модель KIE: `kling-2.6/motion-control`.
- Референсы: строго одно изображение и одно видео.
- Изображение: JPEG/PNG, до 10 MB.
- Видео: MP4, QuickTime/MOV или Matroska/MKV, до 100 MB, от 3 до 30 секунд.
- Mode: только `720p`.
- `character_orientation`: `image` или `video`; default проекта — `image`, как в официальном request example.

Источник:

- https://docs.kie.ai/market/kling/motion-control

## Kling 3.0 Motion Control

- Модель KIE: `kling-3.0/motion-control`.
- Референсы: строго одно изображение и одно видео.
- Изображение: JPEG/PNG, до 10 MB.
- Видео: MP4 или QuickTime/MOV, до 100 MB, от 3 до 30 секунд. MKV не принимается.
- Для изображения и видео каждая сторона должна быть больше 340 px; допустимое соотношение сторон — от `2:5` до `5:2`.
- Mode: только `720p`.
- `background_source`: `input_video`.
- `character_orientation`: `image` или `video`; default проекта — `image`.

Источник:

- https://docs.kie.ai/market/kling/motion-control-v3

## Seedance 2.0

- Основная модель CometAPI: `doubao-seedance-2-0` через `/v1/videos`.
- Режимы: text-to-video без изображения или image-to-video с одним `input_reference`.
- Длительность: любое целое значение от 4 до 15 секунд.
- Aspect ratio: `21:9`, `16:9`, `4:3`, `1:1`, `3:4`, `9:16`.
- Резолюции: `480p`, `720p`, `1080p` для стандартной модели; default `720p`.
- Стартовое изображение необязательно; принимаются JPEG, PNG и WebP.
- KIE fallback: `bytedance/seedance-2`; без изображения используется text-to-video, с изображением — `first_frame_url`.

Источники:

- https://www.cometapi.com/models/doubao/doubao-seedance-2-0/
- https://www.cometapi.com/changelog/
- https://docs.kie.ai/market/bytedance/seedance-2

## Правила синхронизации

- Существующие строки `generation_models` обновляются из каталога при `ensure_defaults`.
- Provider model IDs, лимиты, MIME-типы и опции не должны задаваться независимо в Telegram и Mini App.
- Любое добавление модели требует обновления этого документа и `scripts/regression_model_provider_contracts.py`.
- Неподдерживаемые параметры нормализуются к model-specific default до создания provider task.
- Файловые ограничения Kling 3.0 проверяются до списания кредитов и вызова KIE API.
