from __future__ import annotations

if __package__ in {None, ""}:
    from _bootstrap import add_project_root_to_path

    add_project_root_to_path()

from app.models import GalleryItem
from app.plugins.gallery import plugin as gallery_plugin


def main() -> None:
    caption = gallery_plugin._gallery_caption(
        GalleryItem(title="<b>Title</b>", prompt="prompt & <unsafe>value</unsafe>")
    )
    assert "<b>Title</b>" not in caption
    assert "&lt;b&gt;Title&lt;/b&gt;" in caption
    assert "&lt;unsafe&gt;" in caption
    print("Gallery compatibility regression passed")


if __name__ == "__main__":
    main()
