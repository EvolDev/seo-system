"""Язык площадки и язык выдачи страны — для угадываний в карточках.

Набор условий серости (ADR-074) и список анкоров региона (ADR-075) выбираются
одинаково: язык площадки из карточки Collaborator, нет его — по зоне домена.
Здесь — откуда берётся язык и как называется; что по нему выбрать, решает
каждый модуль сам.
"""

from apps.sites.importing.languages import LANGUAGE_CODES
from apps.sites.models import Site

# Названия языков — те же, что в карточке Collaborator: код → «Португальский».
LANGUAGE_NAMES: dict[str, str] = {code: name for name, code in LANGUAGE_CODES.items()}
# Язык по зоне домена — когда в карточке Collaborator языка нет. Только зоны,
# где язык почти всегда один: .com, .io, .ca, .be и подобные не угадываются.
DOMAIN_LANGUAGES: dict[str, str] = {
    "br": "pt",
    "pt": "pt",
    "de": "de",
    "at": "de",
    "fr": "fr",
    "es": "es",
    "mx": "es",
    "ar": "es",
    "cl": "es",
    "pe": "es",
    "it": "it",
    "nl": "nl",
    "pl": "pl",
    "ua": "uk",
    "ru": "ru",
    "cz": "cs",
    "sk": "sk",
    "ro": "ro",
    "bg": "bg",
    "hu": "hu",
    "gr": "el",
    "tr": "tr",
    "se": "sv",
    "dk": "da",
    "no": "no",
    "fi": "fi",
    "id": "id",
    "vn": "vi",
    "jp": "ja",
    "kr": "ko",
    "hr": "hr",
    "rs": "sr",
    "si": "sl",
    "lt": "lt",
    "lv": "lv",
    "ee": "et",
    "il": "he",
    "uk": "en",
    "us": "en",
    "au": "en",
    "ie": "en",
    "nz": "en",
}
# Язык выдачи страны: зона домена страны говорит о нём же. Британия — `gb`, а
# зона у неё `.uk`; Канада и Индия по зоне не угадываются, но выдача — английская.
COUNTRY_LANGUAGES: dict[str, str] = {
    **{zone: language for zone, language in DOMAIN_LANGUAGES.items() if zone != "uk"},
    "gb": "en",
    "ca": "en",
    "in": "en",
}


def language_name(code: str) -> str:
    """«Португальский» по `pt`; незнакомый код — сам код."""
    return LANGUAGE_NAMES.get(code, code)


def domain_language(domain: str) -> str | None:
    """Язык по зоне домена: `acritica.com.br` → `pt`; зона не говорит о языке — None."""
    return DOMAIN_LANGUAGES.get(domain.rsplit(".", 1)[-1].lower())


def country_language(country: str) -> str | None:
    """Язык выдачи страны: `us` → `en`, `br` → `pt`; не знаем — None."""
    return COUNTRY_LANGUAGES.get(country.lower())


def site_language(site: Site) -> tuple[str | None, str]:
    """Язык площадки и откуда он — для подписи рядом с выбором в карточке.

    Из карточки Collaborator, нет его — по зоне домена; зона о языке не
    говорит — языка нет (None), подпись пустая.
    """
    if site.language:
        return site.language, "по языку площадки"
    language = domain_language(site.domain)
    if language is None:
        return None, ""
    return language, f"по домену .{site.domain.rsplit('.', 1)[-1].lower()}"
