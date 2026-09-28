"""Настройки: общее и локальное значение, выбор для продукта (E1-05, ADR-035)."""

import pytest
from django.db import IntegrityError, transaction

from apps.content.domain_settings import (
    PRODUCT_KEYS,
    SETTING_KEYS,
    get_setting,
    set_product_setting,
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
        assert not DomainSetting.objects.exists()

    def test_same_value_keeps_updated_at(self, convertio: Product) -> None:
        set_product_setting(convertio.pk, "TOOL_CATEGORIES", ["Main"])
        before = DomainSetting.objects.get().updated_at
        set_product_setting(convertio.pk, "TOOL_CATEGORIES", ["Main"])
        assert DomainSetting.objects.get().updated_at == before

    def test_general_value_is_untouched(self, convertio: Product) -> None:
        DomainSetting.objects.create(key="DR_ZONES", value={"green": 50})
        set_product_setting(convertio.pk, "DR_ZONES", None)
        assert get_setting("DR_ZONES", None) == {"green": 50}

    def test_unknown_key(self, convertio: Product) -> None:
        with pytest.raises(ValueError, match="MY_SETTING"):
            set_product_setting(convertio.pk, "MY_SETTING", 1)
