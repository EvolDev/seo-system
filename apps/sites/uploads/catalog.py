"""Выгрузка каталога Collaborator: встроенная разметка и перевод значений (ADR-044).

Каталог выгружается в английском интерфейсе (45 041 строка на 01.10.2026),
а база и настройки (`PROJECT_TOPICS`, `SPECIAL_TOPICS`) — в русском
написании Collaborator, как в таблице линкбилдинга. Тематики, особые
тематики, типы сайта и языки переводятся словарями ниже. Словари
сопоставлены по 1891 площадке, которая есть и в таблице, и в выгрузке, —
без единой спорной пары. Незнакомое значение пишется как есть и идёт в
отчёт.

Колонки, которым нет своего поля, — в «прочие данные» предложения
Collaborator: TF, CF, DA, общий трафик и остальное видно в карточке.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from apps.sites.importing.languages import language_code
from apps.sites.models import PlacementType
from apps.sites.uploads import values
from apps.sites.uploads.files import Row, Table, is_blank, show
from apps.sites.uploads.records import Parsed, Record, RowError, merge_duplicates

SOURCE = "Collaborator"

DOMAIN = "Domain"
COLLABORATOR_URL = "Collaborator URL"
CATEGORY = "Category"
LANGUAGES = "Website languages"
SITE_TYPE = "Website type"
SPECIAL = "Special categories"
LINK_TYPE = "Link type article"
MARKS_AS_AD = "Advertising mark article"
LINKS_ALLOWED = "Number of links article"
PRICE = "Publishing price article, EUR"
WRITING = "Writing price article, EUR"
ANNOUNCE = "Announcement price article, EUR"
GRAY = "Sensitive topics price, EUR"
DR = "DR"
TRAFFIC = "Organic traffic"
KEYWORDS = "Keywords"

REQUIRED = (
    DOMAIN, COLLABORATOR_URL, CATEGORY, LANGUAGES, SITE_TYPE, SPECIAL, LINK_TYPE,
    MARKS_AS_AD, LINKS_ALLOWED, PRICE, WRITING, ANNOUNCE, GRAY, DR, TRAFFIC, KEYWORDS,
)  # fmt: skip
# Колонки со своим полем; все прочие непустые — в «прочие данные».
MAPPED = frozenset(REQUIRED)

TOPICS: dict[str, str] = {
    "Astrology and Esotericism": "Астрология и эзотерика",
    "Auto and Moto": "Авто и мото",
    "Business and Finance": "Бизнес и финансы",
    "City Portals": "Городские порталы",
    "Construction and Repair": "Строительство и ремонт",
    "Cooking": "Кулинария",
    "Country House": "Дача",
    "Cryptocurrencies": "Криптовалюты",
    "Culture and Art": "Культура и искусство",
    "Ecology/Resource Conservation": "Экология/сохранение ресурсов",
    "Education and Science": "Образование и наука",
    "Electronics and Technology": "Электроника и техника",
    "Entertainment and Hobbies": "Развлечения и хобби",
    "Fashion and Beauty": "Мода и красота",
    "Furniture and Interior": "Мебель и интерьер",
    "Handmade and Crafts (DIY)": "Хендмейд и рукоделие (DIY)",
    "Health and Medicine": "Здоровье и медицина",
    "Home and Family": "Дом и семья",
    "Internet": "Интернет",
    "Law and Jurisprudence": "Право и юриспруденция",
    "Lifestyle": "Образ жизни",
    "Logistics and Cargo Transportation": "Логистика и грузоперевозки",
    "Manufacturing and Agriculture": "Производство и сельское хозяйство",
    "Marketing": "Маркетинг",
    "Media (News)": "СМИ (Новости)",
    "Mobile Technology": "Мобильные технологи",
    "Music and Cinema": "Музыка и кино",
    "Other": "Другое",
    "PC and Video Games": "Компьютерные и видеоигры",
    "Pets": "Домашние животные",
    "Photography and Videography": "Фотография и видеосъёмка",
    "Psychology and Personal Development": "Психология и личное развитие",
    "Real Estate": "Недвижимость",
    "Religion": "Религия",
    "SEO": "SEO",
    "Society, Politics, and Laws": "Общество, политика, законы",
    "Software and PC": "Компьютеры и ПО",
    "Sports and Healthy Nutrition": "Спорт и здоровое питание",
    "Technologies": "Технологии",
    "Tourism and Travel": "Туризм и путешествия",
    "Web Design": "Веб-дизайн",
    "Web Development": "Веб-разработка",
    "Websites for Shopping and Coupons": "Шопинг (сайты для покупок, купоны)",
    "Work": "Работа",
}
SPECIAL_TOPICS: dict[str, str] = {
    "Legal Betting and Casino": "Азартные игры",
    "Lending and Microloans": "Кредитование, микрозаймы",
    "Forex Brokers": "Форекс, брокеры",
    "Dating Websites": "Сайты знакомств",
}
SITE_TYPES: dict[str, str] = {
    "Personal blog": "Персональный блог",
    "Informational website": "Информационный сайт",
    "Media": "СМИ",
    "Portal": "Портал",
    "Corporate blog": "Корпоративный блог",
    "Online store": "Интернет-магазин",
}
LANGUAGE_NAMES: dict[str, str] = {
    "English": "Английский",
    "Ukrainian": "Украинский",
    "Russian": "Русский",
    "Spanish": "Испанский",
    "French": "Французский",
    "Polish": "Польский",
    "German": "Немецкий",
    "Dutch": "Голландский",
    "Portuguese": "Португальский",
    "Italian": "Итальянский",
    "Turkish": "Турецкий",
    "Arabic": "Арабский",
    "Romanian": "Румынский",
    "Bulgarian": "Болгарский",
    "Indonesian": "Индонезийский",
    "Czech": "Чешский",
    "Danish": "Датский",
    "Greek": "Греческий",
    "Croatian": "Хорватский",
    "Hungarian": "Венгерский",
    "Swedish": "Шведский",
    "Lithuanian": "Литовский",
    "Hindi": "Хинди",
    "Slovak": "Словацкий",
    "Kazakh": "Казахский",
    "Latvian": "Латышский",
    "Vietnamese": "Вьетнамский",
    "Serbian": "Сербский",
    "Finnish": "Финский",
    "Azerbaijan": "Азербайджанский",
    "Norwegian": "Норвежский",
    "Thai": "Тайский",
    "Estonian": "Эстонский",
    "Bosnian": "Боснийский",
    "Bengali": "Бенгальский",
    "Japanese": "Японский",
    "Cantonese": "Кантонский",
    "Georgian": "Грузинский",
    "Armenian": "Армянский",
    "Chinese": "Китайский",
    "Slovene": "Словенский",
    "Uzbek": "Узбекский",
    "Hebrew": "Иврит",
    "Korean": "Корейский",
    "Malay": "Малайский",
    "Tagalog": "Тагальский",
    "Telugu": "Телугу",
    "Tamil": "Тамильский",
    "Egyptian": "Египетский",
    "Latin": "Латинский",
    "Cypriot": "Кипрский",
    "Javanese": "Яванский",
}
_YES_NO = {"yes": True, "no": False}


@dataclass
class Unknown:
    """Значения, которых нет в словарях: пишутся как есть, в отчёт — со счётчиком."""

    topics: dict[str, int] = field(default_factory=dict)
    site_types: dict[str, int] = field(default_factory=dict)
    languages: dict[str, int] = field(default_factory=dict)

    def add(self, kind: dict[str, int], value: str) -> None:
        kind[value] = kind.get(value, 0) + 1

    def lines(self) -> list[str]:
        result = []
        for title, found in (
            ("тематика", self.topics),
            ("тип сайта", self.site_types),
            ("язык", self.languages),
        ):
            for value, amount in sorted(found.items()):
                result.append(f"{title} «{value}» — {amount}")
        return result


def missing_columns(table: Table) -> list[str]:
    headers = {column.header for column in table.columns}
    return [name for name in REQUIRED if name not in headers]


def looks_like_catalog(cells: Sequence[str]) -> bool:
    return DOMAIN in cells and COLLABORATOR_URL in cells


def parse_catalog(table: Table, unknown: Unknown) -> Parsed:
    """Выгрузка каталога → записи. Разметка встроена, вопросов нет."""
    index = {column.header: column.index for column in table.columns}
    extra = [(c.header, c.index) for c in table.columns if c.header not in MAPPED and c.filled]
    records: list[Record] = []
    errors: list[RowError] = []
    for row in table.rows:
        try:
            records.append(_record(row, index, extra, unknown))
        except ValueError as error:
            errors.append(RowError(row.line, str(error)))
    merged, duplicates = merge_duplicates(records)
    return Parsed(merged, errors, duplicates)


def split_topics(value: object, dictionary: dict[str, str], unknown: dict[str, int]) -> list[str]:
    """«Business and Finance, Society, Politics, and Laws» → две тематики, по-русски.

    Запятая бывает внутри названия, поэтому сначала ищутся известные
    названия с начала строки, самое длинное первым. Незнакомый кусок — до
    следующей запятой перед заглавной буквой — пишется как есть.
    """
    text = show(value)
    known = sorted(dictionary, key=len, reverse=True)
    result: list[str] = []
    while text:
        text = text.lstrip(", ")
        if not text:
            break
        name = next(
            (k for k in known if text.startswith(k) and text[len(k) :][:1] in ("", ",")), None
        )
        if name is not None:
            result.append(dictionary[name])
            text = text[len(name) :]
            continue
        end = _next_category_start(text)
        piece = text[:end].strip()
        unknown[piece] = unknown.get(piece, 0) + 1
        result.append(piece)
        text = text[end:]
    return result


def _next_category_start(text: str) -> int:
    for position, char in enumerate(text):
        if char == "," and text[position + 1 :].lstrip()[:1].isupper():
            return position
    return len(text)


def _record(
    row: Row, index: dict[str, int], extra: Sequence[tuple[str, int]], unknown: Unknown
) -> Record:
    def cell(name: str) -> object:
        return row.get(index[name])

    domain, _ = values.parse_domain(cell(DOMAIN))
    price = values.parse_money(cell(PRICE))
    if price is None:
        raise ValueError(f"{domain}: нет цены публикации")
    languages: list[str] = []
    for name in show(cell(LANGUAGES)).split(","):
        name = name.strip()
        if not name:
            continue
        translated = LANGUAGE_NAMES.get(name)
        if translated is None:
            unknown.add(unknown.languages, name)
        languages.append(translated or name)
    site_type = show(cell(SITE_TYPE)) or None
    if site_type is not None and site_type not in SITE_TYPES:
        unknown.add(unknown.site_types, site_type)
    marks = show(cell(MARKS_AS_AD)).lower()
    card: dict[str, object] = {
        "source": SOURCE,
        "collaborator_url": show(cell(COLLABORATOR_URL)) or None,
        "topics": split_topics(cell(CATEGORY), TOPICS, unknown.topics) or None,
        "declared_topics": split_topics(cell(SPECIAL), SPECIAL_TOPICS, unknown.topics) or None,
        "languages": languages or None,
        "language": language_code(languages[0]) if languages else None,
        "site_type": SITE_TYPES.get(site_type, site_type) if site_type else None,
        "links_allowed": values.parse_number(cell(LINKS_ALLOWED)),
        "link_type": values.parse_link_type(cell(LINK_TYPE)),
        "marks_as_ad": _YES_NO.get(marks),
    }
    return Record(
        line=row.line,
        domain=domain,
        offers={PlacementType.GUEST_POST: price},
        gray_cents=values.parse_money(cell(GRAY)),
        writing_cents=values.parse_money(cell(WRITING), free_is_zero=True),
        announce_cents=values.parse_money(cell(ANNOUNCE), free_is_zero=True),
        metrics={
            "dr": values.parse_number(cell(DR)),
            "organic_traffic": values.parse_number(cell(TRAFFIC)),
            "total_keywords": values.parse_number(cell(KEYWORDS)),
        },
        extra={header: show(row.get(i)) for header, i in extra if not is_blank(row.get(i))},
        card=card,
    )
