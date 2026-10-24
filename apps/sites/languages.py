"""Язык площадки — для угадываний в карточках.

Набор условий серости (ADR-074) выбирается по языку площадки из карточки
Collaborator, нет его — по зоне домена. Здесь — откуда берётся язык и как
называется; что по нему выбрать, решает модуль сам.
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


def language_name(code: str) -> str:
    """«Португальский» по `pt`; незнакомый код — сам код."""
    return LANGUAGE_NAMES.get(code, code)


def domain_language(domain: str) -> str | None:
    """Язык по зоне домена: `acritica.com.br` → `pt`; зона не говорит о языке — None."""
    return DOMAIN_LANGUAGES.get(domain.rsplit(".", 1)[-1].lower())


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
