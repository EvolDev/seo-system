"""Ключ во фразе анкора и вид ссылки (E3-05, ADR-069).

Ссылка хранит базовый ключ (`keyword_id`), свой текст (`anchor`) и вид именно
этой ссылки (`anchor_type`). «Convertio's mp4 to mp3 converter» — ключ
«mp4 to mp3 converter», вид «разбавленный»: ссылка засчитывается ключу, его
странице и типу страниц, отдельный анкор под фразу не заводится.

Одна функция поиска на всех: форма ссылки, подсказка в панели, обе загрузки
размещений и привязка ссылок при загрузке анкоров. Ищем в одном списке анкоров —
регионе размещения (ADR-075): «Convertio» у США и у Бразилии — разные анкоры.

Правила поиска (`find`):
1. Полное совпадение текста с анкором — без регистра и лишних пробелов. Так
   находится и безанкорка («Convertio», «https://convertio.co/»).
2. Иначе — ключ внутри фразы целыми словами: «convert» не находится ни в
   «Convertio», ни в «converter». Безанкорку внутри фразы не ищем: «here» в
   длинной фразе не делает ссылку безанкорной. Формы слов («converters»,
   «converting … into …») не угадываем — ключ выбирает человек.
3. Задан адрес ссылки — сначала ключи, которые ведут на него: длинный ключ
   другой страницы не перебивает подходящий короткий.
4. Порядок — длиннее (по словам), при равной длине — заведённый раньше.
   Счётчики ссылок не участвуют: та же фраза при повторной загрузке
   привязывается к тому же ключу.
5. Во фразе есть ключ, который не входит в первый и ведёт на другую страницу,
   — выбор неоднозначен: ключ не подставляется, выбирает человек.

Вид ссылки (`kind`): текст равен ключу (те же слова) — вид ключа; ключ прямой и стоит
внутри фразы — разбавленный; ключ безанкорный и стоит внутри фразы — его вид;
ключа во фразе нет — вид не определить, его выбирает человек.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from apps.keywords.models import NAKED_TYPES, AnchorList, AnchorType, Keyword

NAKED = frozenset(str(kind) for kind in NAKED_TYPES)
# Слово — буквы и цифры подряд; апостроф и точка его разрывают:
# «Convertio's» — «convertio» и «s», «convertio.co» — «convertio» и «co».
_WORD = re.compile(r"\w+")


@dataclass(frozen=True)
class Candidate:
    """Анкор продукта, среди которых ищем ключ."""

    id: int
    keyword: str
    target_url: str
    anchor_type: str | None

    @property
    def is_naked(self) -> bool:
        return self.anchor_type in NAKED

    @property
    def words(self) -> tuple[str, ...]:
        return words(self.keyword)


@dataclass(frozen=True)
class Match:
    """Что нашлось во фразе: выбранный ключ и все найденные по порядку."""

    keyword: Candidate | None
    candidates: tuple[Candidate, ...] = ()
    exact: bool = False  # текст целиком равен анкору
    ambiguous: bool = False  # ключи на разные страницы — выбирает человек


def normalize(text: str) -> str:
    return " ".join(text.split()).casefold()


def words(text: str) -> tuple[str, ...]:
    return tuple(_WORD.findall(text.casefold()))


def same_url(first: str, second: str) -> bool:
    """Адреса совпадают с точностью до слеша в конце — как везде в загрузках."""
    return first.strip().rstrip("/") == second.strip().rstrip("/")


def candidates(product_id: int, anchor_list_id: int | None = None) -> list[Candidate]:
    """Активные анкоры списка — для новых привязок; список не задан — основной."""
    if anchor_list_id is None:
        main = AnchorList.main_of(product_id)
        anchor_list_id = main.pk if main is not None else 0
    return [
        Candidate(pk, text, url, kind)
        for pk, text, url, kind in Keyword.objects.filter(
            product_id=product_id, anchor_list_id=anchor_list_id, is_active=True
        )
        .order_by("pk")
        .values_list("pk", "keyword", "target_url", "anchor_type")
    ]


def find(text: str, pool: Iterable[Candidate], *, url: str | None = None) -> Match:
    """Ключ для текста ссылки по правилам модуля."""
    everything = list(pool)
    wanted = normalize(text)
    if not wanted:
        return Match(None)
    exact = sorted((c for c in everything if normalize(c.keyword) == wanted), key=lambda c: c.id)
    if exact:
        return Match(exact[0], tuple(exact), exact=True)
    phrase = words(text)
    found = [c for c in everything if not c.is_naked and _contains(phrase, c.words)]
    if url:
        same_page = [c for c in found if same_url(c.target_url, url)]
        if same_page:
            found = same_page
    found.sort(key=lambda c: (-len(c.words), c.id))
    if not found:
        return Match(None)
    first = found[0]
    others = [
        c
        for c in found[1:]
        if not _contains(first.words, c.words) and not same_url(c.target_url, first.target_url)
    ]
    if others:
        return Match(None, tuple(found), ambiguous=True)
    return Match(first, tuple(found))


def bind(
    text: str, pool: Iterable[Candidate], *, url: str | None = None
) -> tuple[Match, AnchorType | None]:
    """Ключ и вид для ссылки из файла: ключ нашёлся — вид определяется всегда."""
    found = find(text, pool, url=url)
    if found.keyword is None:
        return found, None
    return found, kind(text, found.keyword.keyword, found.keyword.anchor_type)


def why_unbound(found: Match) -> str:
    """Почему ссылка осталась без ключа — для отчёта загрузки."""
    if found.ambiguous:
        names = ", ".join(f"«{c.keyword}»" for c in found.candidates[:4])
        return f"во фразе ключи на разные страницы: {names} — выберите в размещении"
    return "ключа из списка во фразе нет"


def kind(text: str, keyword: str, keyword_type: str | None) -> AnchorType | None:
    """Вид ссылки с этим текстом и ключом; None — ключа во фразе нет."""
    own = AnchorType(keyword_type) if keyword_type else AnchorType.EXACT
    key = words(keyword)
    # Те же слова — та же ссылка: регистр, пробелы и знаки вокруг не разбавляют.
    if normalize(text) == normalize(keyword) or (key and words(text) == key):
        return own
    if not key or not _contains(words(text), key):
        return None
    return own if own in NAKED_TYPES else AnchorType.DILUTED


def _contains(phrase: Sequence[str], key: Sequence[str]) -> bool:
    size = len(key)
    if size == 0 or size > len(phrase):
        return False
    return any(tuple(phrase[i : i + size]) == tuple(key) for i in range(len(phrase) - size + 1))
