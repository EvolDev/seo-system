"""Наша статика — с версией в адресе: после правки браузер берёт свежий файл (E1-10)."""

import os
import re

import pytest
from django.contrib.staticfiles import finders
from django.test import Client
from django.urls import reverse

from config.assets import Css, Js, versioned

pytestmark = pytest.mark.django_db


def test_version_is_file_mtime() -> None:
    found = finders.find("seo/offers.css")
    assert isinstance(found, str)
    assert versioned("seo/offers.css") == f"/static/seo/offers.css?v={int(os.path.getmtime(found))}"
    assert versioned("seo/no-such-file.css") == "/static/seo/no-such-file.css?v=0"


def test_media_objects_render_versioned_tags() -> None:
    assert re.fullmatch(
        r'<link href="/static/seo/offers\.css\?v=\d+" media="all" rel="stylesheet">',
        str(Css("seo/offers.css").__html__()),
    )
    assert re.fullmatch(
        r'<script src="/static/seo/indexation\.js\?v=\d+"></script>',
        str(Js("seo/indexation.js").__html__()),
    )
    assert Css("seo/offers.css") == Css("seo/offers.css")


@pytest.mark.parametrize(
    ("url", "assets"),
    [
        (
            reverse("admin:sites_productsitelatest_changelist"),
            [
                "seo/admin-palettes.css",
                "seo/offers.css",
                "flags/sprite-hq.css",
                "seo/country-picker.js",
            ],
        ),
        (
            reverse("admin:sites_upload_add"),
            ["seo/offers.css", "seo/uploads.css", "seo/country-picker.js", "seo/uploads.js"],
        ),
        (reverse("admin:sites_upload_changelist"), ["seo/offers.css", "seo/uploads.css"]),
        (reverse("admin:placements_placement_changelist"), ["seo/indexation.js"]),
    ],
)
def test_pages_link_versioned_assets(admin_client: Client, url: str, assets: list[str]) -> None:
    page = admin_client.get(url, {"list": "all"} if "productsitelatest" in url else {})
    html = page.content.decode()
    for name in assets:
        assert re.search(rf"/static/{re.escape(name)}\?v=\d+", html), name
        assert f'/static/{name}"' not in html, name
