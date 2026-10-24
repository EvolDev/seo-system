"""Серость площадки в Google: запросы, доля, зона, замер в карточке, колонка (E2-06);
наборы условий по языкам (ADR-074)."""

from datetime import timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from django.contrib.auth.models import Permission
from django.test import Client
from django.urls import reverse

from apps.content.domain_settings import GrayZones
from apps.content.models import DomainSetting
from apps.sites import gray_scan, languages
from apps.sites.gray_scan import Reading
from apps.sites.models import GrayScan, GrayTermSet, MetricSource, Product, Site, SiteMetric
from config.admin import PARTIAL_HEADER

pytestmark = pytest.mark.django_db

ZONES = GrayZones(green=10.0, yellow=25.0)
LIST_URL = reverse("admin:sites_productsitelatest_changelist")


@pytest.fixture
def site() -> Site:
    Product.objects.create(name="Convertio", domain="convertio.co")
    site = Site.objects.create(domain="youengage.me", language="en")
    SiteMetric.objects.create(site=site, dr=60)
    return site


def _english() -> list[str]:
    return GrayTermSet.objects.get(language="en").terms


def _card(client: Client, site: Site) -> str:
    url = reverse("admin:sites_site_card", args=[site.pk])
    response = client.get(url, headers={PARTIAL_HEADER: "1"})
    assert response.status_code == 200
    return response.content.decode()


def _save(client: Client, site: Site, data: dict[str, str], partial: bool = True) -> Any:
    url = reverse("admin:sites_site_card_gray", args=[site.pk])
    form = {
        "total_query": "site:youengage.me",
        "gray_query": 'site:youengage.me "casino"',
        "source": "typed",
        **data,
    }
    return client.post(url, form, headers={PARTIAL_HEADER: "1"} if partial else {})


class TestQueries:
    def test_total_query_is_bare_domain(self) -> None:
        # Без схемы и www.: так запрос берёт и www., и поддомены.
        assert gray_scan.total_query("youengage.me") == "site:youengage.me"

    def test_gray_query_quotes_every_term(self) -> None:
        query = gray_scan.gray_query("youengage.me", ["casino", "delta 8", '"online casino"'])
        assert query == 'site:youengage.me "casino" OR "delta 8" OR "online casino"'

    def test_default_terms_repeat_manual_query(self) -> None:
        query = gray_scan.gray_query("youengage.me", _english())
        assert query.startswith('site:youengage.me "casino" OR "poker" OR "betting"')
        assert query.endswith('OR "ed pills" OR "xanax"')

    def test_google_url(self) -> None:
        url = gray_scan.google_url('site:youengage.me "casino" OR "poker"')
        parts = urlsplit(url)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://www.google.com/search"
        assert parse_qs(parts.query) == {
            "q": ['site:youengage.me "casino" OR "poker"'],
            "hl": ["en"],
        }

    def test_word_count_skips_or(self) -> None:
        assert gray_scan.word_count('site:a.com "casino" OR "delta 8"') == 4
        # Запрос из ручной проверки: site: и 32 слова условий.
        assert gray_scan.word_count(gray_scan.gray_query("a.com", _english())) == 33


class TestRatio:
    @pytest.mark.parametrize(
        ("total", "gray", "ratio"),
        [
            (375, 12, Decimal("3.20")),
            (3, 1, Decimal("33.33")),
            (8, 1, Decimal("12.50")),
            (200, 1, Decimal("0.50")),
            (10, 20, Decimal("100.00")),
            (5, 0, Decimal("0.00")),
        ],
    )
    def test_ratio(self, total: int, gray: int, ratio: Decimal) -> None:
        assert gray_scan.gray_ratio(total, gray) == ratio

    def test_nothing_indexed_has_no_ratio(self) -> None:
        assert gray_scan.gray_ratio(0, 5) is None

    @pytest.mark.parametrize(
        ("ratio", "zone"),
        [
            ("9.99", "green"),
            ("10.00", "yellow"),
            ("25.00", "yellow"),
            ("25.01", "red"),
        ],
    )
    def test_zone_borders(self, ratio: str, zone: str) -> None:
        assert gray_scan.zone_of(Decimal(ratio), ZONES) == zone

    def test_no_zone_without_ratio_or_settings(self) -> None:
        assert gray_scan.zone_of(None, ZONES) is None
        assert gray_scan.zone_of(Decimal("50"), None) is None

    @pytest.mark.parametrize(
        ("ratio", "text"),
        [
            (Decimal("3.20"), "3,2 %"),
            (Decimal("0.35"), "0,35 %"),
            (Decimal("100.00"), "100 %"),
            (Decimal("0.00"), "0 %"),
            (None, "—"),
        ],
    )
    def test_percent_text(self, ratio: Decimal | None, text: str) -> None:
        assert gray_scan.percent_text(ratio) == text


class TestRecord:
    def test_scan_is_a_snapshot(self, site: Site) -> None:
        reading = Reading(375, 12, "site:youengage.me", 'site:youengage.me "casino"')
        first = gray_scan.record(site, reading)
        second = gray_scan.record(site, Reading(380, 40, "q1", "q2", source="extension"))
        assert GrayScan.objects.filter(site=site).count() == 2
        assert (first.total_indexed, first.gray_hits, first.ratio) == (375, 12, Decimal("3.20"))
        assert first.method == MetricSource.MANUAL
        assert first.breakdown == {
            "queries": {"total": "site:youengage.me", "gray": 'site:youengage.me "casino"'},
            "language": None,
            "source": "typed",
            "over_total": False,
        }
        assert first.sample_urls is None
        assert (second.breakdown or {})["source"] == "extension"

    def test_more_gray_than_total_is_marked(self, site: Site) -> None:
        scan = gray_scan.record(site, Reading(10, 30, "q1", "q2"))
        assert scan.ratio == Decimal("100.00")
        assert (scan.breakdown or {})["over_total"] is True

    def test_samples_are_limited(self, site: Site) -> None:
        urls = [f"https://youengage.me/p{n}" for n in range(15)]
        scan = gray_scan.record(site, Reading(100, 15, "q1", "q2", sample_urls=urls))
        assert scan.sample_urls == urls[:10]


class TestCard:
    def test_buttons_open_google(self, admin_client: Client, site: Site) -> None:
        page = _card(admin_client, site)
        total_url = gray_scan.google_url("site:youengage.me")
        assert f'href="{total_url}"' in page.replace("&amp;", "&")
        gray_url = gray_scan.google_url(gray_scan.gray_query("youengage.me", _english()))
        assert gray_url.replace("&", "&amp;") in page
        assert 'data-total-query="site:youengage.me"' in page
        assert 'data-green="10.0" data-yellow="25.0"' in page
        assert 'target="_blank"' in page

    def test_long_query_warning(self, admin_client: Client, site: Site) -> None:
        page = _card(admin_client, site)
        assert "Слов в запросе серости без OR: 33." in page
        assert "по языку площадки · условий: 29" in page

    def test_short_list_has_no_warning(self, admin_client: Client, site: Site) -> None:
        GrayTermSet.objects.filter(language="en").update(terms=["casino", "loan"])
        page = _card(admin_client, site)
        assert "Слов в запросе серости" not in page
        assert "&quot;casino&quot; OR &quot;loan&quot;" in page

    def test_without_terms_only_total_button(self, admin_client: Client, site: Site) -> None:
        GrayTermSet.objects.all().delete()
        page = _card(admin_client, site)
        assert "Google: всего страниц" in page
        assert "Google: серые темы" not in page
        assert "Нет ни одного набора условий серости" in page

    def test_save_from_card(self, admin_client: Client, site: Site) -> None:
        response = _save(admin_client, site, {"total": "375", "gray": "12"})
        assert response.status_code == 200
        scan = GrayScan.objects.get(site=site)
        assert (scan.total_indexed, scan.gray_hits, scan.ratio) == (375, 12, Decimal("3.20"))
        page = response.content.decode()
        assert 'class="seo-gray-latest"' in page
        assert "3,2 %" in page
        assert "зелёная зона" in page

    def test_page_without_panel_goes_back_to_card(self, admin_client: Client, site: Site) -> None:
        response = _save(admin_client, site, {"total": "100", "gray": "30"}, partial=False)
        assert response.status_code == 302
        assert response["Location"] == reverse("admin:sites_site_card", args=[site.pk])
        assert GrayScan.objects.get(site=site).ratio == Decimal("30.00")

    def test_extension_samples_are_kept(self, admin_client: Client, site: Site) -> None:
        samples = "https://youengage.me/a\njavascript:alert(1)\nhttps://youengage.me/a\n/b\n"
        data = {
            "total": "100",
            "gray": "2",
            "source": "extension",
            "sample_urls": samples + "https://youengage.me/c",
        }
        _save(admin_client, site, data)
        scan = GrayScan.objects.get(site=site)
        assert scan.sample_urls == ["https://youengage.me/a", "https://youengage.me/c"]
        assert (scan.breakdown or {})["source"] == "extension"
        page = _card(admin_client, site)
        assert "примеры (2)" in page
        assert "расширение" in page

    @pytest.mark.parametrize(
        ("data", "error"),
        [
            ({"gray": "12"}, "сколько всего страниц"),
            ({"total": "-1", "gray": "0"}, "0"),
            ({"total": "много", "gray": "1"}, "целое число"),
        ],
    )
    def test_wrong_numbers_are_not_saved(
        self, admin_client: Client, site: Site, data: dict[str, str], error: str
    ) -> None:
        response = _save(admin_client, site, data)
        assert response.status_code == 200
        assert not GrayScan.objects.exists()
        page = response.content.decode()
        assert "seo-warnline" in page
        assert error in page

    def test_viewer_sees_no_form_and_cannot_save(self, client: Client, site: Site) -> None:
        from django.contrib.auth.models import User

        user = User.objects.create_user("viewer", password="x", is_staff=True)
        user.user_permissions.add(Permission.objects.get(codename="view_site"))
        client.force_login(user)
        page = _card(client, site)
        assert "Google: всего страниц" in page
        assert "seo-gray-form" not in page
        _save(client, site, {"total": "10", "gray": "1"})
        assert not GrayScan.objects.exists()


PORTUGUESE_QUERY = (
    'site:acritica.net ("casino" OR "cassino" OR "póquer" OR "apostas" OR'
    ' "apostas esportivas" OR "Casino" OR "delta 8")'
)


def _post(client: Client, name: str, site: Site, data: dict[str, str]) -> Any:
    url = reverse(f"admin:sites_site_card_{name}", args=[site.pk])
    return client.post(url, data, headers={PARTIAL_HEADER: "1"})


class TestTermSets:
    """Наборы условий серости по языкам: стартовые, выбор набора, разбор условий (ADR-074)."""

    def test_sets_come_from_migration(self) -> None:
        english = _english()
        assert len(english) == 29
        assert (english[0], english[-1]) == ("casino", "xanax")
        portuguese = GrayTermSet.objects.get(language="pt").terms
        assert portuguese[:3] == ["casino", "cassino", "poker"]
        # Сокращён до лимита Google вместе с site:домен.
        query = gray_scan.gray_query("acritica.net", portuguese)
        assert gray_scan.word_count(query) == gray_scan.GOOGLE_WORD_LIMIT

    def test_site_language_wins(self) -> None:
        site = Site(domain="example.com.br", language="pt")
        choice = gray_scan.choose(site, list(GrayTermSet.objects.all()), None)
        assert choice.term_set is not None
        assert (choice.term_set.language, choice.reason) == ("pt", "по языку площадки")
        assert not choice.picked

    def test_domain_zone_without_language(self) -> None:
        site = Site(domain="acritica.com.br")
        choice = gray_scan.choose(site, list(GrayTermSet.objects.all()), None)
        assert choice.term_set is not None
        assert (choice.term_set.language, choice.reason) == ("pt", "по домену .br")

    @pytest.mark.parametrize(
        ("language", "domain", "reason"),
        [
            ("de", "spiegel.de", "для языка «Немецкий» набора нет — взят «Английский»"),
            (None, "example.com", "язык площадки не известен — взят «Английский»"),
        ],
    )
    def test_falls_back_to_english(self, language: str | None, domain: str, reason: str) -> None:
        site = Site(domain=domain, language=language)
        choice = gray_scan.choose(site, list(GrayTermSet.objects.all()), None)
        assert choice.term_set is not None
        assert (choice.term_set.language, choice.reason) == ("en", reason)

    def test_without_english_first_by_name(self) -> None:
        GrayTermSet.objects.filter(language="en").delete()
        GrayTermSet.objects.create(language="de", terms=["casino"])
        choice = gray_scan.choose(
            Site(domain="a.fr", language="fr"), list(GrayTermSet.objects.all()), None
        )
        assert choice.term_set is not None
        assert choice.term_set.language == "de"

    def test_picked_set_wins_and_unknown_pick_is_ignored(self) -> None:
        sets = list(GrayTermSet.objects.all())
        site = Site(domain="example.com", language="en")
        picked = gray_scan.choose(site, sets, "pt")
        assert picked.term_set is not None
        assert (picked.term_set.language, picked.reason, picked.picked) == (
            "pt",
            "выбран вами",
            True,
        )
        stale = gray_scan.choose(site, sets, "de")
        assert stale.term_set is not None
        assert (stale.term_set.language, stale.picked) == ("en", False)

    def test_no_sets(self) -> None:
        assert gray_scan.choose(Site(domain="a.com"), [], None).term_set is None

    def test_terms_one_per_line(self) -> None:
        text = "casino\n\n  online   casino \nCasino\nforex\n"
        assert gray_scan.clean_terms(text) == ["casino", "online casino", "forex"]

    def test_whole_google_query_is_split(self) -> None:
        assert gray_scan.clean_terms(PORTUGUESE_QUERY) == [
            "casino",
            "cassino",
            "póquer",
            "apostas",
            "apostas esportivas",
            "delta 8",
        ]

    def test_empty_terms_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="хотя бы одно"):
            gray_scan.clean_terms(" \n ")

    def test_language_names_and_flags(self) -> None:
        assert languages.language_name("pt") == "Португальский"
        assert gray_scan.language_flag("pt") == "pt"
        assert gray_scan.language_flag("en") == "gb"
        assert gray_scan.language_flag("xx") is None
        assert languages.domain_language("site.io") is None


class TestCardTermSets:
    """Выбор, добавление, правка и удаление набора из карточки площадки (ADR-074)."""

    @pytest.fixture
    def brazil(self) -> Site:
        return Site.objects.create(domain="acritica.net", language="pt")

    def test_card_uses_site_language(self, admin_client: Client, brazil: Site) -> None:
        page = _card(admin_client, brazil)
        assert "&quot;cassino&quot;" in page
        assert "по языку площадки · условий: 28" in page
        assert '<option value="" data-flag="pt" selected>Авто — Португальский</option>' in page
        assert 'data-gray-toggle="add"' in page
        assert 'name="language" value="pt"' in page  # набор уходит в замер

    def test_pick_is_kept_for_session(self, admin_client: Client, site: Site, brazil: Site) -> None:
        response = _post(admin_client, "gray_language", site, {"language": "pt"})
        assert response.status_code == 200
        assert "выбран вами" in response.content.decode()
        # Следующая площадка — с тем же набором, хотя язык у неё свой.
        other = Site.objects.create(domain="other.com", language="en")
        page = _card(admin_client, other)
        assert "&quot;cassino&quot;" in page
        assert '<option value="pt" data-flag="pt" selected>Португальский</option>' in page
        _post(admin_client, "gray_language", site, {"language": ""})
        assert "&quot;cassino&quot;" not in _card(admin_client, other)
        assert "&quot;cassino&quot;" in _card(admin_client, brazil)

    def test_add_set(self, admin_client: Client, site: Site) -> None:
        data = {"original": "", "language": "de", "terms": "Kasino\nKredit\n"}
        response = _post(admin_client, "gray_set", site, data)
        assert response.status_code == 200
        assert GrayTermSet.objects.get(language="de").terms == ["Kasino", "Kredit"]
        assert '<option value="de" data-flag="de">Немецкий</option>' in response.content.decode()

    def test_existing_language_is_rejected(self, admin_client: Client, site: Site) -> None:
        data = {"original": "", "language": "pt", "terms": "casino"}
        page = _post(admin_client, "gray_set", site, data).content.decode()
        assert "Набор «Португальский» уже есть — правьте его." in page
        assert GrayTermSet.objects.get(language="pt").terms != ["casino"]
        # Редактор нового набора открыт, введённое на месте.
        editor = page[page.index('data-gray-editor="add"') :]
        assert editor[: editor.index(">")].strip() == 'data-gray-editor="add"'
        assert ">casino</textarea>" in editor

    def test_edit_set_and_move_pick(self, admin_client: Client, site: Site) -> None:
        _post(admin_client, "gray_language", site, {"language": "pt"})
        data = {"original": "pt", "language": "es", "terms": "casino\napuestas"}
        page = _post(admin_client, "gray_set", site, data).content.decode()
        assert not GrayTermSet.objects.filter(language="pt").exists()
        assert GrayTermSet.objects.get(language="es").terms == ["casino", "apuestas"]
        assert "&quot;apuestas&quot;" in page
        assert "выбран вами" in page

    def test_delete_set_clears_pick(self, admin_client: Client, site: Site) -> None:
        _post(admin_client, "gray_language", site, {"language": "pt"})
        gray_scan.record(site, Reading(100, 1, "q1", "q2", language="pt"))
        page = _post(admin_client, "gray_set_delete", site, {"language": "pt"}).content.decode()
        assert not GrayTermSet.objects.filter(language="pt").exists()
        assert GrayScan.objects.filter(site=site).count() == 1
        assert "по языку площадки" in page
        # Замер помнит набор и после удаления.
        assert 'title="Набор «Португальский»"' in page

    def test_scan_keeps_set_language(self, admin_client: Client, site: Site) -> None:
        _save(admin_client, site, {"total": "100", "gray": "2", "language": "en"})
        assert (GrayScan.objects.get(site=site).breakdown or {})["language"] == "en"

    def test_viewer_cannot_change_sets(self, client: Client, site: Site) -> None:
        from django.contrib.auth.models import User

        user = User.objects.create_user("viewer", password="x", is_staff=True)
        user.user_permissions.add(Permission.objects.get(codename="view_site"))
        client.force_login(user)
        page = _card(client, site)
        assert "data-gray-toggle" not in page
        assert "data-gray-language" in page  # выбрать набор может и он
        data = {"original": "", "language": "de", "terms": "Kasino"}
        assert _post(client, "gray_set", site, data).status_code == 403
        assert _post(client, "gray_set_delete", site, {"language": "pt"}).status_code == 403
        assert GrayTermSet.objects.count() == 2


class TestSitesColumn:
    def test_latest_scan_in_zone_color(self, admin_client: Client, site: Site) -> None:
        old = gray_scan.record(site, Reading(100, 40, "q1", "q2"))
        # В одной транзакции now() один и тот же: прошлый замер — часом раньше.
        GrayScan.objects.filter(pk=old.pk).update(checked_at=old.checked_at - timedelta(hours=1))
        gray_scan.record(site, Reading(375, 12, "q1", "q2"))
        page = admin_client.get(LIST_URL, {"list": "all"}).content.decode()
        assert '<span class="seo-gray-zone seo-gray-green"' in page
        assert ">3,2 %</span>" in page
        assert ">40 %</span>" not in page

    def test_red_zone_and_sorting(self, admin_client: Client, site: Site) -> None:
        clean = Site.objects.create(domain="clean.com", language="en")
        gray_scan.record(site, Reading(100, 40, "q1", "q2"))
        gray_scan.record(clean, Reading(100, 1, "q1", "q2"))
        columns = admin_client.get(LIST_URL, {"list": "all"}).context["cl"].list_display
        order = f"-{columns.index('gray_cell')}"
        response = admin_client.get(LIST_URL, {"list": "all", "o": order})
        page = response.content.decode()
        assert '<span class="seo-gray-zone seo-gray-red"' in page
        assert page.index("youengage.me") < page.index("clean.com")

    def test_without_zones_no_color(self, admin_client: Client, site: Site) -> None:
        DomainSetting.objects.filter(key="GRAY_ZONES").delete()
        gray_scan.record(site, Reading(100, 40, "q1", "q2"))
        page = admin_client.get(LIST_URL, {"list": "all"}).content.decode()
        assert '<span class="seo-gray-zone seo-gray-none"' in page


class TestDomainTools:
    """Открыть сайт в новой вкладке и скопировать домен — у домена в «Площадках» и в карточке."""

    OPEN = 'href="https://youengage.me/" target="_blank" rel="noopener noreferrer"'

    def test_sites_row(self, admin_client: Client, site: Site) -> None:
        page = admin_client.get(LIST_URL, {"list": "all"}).content.decode()
        assert self.OPEN in page
        assert 'data-copy="youengage.me"' in page

    def test_card_panel_title_is_a_link(self, admin_client: Client, site: Site) -> None:
        page = _card(admin_client, site)
        title = page[page.index('<h2 class="seo-panel-title seo-card-title">') :]
        title = title[: title.index("</h2>")]
        assert self.OPEN in title
        assert 'data-copy="youengage.me"' in title

    def test_card_page_title_is_a_link(self, admin_client: Client, site: Site) -> None:
        url = reverse("admin:sites_site_card", args=[site.pk])
        page = admin_client.get(url).content.decode()
        title = page[page.index('<h1 class="seo-panel-title seo-card-title">') :]
        title = title[: title.index("</h1>")]
        assert self.OPEN in title
        assert ">youengage.me</a>" in title
        assert 'data-copy="youengage.me"' in title
