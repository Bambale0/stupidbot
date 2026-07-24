from __future__ import annotations

from app.config import Settings


def run_model_env_migration_regression() -> None:
    settings = Settings(
        _env_file=None,
        comet_image_simple_model="gemini-3.1-flash-image-preview",
        comet_image_pro_model="gemini-3-pro-image-preview",
        comet_image_2_model="gemini-3.1-flash-image-preview",
        kie_image_simple_model="nano-banana-2",
    )
    assert settings.comet_image_simple_model == "gemini-3.1-flash-lite-image"
    assert settings.comet_image_pro_model == "gemini-3-pro-image"
    assert settings.comet_image_2_model == "gemini-3.1-flash-image"
    assert settings.kie_image_simple_model == "nano-banana-2-lite"

    custom = Settings(
        _env_file=None,
        comet_image_simple_model="custom-lite-model",
        comet_image_pro_model="custom-pro-model",
        comet_image_2_model="custom-flash-model",
        kie_image_simple_model="custom-kie-lite",
    )
    assert custom.comet_image_simple_model == "custom-lite-model"
    assert custom.comet_image_pro_model == "custom-pro-model"
    assert custom.comet_image_2_model == "custom-flash-model"
    assert custom.kie_image_simple_model == "custom-kie-lite"

    print("deprecated model environment migration regression passed")


if __name__ == "__main__":
    run_model_env_migration_regression()
