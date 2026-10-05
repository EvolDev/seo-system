"""Настройки в админке: страница продукта и раздел «Настройки» (E1-05, ADR-035)."""

import json
from collections.abc import Callable
from typing import Any

import pytest
from django.db import connection
from django.test import Client
from django.urls import reverse
from django.utils.html import escape

from apps.content.domain_settings import get_setting
from apps.content.models import DomainSetting
from apps.sites.models import Product, Site, SitePrice

pytestmark = pytest.mark.django_db

INLINE = "domain_settings"


def _form(product: Product | None = None, **fields: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": product.name if product else "Convertio",
        "domain": product.domain if product else "convertio.co",
        "is_active": "on",
        "price_total_eur": "",
        "price_writing_eur": "",
        "price_announce_eur": "",
        "project_topics": "",
        "tool_categories": "",
        "authority_domains": "",
        f"{INLINE}-TOTAL_FORMS": "0",
        f"{INLINE}-INITIAL_FORMS": "0",
        f"{INLINE}-MIN_NUM_FORMS": "0",
        f"{INLINE}-MAX_NUM_FORMS": "1000",
    }
    data.update(fields)
    return data


def _inline_rows(*rows: dict[str, Any], initial: int = 0) -> dict[str, Any]:
    data: dict[str, Any] = {
        f"{INLINE}-TOTAL_FORMS": str(len(rows)),
        f"{INLINE}-INITIAL_FORMS": str(initial),
    }
    for index, row in enumerate(rows):
        for name, value in row.items():
            data[f"{INLINE}-{index}-{name}"] = value
    return data


def _local(product: Product) -> dict[str, Any]:
    return dict(
        DomainSetting.objects.filter(product=product).values_list("key", "value").order_by("key")
    )


@pytest.fixture
def product() -> Product:
    return Product.objects.create(name="Convertio", domain="convertio.co")


def _change_url(product: Product) -> str:
    return reverse("admin:sites_product_change", args=[product.pk])


def _post(client: Client, url: str, data: dict[str, Any]) -> Any:
    response = client.post(url, data)
    errors = response.context["adminform"].errors if response.context else None
    assert response.status_code == 302, errors
    return response


class TestProductPage:
    def test_add_page_shows_product_settings(self, admin_client: Client) -> None:
        response = admin_client.get(reverse("admin:sites_product_add"))
        content = response.content.decode()
        assert "Настройки продукта" in content
        assert "Порог написания, EUR" in content
        assert "Белый список доменов" in content

    def test_new_product_with_settings(self, admin_client: Client) -> None:
        data = _form(
            price_total_eur="550",
            price_writing_eur="50",
            price_announce_eur="100",
            tool_categories="Main\nVideo\n\n Video \nAudio",
            authority_domains="https://www.W3.org/TR/\nietf.org\nw3.org",
        )
        _post(admin_client, reverse("admin:sites_product_add"), data)
        product = Product.objects.get(domain="convertio.co")
        assert _local(product) == {
            "AUTHORITY_DOMAINS": ["w3.org", "ietf.org"],
            "PRICE_REFERENCE": {"total_eur": 550, "writing_eur": 50, "announce_eur": 100},
            "TOOL_CATEGORIES": ["Main", "Video", "Audio"],
        }

    def test_saved_settings_are_shown_for_editing(
        self, admin_client: Client, product: Product
    ) -> None:
        DomainSetting.objects.create(
            key="PRICE_REFERENCE", product=product, value={"writing_eur": 50}
        )
        DomainSetting.objects.create(key="TOOL_CATEGORIES", product=product, value=["Main", "Doc"])
        form = admin_client.get(_change_url(product)).context["adminform"].form
        assert form["price_writing_eur"].value() == 50
        assert form["tool_categories"].value() == "Main\nDoc"

    def test_edit_and_clear(self, admin_client: Client, product: Product) -> None:
        url = _change_url(product)
        _post(
            admin_client,
            url,
            _form(product, price_total_eur="550", price_writing_eur="50", tool_categories="Main"),
        )
        _post(admin_client, url, _form(product, price_total_eur="600", tool_categories=""))
        # Стёртый порог и стёртый список — у продукта больше нет своих значений.
        assert _local(product) == {"PRICE_REFERENCE": {"total_eur": 600}}
        _post(admin_client, url, _form(product))
        assert _local(product) == {}

    def test_cleared_field_falls_back_to_general(
        self, admin_client: Client, product: Product
    ) -> None:
        DomainSetting.objects.create(key="AUTHORITY_DOMAINS", value=["wikipedia.org"])
        url = _change_url(product)
        _post(admin_client, url, _form(product, authority_domains="w3.org"))
        assert get_setting("AUTHORITY_DOMAINS", product.pk) == ["w3.org"]
        _post(admin_client, url, _form(product))
        assert get_setting("AUTHORITY_DOMAINS", product.pk) == ["wikipedia.org"]

    def test_invalid_input_is_rejected(self, admin_client: Client, product: Product) -> None:
        response = admin_client.post(
            _change_url(product), _form(product, price_writing_eur="-5", authority_domains="///")
        )
        errors = response.context["adminform"].form.errors
        assert set(errors) == {"price_writing_eur", "authority_domains"}
        assert _local(product) == {}

    def test_threshold_turns_on_expected_spend(
        self, admin_client: Client, product: Product, offer: Callable[..., SitePrice]
    ) -> None:
        # Критерий приёмки: после ввода порога expected_spend считается, без него — пусто.
        site = Site.objects.create(domain="example.com")
        offer(site, 40000, announce_cents=10000, writing_cents=4000)

        def spend() -> Any:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT expected_spend_cents FROM v_product_site_latest"
                    " WHERE site_id = %s AND product_id = %s",
                    [site.pk, product.pk],
                )
                return cursor.fetchone()[0]

        assert spend() is None
        _post(admin_client, _change_url(product), _form(product, price_writing_eur="50"))
        assert spend() == 54000
        _post(admin_client, _change_url(product), _form(product))
        assert spend() is None


class TestOtherSettings:
    def test_product_overrides_general_zone_and_returns(
        self, admin_client: Client, product: Product
    ) -> None:
        DomainSetting.objects.create(key="DR_ZONES", value={"green": 50, "yellow": 35})
        url = _change_url(product)
        row = {"key": "DR_ZONES", "value": '{"green": 30, "yellow": 20}', "description": ""}
        _post(admin_client, url, _form(product, **_inline_rows(row)))
        assert get_setting("DR_ZONES", product.pk) == {"green": 30, "yellow": 20}

        local = DomainSetting.objects.get(key="DR_ZONES", product=product)
        page = admin_client.get(url).content.decode()
        assert escape('{"green": 50, "yellow": 35}') in page  # общее значение рядом

        existing = {**row, "id": str(local.pk), "product": str(product.pk), "DELETE": "on"}
        _post(admin_client, url, _form(product, **_inline_rows(existing, initial=1)))
        assert get_setting("DR_ZONES", product.pk) == {"green": 50, "yellow": 35}

    def test_same_key_twice_is_rejected(self, admin_client: Client, product: Product) -> None:
        row = {"key": "DR_ZONES", "value": '{"green": 30}', "description": ""}
        response = admin_client.post(_change_url(product), _form(product, **_inline_rows(row, row)))
        assert response.status_code == 200
        assert not DomainSetting.objects.filter(product=product).exists()

    def test_key_taken_in_database_is_rejected(
        self, admin_client: Client, product: Product
    ) -> None:
        # Строку добавили в другой вкладке: в этой форме её нет.
        DomainSetting.objects.create(key="DR_ZONES", product=product, value={"green": 1})
        row = {"key": "DR_ZONES", "value": '{"green": 30}', "description": ""}
        response = admin_client.post(_change_url(product), _form(product, **_inline_rows(row)))
        assert response.status_code == 200
        assert "уже переопределена" in response.content.decode()
        assert DomainSetting.objects.get(product=product).value == {"green": 1}

    def test_product_keys_are_not_offered(self, admin_client: Client, product: Product) -> None:
        row = {"key": "PRICE_REFERENCE", "value": '{"writing_eur": 50}', "description": ""}
        response = admin_client.post(_change_url(product), _form(product, **_inline_rows(row)))
        assert response.status_code == 200
        assert not DomainSetting.objects.filter(product=product).exists()

    def test_product_keys_are_not_listed(self, admin_client: Client, product: Product) -> None:
        DomainSetting.objects.create(key="PRICE_REFERENCE", product=product, value={})
        formset = admin_client.get(_change_url(product)).context["inline_admin_formsets"][0]
        assert list(formset.formset.queryset) == []


class TestGeneralSettings:
    def test_lists_only_general_values(self, admin_client: Client, product: Product) -> None:
        DomainSetting.objects.create(key="DR_ZONES", value={"green": 50, "yellow": 35})
        DomainSetting.objects.create(key="DR_ZONES", product=product, value={"green": 30})
        response = admin_client.get(reverse("admin:content_domainsetting_changelist"))
        # Общие: DR_ZONES и из миграций content.0003–0007 — INDEXATION_SCHEDULE,
        # OFFER_RECHECK, UPLOAD_PRICE_CAP, GRAY_TERMS, GRAY_ZONES, ANCHOR_RECOMMEND.
        assert response.context["cl"].result_count == 7

    def test_add_general_value(self, admin_client: Client) -> None:
        url = reverse("admin:content_domainsetting_add")
        data = {"key": "DR_ZONES", "value": '{"green": 50, "yellow": 35}', "description": ""}
        _post(admin_client, url, data)
        assert get_setting("DR_ZONES", None) == {"green": 50, "yellow": 35}

    def test_duplicate_general_value_is_rejected(self, admin_client: Client) -> None:
        DomainSetting.objects.create(key="DR_ZONES", value={"green": 50, "yellow": 35})
        data = {"key": "DR_ZONES", "value": '{"green": 40}', "description": ""}
        response = admin_client.post(reverse("admin:content_domainsetting_add"), data)
        assert response.status_code == 200
        assert DomainSetting.objects.filter(key="DR_ZONES").count() == 1

    def test_unknown_key_is_rejected(self, admin_client: Client) -> None:
        data = {"key": "MY_SETTING", "value": "1", "description": ""}
        response = admin_client.post(reverse("admin:content_domainsetting_add"), data)
        assert response.status_code == 200
        assert not DomainSetting.objects.filter(key="MY_SETTING").exists()

    def test_general_value_cannot_be_deleted(self, admin_client: Client) -> None:
        setting = DomainSetting.objects.create(key="DR_ZONES", value={"green": 50})
        url = reverse("admin:content_domainsetting_delete", args=[setting.pk])
        assert admin_client.post(url, {"post": "yes"}).status_code == 403
        assert DomainSetting.objects.filter(pk=setting.pk).exists()


class TestSettingValueShape:
    """Значение с проверкой формы (INDEXATION_SCHEDULE): ошибка видна в форме (E2-03)."""

    def test_wrong_general_value_is_rejected(self, admin_client: Client) -> None:
        setting = DomainSetting.objects.get(key="INDEXATION_SCHEDULE", product=None)
        url = reverse("admin:content_domainsetting_change", args=[setting.pk])
        data = {"key": "INDEXATION_SCHEDULE", "value": '{"enabled": true}', "description": ""}
        response = admin_client.post(url, data)
        assert response.status_code == 200
        assert "ровно с полями" in str(response.context["adminform"].errors)
        setting.refresh_from_db()
        assert setting.value["enabled"] is False

    def test_general_schedule_can_be_switched_on(self, admin_client: Client) -> None:
        setting = DomainSetting.objects.get(key="INDEXATION_SCHEDULE", product=None)
        url = reverse("admin:content_domainsetting_change", args=[setting.pk])
        value = {**setting.value, "enabled": True}
        _post(admin_client, url, {"key": "INDEXATION_SCHEDULE", "value": json.dumps(value)})
        setting.refresh_from_db()
        assert setting.value["enabled"] is True

    def test_wrong_product_value_is_rejected(self, admin_client: Client, product: Product) -> None:
        row = {"key": "INDEXATION_SCHEDULE", "value": '{"retry_days": 0}', "description": ""}
        response = admin_client.post(_change_url(product), _form(product, **_inline_rows(row)))
        assert response.status_code == 200
        assert not DomainSetting.objects.filter(product=product).exists()


class TestGrayTermsEditor:
    """Условия серости — по одному на строку, а не JSON (E2-06)."""

    @pytest.fixture
    def url(self) -> str:
        setting = DomainSetting.objects.get(key="GRAY_TERMS", product=None)
        return reverse("admin:content_domainsetting_change", args=[setting.pk])

    def test_terms_are_shown_one_per_line(self, admin_client: Client, url: str) -> None:
        form = admin_client.get(url).context["adminform"].form
        assert form.initial["value"].splitlines()[:3] == ["casino", "poker", "betting"]
        assert form.fields["key"].disabled

    def test_lines_are_saved_as_list(self, admin_client: Client, url: str) -> None:
        text = "casino\n\n  online casino  \ncasino\nforex\n"
        _post(admin_client, url, {"key": "GRAY_TERMS", "value": text, "description": ""})
        assert get_setting("GRAY_TERMS", None) == ["casino", "online casino", "forex"]

    def test_key_is_not_changed(self, admin_client: Client, url: str) -> None:
        _post(admin_client, url, {"key": "DR_ZONES", "value": "casino", "description": ""})
        assert get_setting("GRAY_TERMS", None) == ["casino"]
        assert get_setting("DR_ZONES", None) is None

    def test_empty_list_is_rejected(self, admin_client: Client, url: str) -> None:
        response = admin_client.post(url, {"key": "GRAY_TERMS", "value": " \n", "description": ""})
        assert response.status_code == 200
        assert DomainSetting.objects.get(key="GRAY_TERMS", product=None).value[-1] == "xanax"

    def test_list_shows_terms_in_a_row(self, admin_client: Client) -> None:
        response = admin_client.get(reverse("admin:content_domainsetting_changelist"))
        assert "casino, poker, betting" in response.content.decode()
