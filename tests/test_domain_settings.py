"""Настройки: общее и локальное значение, выбор для продукта (E1-05, ADR-035)."""

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction

from apps.content.domain_settings import (
    PRODUCT_KEYS,
    SETTING_KEYS,
    IndexationSchedule,
    OfferRecheck,
    UploadPriceCap,
    get_setting,
    indexation_schedule,
    offer_recheck,
    set_product_setting,
    upload_price_cap,
    validate_setting,
)
from apps.content.models import DomainSetting
from apps.sites.models import Product

pytestmark = pytest.mark.django_db


@pytest.fixture
def convertio() -> Product:
    return Product.objects.create(name="Convertio", domain="convertio.co")


@pytest.fixture
def clideo() -> Product:
    return Product.objects.create(name="Clideo", domain="clideo.com")


class TestUniqueness:
    def test_two_general_values_of_one_key_are_rejected(self) -> None:
        DomainSetting.objects.create(key="DR_ZONES", value={"green": 50, "yellow": 35})
        with pytest.raises(IntegrityError), transaction.atomic():
            DomainSetting.objects.create(key="DR_ZONES", value={"green": 40, "yellow": 30})

    def test_general_and_local_values_coexist(self, convertio: Product) -> None:
        DomainSetting.objects.create(key="DR_ZONES", value={"green": 50, "yellow": 35})
        DomainSetting.objects.create(
            key="DR_ZONES", product=convertio, value={"green": 30, "yellow": 20}
        )
        assert DomainSetting.objects.filter(key="DR_ZONES").count() == 2


class TestGetSetting:
    def test_nothing_set(self, convertio: Product) -> None:
        assert get_setting("DR_ZONES", convertio.pk) is None

    def test_general_value_applies_to_product(self, convertio: Product) -> None:
        DomainSetting.objects.create(key="DR_ZONES", value={"green": 50, "yellow": 35})
        assert get_setting("DR_ZONES", convertio.pk) == {"green": 50, "yellow": 35}

    def test_local_value_overrides_general(self, convertio: Product, clideo: Product) -> None:
        DomainSetting.objects.create(key="DR_ZONES", value={"green": 50, "yellow": 35})
        DomainSetting.objects.create(
            key="DR_ZONES", product=clideo, value={"green": 30, "yellow": 20}
        )
        assert get_setting("DR_ZONES", clideo.pk) == {"green": 30, "yellow": 20}
        assert get_setting("DR_ZONES", convertio.pk) == {"green": 50, "yellow": 35}

    def test_local_value_without_general(self, convertio: Product, clideo: Product) -> None:
        DomainSetting.objects.create(
            key="TOOL_CATEGORIES", product=convertio, value=["Main", "Video"]
        )
        assert get_setting("TOOL_CATEGORIES", convertio.pk) == ["Main", "Video"]
        assert get_setting("TOOL_CATEGORIES", clideo.pk) is None

    def test_without_product_only_general(self, convertio: Product) -> None:
        DomainSetting.objects.create(
            key="DR_ZONES", product=convertio, value={"green": 30, "yellow": 20}
        )
        assert get_setting("DR_ZONES", None) is None

    def test_deleted_local_value_falls_back_to_general(self, convertio: Product) -> None:
        DomainSetting.objects.create(key="DR_ZONES", value={"green": 50, "yellow": 35})
        local = DomainSetting.objects.create(
            key="DR_ZONES", product=convertio, value={"green": 30, "yellow": 20}
        )
        local.delete()
        assert get_setting("DR_ZONES", convertio.pk) == {"green": 50, "yellow": 35}


def test_product_keys_are_known() -> None:
    assert set(PRODUCT_KEYS) <= set(SETTING_KEYS)


class TestSetProductSetting:
    def test_create_update_delete(self, convertio: Product) -> None:
        set_product_setting(convertio.pk, "TOOL_CATEGORIES", ["Main"])
        set_product_setting(convertio.pk, "TOOL_CATEGORIES", ["Main", "Video"])
        assert get_setting("TOOL_CATEGORIES", convertio.pk) == ["Main", "Video"]
        set_product_setting(convertio.pk, "TOOL_CATEGORIES", None)
        assert not DomainSetting.objects.filter(product=convertio).exists()

    def test_same_value_keeps_updated_at(self, convertio: Product) -> None:
        set_product_setting(convertio.pk, "TOOL_CATEGORIES", ["Main"])
        before = DomainSetting.objects.get(product=convertio).updated_at
        set_product_setting(convertio.pk, "TOOL_CATEGORIES", ["Main"])
        assert DomainSetting.objects.get(product=convertio).updated_at == before

    def test_general_value_is_untouched(self, convertio: Product) -> None:
        DomainSetting.objects.create(key="DR_ZONES", value={"green": 50})
        set_product_setting(convertio.pk, "DR_ZONES", None)
        assert get_setting("DR_ZONES", None) == {"green": 50}

    def test_unknown_key(self, convertio: Product) -> None:
        with pytest.raises(ValueError, match="MY_SETTING"):
            set_product_setting(convertio.pk, "MY_SETTING", 1)


SCHEDULE = {
    "enabled": True,
    "first_check_days": 3,
    "retry_days": 1,
    "recheck_days": 30,
    "alert_after_days": 30,
}


class TestIndexationSchedule:
    """Сроки проверки индексации — INDEXATION_SCHEDULE (E2-03)."""

    def test_general_value_comes_from_migration_switched_off(self) -> None:
        schedule = indexation_schedule(None)
        assert schedule == IndexationSchedule(
            enabled=False, first_check_days=3, retry_days=1, recheck_days=30, alert_after_days=30
        )

    def test_product_overrides_schedule(self, convertio: Product) -> None:
        set_product_setting(convertio.pk, "INDEXATION_SCHEDULE", {**SCHEDULE, "retry_days": 2})
        assert indexation_schedule(convertio.pk).retry_days == 2
        assert indexation_schedule(None).retry_days == 1

    @pytest.mark.parametrize(
        ("value", "message"),
        [
            ([], "ровно с полями"),
            ({**SCHEDULE, "extra": 1}, "ровно с полями"),
            ({k: v for k, v in SCHEDULE.items() if k != "enabled"}, "ровно с полями"),
            ({**SCHEDULE, "enabled": "yes"}, "enabled"),
            ({**SCHEDULE, "first_check_days": -1}, "first_check_days"),
            ({**SCHEDULE, "recheck_days": 1.5}, "recheck_days"),
            ({**SCHEDULE, "alert_after_days": True}, "alert_after_days"),
            ({**SCHEDULE, "retry_days": 0}, "retry_days — не меньше одного"),
        ],
    )
    def test_wrong_value_is_rejected(self, value: object, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            validate_setting("INDEXATION_SCHEDULE", value)

    def test_zero_alert_and_first_check_are_allowed(self) -> None:
        validate_setting(
            "INDEXATION_SCHEDULE", {**SCHEDULE, "first_check_days": 0, "alert_after_days": 0}
        )

    def test_set_product_setting_validates(self, convertio: Product) -> None:
        with pytest.raises(ValueError, match="retry_days"):
            set_product_setting(convertio.pk, "INDEXATION_SCHEDULE", {**SCHEDULE, "retry_days": 0})

    def test_missing_setting_is_a_configuration_error(self) -> None:
        DomainSetting.objects.filter(key="INDEXATION_SCHEDULE").delete()
        with pytest.raises(ImproperlyConfigured, match="INDEXATION_SCHEDULE"):
            indexation_schedule(None)

    def test_broken_setting_is_a_configuration_error(self) -> None:
        DomainSetting.objects.filter(key="INDEXATION_SCHEDULE").update(value={"enabled": True})
        with pytest.raises(ImproperlyConfigured, match="ровно с полями"):
            indexation_schedule(None)


class TestOfferRecheck:
    """Порог повторного разбора предложений — OFFER_RECHECK (E1-08, ADR-044)."""

    def test_general_value_comes_from_migration(self) -> None:
        assert offer_recheck() == OfferRecheck(min_change_pct=3.0)

    def test_fraction_is_allowed(self) -> None:
        validate_setting("OFFER_RECHECK", {"min_change_pct": 2.5})

    @pytest.mark.parametrize(
        ("value", "message"),
        [
            (3, "ровно с полем"),
            ({"min_change_pct": 3, "extra": 1}, "ровно с полем"),
            ({"min_change_pct": "3"}, "min_change_pct"),
            ({"min_change_pct": -1}, "min_change_pct"),
            ({"min_change_pct": True}, "min_change_pct"),
        ],
    )
    def test_wrong_value_is_rejected(self, value: object, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            validate_setting("OFFER_RECHECK", value)

    def test_missing_setting_is_a_configuration_error(self) -> None:
        DomainSetting.objects.filter(key="OFFER_RECHECK").delete()
        with pytest.raises(ImproperlyConfigured, match="OFFER_RECHECK"):
            offer_recheck()


class TestUploadPriceCap:
    """Порог «цена из файла похожа на ошибку» — UPLOAD_PRICE_CAP (E1-09, ADR-051)."""

    def test_general_value_comes_from_migration(self) -> None:
        cap = upload_price_cap()
        assert cap == UploadPriceCap(eur=5000.0)
        assert cap.eur_cents == 500_000

    def test_fraction_is_allowed(self) -> None:
        validate_setting("UPLOAD_PRICE_CAP", {"eur": 1499.99})

    @pytest.mark.parametrize(
        ("value", "message"),
        [
            (5000, "ровно с полем"),
            ({"eur": 5000, "usd": 1}, "ровно с полем"),
            ({"eur": "5000"}, "eur"),
            ({"eur": 0}, "eur"),
            ({"eur": True}, "eur"),
        ],
    )
    def test_wrong_value_is_rejected(self, value: object, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            validate_setting("UPLOAD_PRICE_CAP", value)

    def test_missing_setting_is_a_configuration_error(self) -> None:
        DomainSetting.objects.filter(key="UPLOAD_PRICE_CAP").delete()
        with pytest.raises(ImproperlyConfigured, match="UPLOAD_PRICE_CAP"):
            upload_price_cap()
