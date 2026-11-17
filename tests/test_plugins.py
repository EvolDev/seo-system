"""«Работа → Плагины» (ADR-084): расширение серости скачивается с сайта.

Архив собирается из `browser-extension/` при скачивании: одна папка внутри, в
manifest — адрес, с которого скачали. Страница — для всех, кто видит площадки.
"""

import io
import json
import zipfile
from typing import Any

import pytest
from django.contrib import admin
from django.contrib.auth.models import Permission, User
from django.test import Client
from django.urls import reverse

from apps.sites import plugins

pytestmark = pytest.mark.django_db

PAGE = reverse("admin:plugins")
DOWNLOAD = reverse("admin:plugin_download", args=["gray"])
FILES = {"background.js", "count.js", "google.js", "manifest.json", "relay.js"}


def _manifest(content: bytes) -> dict[str, Any]:
    with zipfile.ZipFile(io.BytesIO(content)) as bundle:
        result: dict[str, Any] = json.loads(bundle.read("seo-gray-extension/manifest.json"))
        return result


def _relay_matches(manifest: dict[str, Any]) -> list[str]:
    script = next(s for s in manifest["content_scripts"] if "relay.js" in s["js"])
    return list(script["matches"])


class TestArchive:
    def test_one_folder_with_extension(self) -> None:
        content = plugins.archive(plugins.GRAY, "https://seo.example/*")
        with zipfile.ZipFile(io.BytesIO(content)) as bundle:
            names = bundle.namelist()
            assert bundle.testzip() is None
        assert {name.split("/", 1)[0] for name in names} == {"seo-gray-extension"}
        assert {name.split("/", 1)[1] for name in names} == FILES
        manifest = _manifest(content)
        assert manifest["version"] == plugins.GRAY.version
        assert manifest["manifest_version"] == 3
        assert "https://seo.example/*" in _relay_matches(manifest)
        # Google — как в папке: адрес системы дописывается только скрипту её страниц.
        google = next(s for s in manifest["content_scripts"] if "google.js" in s["js"])
        assert google["matches"] == ["https://www.google.com/search*"]

    def test_same_code_same_bytes(self) -> None:
        # Дата файлов в архиве фиксирована: повторное скачивание — тот же архив.
        first = plugins.archive(plugins.GRAY, "https://seo.example/*")
        assert plugins.archive(plugins.GRAY, "https://seo.example/*") == first

    def test_known_address_not_repeated(self) -> None:
        manifest = plugins.GRAY.manifest()
        known = _relay_matches(manifest)[0]
        assert _relay_matches(plugins.with_site(manifest, known)) == _relay_matches(manifest)
        # Исходный manifest не тронут.
        assert "https://new.example/*" not in _relay_matches(manifest)
        assert "https://new.example/*" in _relay_matches(
            plugins.with_site(manifest, "https://new.example/*")
        )

    @pytest.mark.parametrize(
        ("scheme", "host", "pattern"),
        [
            ("https", "2.29.192.198", "https://2.29.192.198/*"),
            ("http", "localhost:8000", "http://localhost/*"),
            ("https", "seo.example:443", "https://seo.example/*"),
            ("http", "[::1]:8000", "http://[::1]/*"),
        ],
    )
    def test_site_pattern(self, scheme: str, host: str, pattern: str) -> None:
        assert plugins.site_pattern(scheme, host) == pattern

    def test_relay_reports_version_to_page(self) -> None:
        # Версию в атрибуте <html> читает страница «Плагины» (seo/plugins.js).
        relay = (plugins.GRAY.folder / "relay.js").read_text("utf-8")
        assert "dataset.seoExtension = chrome.runtime.getManifest().version" in relay
        assert plugins.GRAY.version == "1.2.0"


class TestScreen:
    def test_page_with_download_and_instructions(self, admin_client: Client) -> None:
        response = admin_client.get(PAGE)
        assert response.status_code == 200
        html = response.content.decode()
        assert "Серость из Google" in html
        assert f'href="{DOWNLOAD}"' in html
        assert f"Скачать .zip · {plugins.GRAY.version}" in html
        assert "opera://extensions" in html and "Загрузить распакованное расширение" in html
        assert "Как обновить" in html

    @pytest.fixture(autouse=True)
    def host(self, settings: Any) -> None:
        # Хост из заголовка Django проверяет по ALLOWED_HOSTS — чужой адрес в архив не попадёт.
        settings.ALLOWED_HOSTS = [*settings.ALLOWED_HOSTS, "seo.example"]

    def test_download(self, admin_client: Client) -> None:
        response = admin_client.get(DOWNLOAD, HTTP_HOST="seo.example")
        assert response.status_code == 200
        assert response["Content-Type"] == "application/zip"
        assert response["Content-Disposition"] == (
            f'attachment; filename="seo-gray-extension-{plugins.GRAY.version}.zip"'
        )
        assert "no-store" in response["Cache-Control"]
        assert "http://seo.example/*" in _relay_matches(_manifest(response.content))

    def test_download_over_https(self, admin_client: Client) -> None:
        response = admin_client.get(DOWNLOAD, HTTP_HOST="seo.example", secure=True)
        assert "https://seo.example/*" in _relay_matches(_manifest(response.content))

    def test_foreign_host_refused(self, admin_client: Client) -> None:
        assert admin_client.get(DOWNLOAD, HTTP_HOST="evil.example").status_code == 400

    def test_unknown_plugin(self, admin_client: Client) -> None:
        assert admin_client.get(reverse("admin:plugin_download", args=["nope"])).status_code == 404

    def test_needs_login(self, client: Client) -> None:
        for url in (PAGE, DOWNLOAD):
            response = client.get(url)
            assert response.status_code == 302
            assert reverse("admin:login") in response["Location"]

    def test_needs_sites_view(self, client: Client) -> None:
        user = User.objects.create_user("nobody", password="x", is_staff=True)
        client.force_login(user)
        assert client.get(PAGE).status_code == 403
        assert client.get(DOWNLOAD).status_code == 403
        assert f'href="{PAGE}"' not in client.get(reverse("admin:index")).content.decode()
        user.user_permissions.add(Permission.objects.get(codename="view_site"))
        user = User.objects.get(pk=user.pk)
        client.force_login(user)
        assert client.get(PAGE).status_code == 200


class TestMenu:
    def test_last_in_work_not_on_home(self, admin_client: Client) -> None:
        request = admin_client.get(reverse("admin:index")).wsgi_request
        work = next(g for g in admin.site.get_app_list(request) if g["app_label"] == "work")
        last = work["models"][-1]
        assert (last["name"], last["admin_url"]) == ("Плагины", PAGE)
        # Карточки главной — по пунктам «Работы», но расширение ставят один раз.
        response = admin_client.get(reverse("admin:index"))
        names = [card["name"] for card in response.context["home_cards"]]
        assert "Плагины" not in names and "Экономика" in names
