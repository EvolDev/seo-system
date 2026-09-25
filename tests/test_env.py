import pytest
from django.core.exceptions import ImproperlyConfigured

from config.env import env_bool, env_list, env_str, settings_module

# monkeypatch — встроенная фикстура pytest: меняет окружение только на время теста.


class TestEnvStr:
    def test_returns_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("X", "  value  ")
        assert env_str("X") == "value"

    def test_missing_without_default_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("X", raising=False)
        with pytest.raises(ImproperlyConfigured, match="X"):
            env_str("X")

    def test_empty_counts_as_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("X", "   ")
        with pytest.raises(ImproperlyConfigured):
            env_str("X")

    def test_missing_with_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("X", raising=False)
        assert env_str("X", default="d") == "d"


class TestEnvBool:
    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "On"])
    def test_true(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv("X", raw)
        assert env_bool("X", default=False) is True

    @pytest.mark.parametrize("raw", ["0", "false", "False", "no", "off"])
    def test_false(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv("X", raw)
        assert env_bool("X", default=True) is False

    def test_missing_gives_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("X", raising=False)
        assert env_bool("X", default=True) is True

    def test_garbage_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("X", "maybe")
        with pytest.raises(ImproperlyConfigured, match="maybe"):
            env_bool("X", default=False)


class TestEnvList:
    def test_splits_and_strips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("X", " a, b ,,c ")
        assert env_list("X") == ["a", "b", "c"]

    def test_missing_gives_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("X", raising=False)
        assert env_list("X") == []

    def test_missing_gives_copy_of_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("X", raising=False)
        default = ["a"]
        result = env_list("X", default=default)
        result.append("b")
        assert default == ["a"]


class TestSettingsModule:
    @pytest.mark.parametrize("name", ["local", "prod"])
    def test_known_environment(self, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
        monkeypatch.setenv("DJANGO_ENV", name)
        assert settings_module() == f"config.settings.{name}"

    def test_unknown_environment_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DJANGO_ENV", "production")
        with pytest.raises(ImproperlyConfigured, match="production"):
            settings_module()

    def test_missing_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DJANGO_ENV", raising=False)
        with pytest.raises(ImproperlyConfigured, match="DJANGO_ENV"):
            settings_module()
