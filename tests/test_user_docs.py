"""Раздел «Документация» в интерфейсе (E9-07, ADR-056): config/user_docs.py.

Настоящий сайт собирается при сборке образа; здесь — маленький сайт в
временной папке с теми же картами, что пишет проверщик. Как выглядит и ищет
настоящий сайт — браузерные проверки tests/e2e/test_user_docs.py.
"""

import json
from pathlib import Path

import pytest
from django.test import Client
from django.urls import get_resolver, reverse
from pytest_django import Settings

from config.user_docs import SCREENS
from tools.check_user_docs import DOCS_URL, known_screens

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parent.parent
PARTIAL = {"X-Seo-Partial": "1"}

FIND_SITES = {"url": "/docs/how-to/find-sites/", "title": "Как найти площадку"}
EXPORT = {"url": "/docs/how-to/export-to-excel/", "title": "Как выгрузить список в Excel"}
INDEXATION = {"url": "/docs/how-to/check-indexation/", "title": "Как проверить индексацию"}


@pytest.fixture
def docs_site(tmp_path: Path, settings: Settings) -> Path:
    """Собранный сайт в миниатюре: страницы-папки, файл скрипта и обе карты."""
    files = {
        "index.html": "<h1>Документация</h1>",
        "how-to/find-sites/index.html": "<h1>Как найти площадку</h1>",
        "assets/app.js": "console.log(1);",
        "screens.json": json.dumps(
            {"site_list": [FIND_SITES, EXPORT], "placement_list": [INDEXATION]}
        ),
        "whatsnew.json": json.dumps(
            {
                "version": "Не выпущено",
                "released": False,
                "html": "<h3>Добавлено</h3>\n<ul>\n<li>Раздел <strong>Документация</strong>"
                ' (E9-07, <a href="/docs/">главная</a>).</li>\n</ul>',
            }
        ),
    }
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    settings.USER_DOCS_ROOT = tmp_path
    return tmp_path


# ---------- Отдача сайта ----------


def test_root_address_matches_checker() -> None:
    # Адреса страниц в картах считает проверщик — от того же адреса раздела.
    assert reverse("user_docs") == DOCS_URL


def test_docs_need_login(client: Client, docs_site: Path) -> None:
    response = client.get("/docs/how-to/find-sites/")
    assert response.status_code == 302
    assert response["Location"] == "/admin/login/?next=/docs/how-to/find-sites/"


def test_docs_pages_for_staff(admin_client: Client, docs_site: Path) -> None:
    response = admin_client.get("/docs/")
    assert response.status_code == 200
    assert b"".join(response.streaming_content) == "<h1>Документация</h1>".encode()  # type: ignore[attr-defined]
    page = admin_client.get("/docs/how-to/find-sites/")
    assert page.status_code == 200
    assert page["Content-Type"].startswith("text/html")
    # После выкатки браузер сверяется с сервером, а не держит прежнюю страницу.
    assert "no-cache" in page["Cache-Control"] and "private" in page["Cache-Control"]
    script = admin_client.get("/docs/assets/app.js")
    assert script["Content-Type"].startswith("text/javascript")


def test_folder_without_slash_redirects(admin_client: Client, docs_site: Path) -> None:
    response = admin_client.get("/docs/how-to/find-sites", {"q": "цена"})
    assert response.status_code == 302
    assert response["Location"] == "/docs/how-to/find-sites/?q=%D1%86%D0%B5%D0%BD%D0%B0"


@pytest.mark.parametrize(
    "url", ["/docs/no-such-page/", "/docs/how-to/../../etc/passwd", "/docs/%2e%2e/secret"]
)
def test_missing_or_outside_is_not_found(admin_client: Client, docs_site: Path, url: str) -> None:
    assert admin_client.get(url).status_code in (400, 404)


def test_docs_not_built(admin_client: Client, settings: Settings, tmp_path: Path) -> None:
    settings.USER_DOCS_ROOT = tmp_path / "nothing"
    assert admin_client.get("/docs/").status_code == 404
    # Остальные экраны без документации работают, просто без «?» и «Что нового».
    home = admin_client.get(reverse("admin:index"))
    assert home.status_code == 200
    assert "Что нового" not in home.content.decode()


# ---------- Карта экранов ----------


def test_screen_names_are_admin_addresses() -> None:
    names = get_resolver().namespace_dict["admin"][1].reverse_dict
    missing = [name for name in SCREENS if name.removeprefix("admin:") not in names]
    assert missing == []


def test_screens_are_listed_in_user_docs_rules() -> None:
    listed = known_screens(ROOT / "docs" / "15-USER-DOCS.md")
    assert set(SCREENS.values()) <= listed


def test_docs_link_in_header(admin_client: Client, docs_site: Path) -> None:
    html = admin_client.get(reverse("admin:placements_placement_changelist")).content.decode()
    assert (
        '<a href="/docs/" target="_blank" rel="noopener" '
        'title="Документация — в новой вкладке">Документация</a>' in html
    )


def test_help_menu_on_site_list(admin_client: Client, docs_site: Path) -> None:
    html = admin_client.get(reverse("admin:sites_productsitelatest_changelist")).content.decode()
    menu = html.split("data-seo-help", 1)[1].split("</h1>", 1)[0]
    # Сначала — страница только про «Площадки».
    assert menu.index(FIND_SITES["url"]) < menu.index(EXPORT["url"])
    assert 'target="_blank"' in menu and FIND_SITES["title"] in menu


def test_single_page_is_plain_link(admin_client: Client, docs_site: Path) -> None:
    html = admin_client.get(reverse("admin:placements_placement_changelist")).content.decode()
    assert f'<a class="seo-help" href="{INDEXATION["url"]}" target="_blank"' in html
    assert "data-seo-help" not in html


def test_no_button_without_pages(admin_client: Client, docs_site: Path) -> None:
    html = admin_client.get(reverse("admin:placements_invoice_changelist")).content.decode()
    assert 'class="seo-help' not in html


def test_help_in_panel_head(admin_client: Client, docs_site: Path, settings: Settings) -> None:
    screens = docs_site / "screens.json"
    screens.write_text(json.dumps({"placement_card": [INDEXATION]}), encoding="utf-8")
    response = admin_client.get(reverse("admin:placements_placement_add"), headers=PARTIAL)
    head = response.content.decode().split('class="seo-panel-head"', 1)[1].split("</header>")[0]
    assert f'href="{INDEXATION["url"]}"' in head


# ---------- «Что нового» ----------


def test_whats_new_on_home(admin_client: Client, docs_site: Path) -> None:
    html = admin_client.get(reverse("admin:index")).content.decode()
    news = html.split('class="home-group home-news"', 1)[1].split("</section>", 1)[0]
    assert "Что нового" in news
    assert "ещё не на рабочем сервере" in news
    assert "<strong>Документация</strong>" in news
    assert 'href="/docs/changelog/" target="_blank"' in news


def test_released_version_label(admin_client: Client, docs_site: Path) -> None:
    (docs_site / "whatsnew.json").write_text(
        json.dumps({"version": "2026.10.05", "released": True, "html": "<ul><li>x</li></ul>"}),
        encoding="utf-8",
    )
    html = admin_client.get(reverse("admin:index")).content.decode()
    assert "версия 2026.10.05" in html
    assert "ещё не на рабочем сервере" not in html
