# 13. Конфигурация

Три уровня настроек, и путать их нельзя:

| Уровень | Где живёт | Меняется | Пример |
|---|---|---|---|
| Секреты и подключения | переменные окружения | при деплое | ключи API, пароль БД |
| Поведение системы | `settings.py` + env | при деплое | модель для задачи, размер батча |
| Правила проверки статьи | таблица `rules` | на лету, без деплоя | 1000 символов до первой ссылки |
| Пороги аудита и списки | таблица `domain_settings` | на лету, без деплоя | зелёная зона DR, белый список доменов |

Правило: **всё, что SEO-специалист может захотеть поменять без
программиста, живёт в базе, а не в коде.**

---

## 1. Переменные окружения (`.env.example`)

```bash
# --- Общее ---
DJANGO_ENV=local                 # local | prod
DJANGO_SECRET_KEY=
DJANGO_DEBUG=true
ALLOWED_HOSTS=localhost,127.0.0.1
TIME_ZONE=Europe/Moscow

# --- База ---
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_DB=seo
POSTGRES_USER=seo
POSTGRES_PASSWORD=

# --- Очереди ---
REDIS_URL=redis://redis:6379/0
CELERY_TASK_ALWAYS_EAGER=false   # true в тестах

# --- LLM ---
ANTHROPIC_API_KEY=
LLM_MODEL_CHEAP=
LLM_MODEL_STRONG=
LLM_MAX_COST_PER_CHAIN_CENTS=200 # предохранитель на одну цепочку
LLM_TIMEOUT_SECONDS=120
LLM_MAX_RETRIES=3

# --- Эмбеддинги (E6-06, E5-03) --- у Anthropic своей модели нет, ADR-022
EMBEDDINGS_PROVIDER=voyage
VOYAGE_API_KEY=
EMBEDDINGS_MODEL=voyage-4-lite

# --- SERP ---
SERP_PROVIDER=dataforseo         # dataforseo | serper
DATAFORSEO_LOGIN=
DATAFORSEO_PASSWORD=
SERP_DAILY_BUDGET_CENTS=500
SERP_CACHE_HOURS=24

# --- Ahrefs ---
AHREFS_API_KEY=
AHREFS_ENABLED=false             # см. Q2, пока не подтверждён доступ
AHREFS_DAILY_UNITS_LIMIT=50000

# --- Краулер ---
CRAWLER_USER_AGENT=
CRAWLER_TIMEOUT_SECONDS=30
CRAWLER_MAX_REDIRECTS=5
CRAWLER_RATE_LIMIT_PER_DOMAIN=1  # запросов в секунду

# --- Уведомления ---
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
SENTRY_DSN=

# --- Продукты ---
PRIMARY_PRODUCT_DOMAIN=convertio.co
```

**Правила:**
- в репозитории только `.env.example` с пустыми значениями;
- `AHREFS_ENABLED=false` по умолчанию — чтобы случайно не сжечь юниты
  на этапе разработки;
- любой ключ, попавший в git, считается скомпрометированным и ротируется,
  даже если коммит отменён.

---

## 2. Правила и пороги

Заполняются миграцией данных в E5-02. Ниже — стартовые значения.

### 2.1. Проверка статьи — таблица `rules`

`critical_if_beyond_share`: ссылка дальше этой доли текста — результат
проверки записывается с `severity = critical`, хотя базовая критичность
правила — `medium` (`04-DOMAIN-RULES.md` §3.2).

`check_type`: `deterministic` — код, `llm` — критик, `human` — вычитка.
Критичность сверена с чек-листом промпта аудитора; где промпт
противоречит сам себе, решение и причина записаны в
`04-DOMAIN-RULES.md` §3.

| code | severity | check_type | params |
|---|---|---|---|
| `LINK_NOFOLLOW` | critical | deterministic | `{}` |
| `LINK_REL_SPAM` | critical | deterministic | `{"forbidden": ["sponsored","ugc"]}` |
| `AD_LABEL` | critical | deterministic | `{"markers": ["sponsored","advertisement","partner content","promoted"]}` |
| `WRONG_TARGET` | critical | deterministic | `{}` |
| `ANCHOR_MISMATCH` | critical | deterministic | `{"case_sensitive": false}` |
| `NOINDEX` | critical | deterministic | `{}` |
| `BAD_INTEGRATION` | critical | llm | `{}` |
| `LINK_POSITION_FIRST` | medium | deterministic | `{"max_chars": 1000, "tolerance": 200, "critical_if_beyond_share": 0.33}` |
| `LINK_POSITION_SECOND` | medium | deterministic | `{"max_chars": 2000, "tolerance": 200, "critical_if_beyond_share": 0.33}` |
| `PRODUCT_LINKS_COUNT` | medium | deterministic | `{"min": 1, "max": 2}` |
| `OFF_TOPIC` | medium | llm | `{}` |
| `NO_AUTHORITY_LINKS` | medium | deterministic | `{"min": 1, "whitelist_key": "AUTHORITY_DOMAINS"}` |
| `NO_HOMEPAGE_ANNOUNCE` | medium | deterministic | `{}` |
| `DEPTH_OVER_2_CLICKS` | medium | deterministic | `{"max_clicks": 2}` |
| `RAW_AI_TEXT` | medium | llm | `{}` |
| `LINK_NOT_VISIBLE` | medium | human | `{}` |
| `TOO_MANY_INTERNAL` | soft | deterministic | `{"max": 2}` |
| `NO_TOC` | soft | deterministic | `{}` |
| `NO_TABLES` | soft | deterministic | `{}` |
| `WEAK_STRUCTURE` | soft | deterministic | `{"min_h2": 3, "min_lists": 1, "min_images": 1}` |
| `ARTICLE_LENGTH` | soft | deterministic | `{"min_words": 900, "max_words": 2500}` |
| `STYLE_ISSUES` | soft | llm | `{}` |

### 2.2. Аудит площадки — таблица `domain_settings`

| key | value |
|---|---|
| `TRAFFIC_ZONES` | `{"green": 10000, "yellow": 3000}` |
| `DR_ZONES` | `{"green": 50, "yellow": 35}` |
| `KEYWORDS_ZONES` | `{"green": 100, "yellow": 20}` |
| `GRAY_ZONES` | `{"green": 10, "yellow": 25}` |
| `PRICE_REFERENCE` | `{"total_eur": 550, "writing_eur": 50, "announce_eur": 100}` |
| `PROJECT_TOPICS` | список тематик проекта из `04-DOMAIN-RULES.md` §1.0 — в написании Collaborator, «Мобильные технологи» без последней «и» |
| `SPECIAL_TOPICS` | четыре категории «особых тематик»: «Азартные игры», «Кредитование, микрозаймы», «Форекс, брокеры», «Сайты знакомств» |
| `GRAY_TERMS` | категории и термины из `04-DOMAIN-RULES.md` §1.4 |

### 2.3. Контент — таблица `domain_settings`

| key | value |
|---|---|
| `SIMILARITY_THRESHOLD` | `{"max_cosine": 0.85}` — см. Q6, подобрать эмпирически |
| `FACT_ROTATION_WINDOW` | `{"last_n_articles": 10}` |
| `REVISION_LIMIT` | `{"max_iterations": 3}` |
| `ANCHOR_REUSE_WINDOW` | `{"days": 90, "max_on_similar_sites": 3}` — см. Q4 |
| `AUTHORITY_DOMAINS` | белый список авторитетных доменов, см. §4 |

---

## 3. Расписание задач (Celery beat)

| Задача | Расписание |
|---|---|
| Проверка индексации (свежие) | ежедневно 06:00 |
| Проверка живости ссылок | ежедневно 07:00, порциями |
| Утренняя сводка в Telegram | ежедневно 09:00 |
| Обновление позиций ключей | понедельник 05:00 |
| Обновление метрик площадок | 1-е число месяца |
| Gray scan активных площадок | 1-е число квартала |
| Напоминание обновить цены вручную | 1-е число квартала, пока нет API Collaborator |
| Контроль свежести фактов | 1-е число месяца |
| Проверка дневных лимитов API | каждый час |

Время указано в `TIME_ZONE`.

---

## 4. Белый список авторитетных доменов

Нужен для правила `NO_AUTHORITY_LINKS`. Хранится в `domain_settings`
под ключом `AUTHORITY_DOMAINS`. Стартовый набор задаёт человек — я не
угадываю, какие источники вы считаете уместными.

Ориентир по типам: справочные ресурсы (Wikipedia), крупные
технологические медиа, спецификации и документация форматов (W3C, IETF,
разработчики кодеков), статистические и исследовательские сайты.

Задача на заполнение — E5-02.

---

## 5. Что настраивается только кодом

Чтобы не было соблазна вынести в базу лишнее:

- структура промптов (живёт в `prompt_variants`, но формат — код);
- схемы JSON-ответов;
- логика теста на извлечение;
- порядок шагов пайплайна генерации;
- модель данных.
