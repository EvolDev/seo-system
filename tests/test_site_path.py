"""Секретный путь системы на сервере (SITE_PATH, ADR-081): config/urls.py.

Локально адреса прежние — /admin/, /docs/, /static/. На сервере вся система под
одним путём: админка в его корне, справка и статика внутри, «жив ли сайт» —
и внутри сервера, и снаружи через путь. Что Caddy обрывает всё остальное —
проверка на тестовой копии прода, здесь — раскладка адресов Django.
"""

import importlib
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import NoReverseMatch, clear_url_caches, reverse, set_urlconf
from pytest_django import Settings

import config.urls
from config.user_docs import screen_pages, whats_new

pytestmark = pytest.mark.django_db

PATH = "tihaya-gavan"


def _reload_urls() -> None:
    importlib.reload(config.urls)
    clear_url_caches()
    set_urlconf(None)


@pytest.fixture
def on_server(settings: Settings) -> Iterator[None]:
    """Адреса как на сервере: настройки пути и перечитанный config/urls.py."""
    settings.SITE_PATH = PATH
    settings.URL_ROOT = f"{PATH}/"
    settings.STATIC_URL = f"{PATH}/static/"
    _reload_urls()
    yield
    # Настройки вернёт pytest-django; адреса — перечитать с ними заново.
    settings.SITE_PATH = ""
    settings.URL_ROOT = ""
    settings.STATIC_URL = "static/"
    _reload_urls()


def test_local_addresses_unchanged() -> None:
    assert reverse("admin:index") == "/admin/"
    assert reverse("admin:login") == "/admin/login/"
    assert reverse("user_docs") == "/docs/"
    assert reverse("healthz") == "/healthz"
    with pytest.raises(NoReverseMatch):
        reverse("healthz_outside")


@pytest.mark.usefixtures("on_server")
class TestOnServer:
    def test_everything_under_path(self) -> None:
        assert reverse("admin:index") == "/tihaya-gavan/"
        assert reverse("admin:login") == "/tihaya-gavan/login/"
        assert reverse("admin:otp_verify") == "/tihaya-gavan/otp/"
        assert reverse("admin:sites_productsitelatest_changelist") == (
            "/tihaya-gavan/sites/productsitelatest/"
        )
        assert reverse("user_docs") == "/tihaya-gavan/docs/"
        assert reverse("user_docs_page", args=["changelog/"]) == "/tihaya-gavan/docs/changelog/"
        assert reverse("healthz_outside") == "/tihaya-gavan/healthz"

    def test_old_addresses_gone(self, client: Client) -> None:
        for url in ("/admin/", "/admin/login/", "/docs/", "/"):
            assert client.get(url).status_code == 404, url

    def test_health_inside_and_outside(self, client: Client) -> None:
        # Внутри сервера — проверка контейнера, снаружи — пульт через Caddy.
        for url in ("/healthz", "/tihaya-gavan/healthz"):
            response = client.get(url)
            assert response.status_code == 200 and response.content == b"ok", url

    def test_login_and_screens(self, client: Client) -> None:
        response = client.get("/tihaya-gavan/")
        assert response.status_code == 302
        assert response["Location"] == "/tihaya-gavan/login/?next=/tihaya-gavan/"
        User.objects.create_superuser("boss", password="right-password-42")
        response = client.post(
            "/tihaya-gavan/login/",
            {"username": "boss", "password": "right-password-42", "next": "/tihaya-gavan/"},
        )
        assert response["Location"] == "/tihaya-gavan/"
        home = client.get("/tihaya-gavan/").content.decode()
        # Статика, меню и справка — под путём, без старых адресов.
        assert "/tihaya-gavan/static/seo/soft-nav.js" in home
        assert 'href="/tihaya-gavan/sites/productsitelatest/"' in home
        assert 'href="/tihaya-gavan/docs/"' in home
        assert 'href="/admin/' not in home and 'src="/static/' not in home
        users = client.get("/tihaya-gavan/auth/user/")
        assert users.status_code == 200

    def test_code_page_under_path(self, client: Client, settings: Settings) -> None:
        settings.LOGIN_OTP_REQUIRED = True
        User.objects.create_superuser("boss", password="right-password-42")
        client.post("/tihaya-gavan/login/", {"username": "boss", "password": "right-password-42"})
        response = client.get("/tihaya-gavan/sites/site/")
        assert response["Location"] == "/tihaya-gavan/otp/?next=/tihaya-gavan/sites/site/"

    def test_help_links_follow_path(self, tmp_path: Path, settings: Settings) -> None:
        # Карты справки собраны от корня /docs/ — выдаются под путём системы.
        (tmp_path / "screens.json").write_text(
            json.dumps({"site_list": [{"url": "/docs/how-to/find-sites/", "title": "Найти"}]}),
            encoding="utf-8",
        )
        (tmp_path / "whatsnew.json").write_text(
            json.dumps({"version": "Не выпущено", "html": '<a href="/docs/how-to/x/">x</a>'}),
            encoding="utf-8",
        )
        settings.USER_DOCS_ROOT = tmp_path
        assert screen_pages("site_list") == [
            {"url": "/tihaya-gavan/docs/how-to/find-sites/", "title": "Найти"}
        ]
        news = whats_new()
        assert news is not None
        assert str(news["html"]) == '<a href="/tihaya-gavan/docs/how-to/x/">x</a>'


@pytest.mark.usefixtures("on_server")
def test_docs_home_link_follows_path(
    admin_client: Client, tmp_path: Path, settings: Settings
) -> None:
    """«В систему» в шапке справки собран как /admin/ — выдаётся корнем пути."""
    (tmp_path / "index.html").write_text(
        '<a href="/admin/" title="SEO-система">x</a><a href="how-to/">y</a>', encoding="utf-8"
    )
    settings.USER_DOCS_ROOT = tmp_path
    response = admin_client.get("/tihaya-gavan/docs/")
    assert response.status_code == 200
    html = response.content.decode()
    assert 'href="/tihaya-gavan/"' in html and 'href="/admin/"' not in html
    assert 'href="how-to/"' in html
    assert response["Content-Type"].startswith("text/html")
    assert "no-cache" in response["Cache-Control"]
