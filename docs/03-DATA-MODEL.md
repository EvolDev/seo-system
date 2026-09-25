# 03. Модель данных

Готовый DDL — в `schema.sql` (версия 1.1, проверена применением на
PostgreSQL 16.13: 28 таблиц, 7 представлений). Здесь — смысл каждой
таблицы и обоснование неочевидных решений.

## Принципы

1. **Факты и снапшоты разделены.** `sites` хранит стабильные атрибуты,
   `site_metrics` и `site_prices` — историю замеров. Метрика никогда не
   перезаписывается.
2. **Продукт — сущность первого класса.** Convertio и Clideo равноправны.
   «Пример статьи на Clideo» из старой таблицы — это обычное размещение
   другого продукта, проверяемое тем же чекером.
3. **Деньги — целые числа в центах + код валюты.** Никакого float.
4. **Итоговая цена не хранится, а считается** — в `v_site_latest` до
   размещения (`reference_total` и `expected_spend`, см. ниже) и из
   `placements.price_paid_cents` после.
5. **Статусы — перечисления, обязательные.** Пустой статус запрещён:
   в рабочей таблице на 17.09.2026 было 530 строк из 568 без статуса, и
   восстановить их намерение уже невозможно.
6. **Сырые ответы API кладём в JSONB.** Через полгода понадобится поле,
   которого сегодня нет в схеме, — оно уже будет в истории.
7. **Одна точка правды для производных признаков.** «Пишем ли мы
   статью», последние метрики, итог цены — только в `v_site_latest`, не
   отдельными колонками, которые разъедутся при обновлении цен.

---

## Блок 1. Площадки и метрики

### `products`
Продвигаемые продукты.

| Поле | Тип | Описание |
|---|---|---|
| id | bigserial PK | |
| name | text | «Convertio» |
| domain | text unique | `convertio.co` |
| is_active | bool | Clideo может быть неактивен, но история нужна |

### `sites`
Площадка-донор. Стабильные атрибуты, меняющиеся редко.

| Поле | Тип | Описание |
|---|---|---|
| id | bigserial PK | |
| domain | text unique | нормализованный, без схемы и www |
| collaborator_url | text | карточка на маркетплейсе |
| source | text | откуда взяли домен |
| language | text | основной язык сайта — первый в списке Collaborator |
| languages | text[] | все языки из карточки: «Украинский, Русский» → два элемента |
| topics | text[] | категории Collaborator, их несколько: «Бизнес и финансы, Технологии» |
| declared_topics | text[] | «Особые тематики»: что площадка **готова принимать** (казино, форекс…). Не результат проверки индекса |
| site_type | text | как в Collaborator: персональный блог, информационный сайт, СМИ, портал, корпоративный блог, интернет-магазин |
| links_allowed | smallint | сколько ссылок на продукт разрешает площадка |
| link_type | text | заявленный тип ссылки из карточки: `dofollow` / `nofollow`. Основа `STOP_NOFOLLOW` и предфильтра |
| marks_as_ad | bool | «Пометка о рекламе статья» = Да. Основа `STOP_AD_LABEL` в предфильтре |
| content_profile | jsonb | результат классификации P2: основная тематика, fit_score, пояснение |
| status | enum | `new`, `auditing`, `approved`, `rejected`, `placed`, `blacklisted` |
| reject_reason | text | почему отклонили |
| notes | text | свободный комментарий |
| content_selector | text | ручной CSS-селектор тела статьи, если эвристика краулера промахивается (E2-04) |
| imported_undecided | bool | строка пришла из Excel без решения — отличать от действительно новых |
| is_deleted | bool | мягкое удаление |
| created_at / updated_at | timestamptz | |

Индексы: `domain`, `status`.

Признак «пишем ли мы статью» здесь **не хранится** — он вычисляется из
последних цен в `v_site_latest`.

### `site_metrics`
Снапшот метрик Ahrefs. Одна строка на замер.

| Поле | Тип | Описание |
|---|---|---|
| id | bigserial PK | |
| site_id | FK sites | |
| dr | smallint | Domain Rating |
| organic_traffic | int | общий органический трафик |
| us_traffic | int | трафик из США |
| top_geo | text | страна №1 |
| top_geo_traffic | int | её трафик |
| total_keywords | int | число ключей в органике |
| source | enum | `ahrefs_api`, `manual`, `csv_import` |
| raw | jsonb | полный ответ API |
| checked_at | timestamptz | |

Индексы: `(site_id, checked_at desc)`.

### `site_prices`
Снапшот цен с маркетплейса. Цены меняются, история нужна.

| Поле | Тип | Описание |
|---|---|---|
| id | bigserial PK | |
| site_id | FK sites | |
| placement_cents | int | цена размещения |
| announce_cents | int | цена анонса на главной: `0` — бесплатно, `null` — анонс не предлагается или неизвестно |
| writing_cents | int | цена написания, null если не пишут |
| currency | char(3) | EUR |
| source | enum | `manual`, `collaborator_api` |
| checked_at | timestamptz | |

Расчётные значения — только в `v_site_latest`, не в колонках:
- `reference_total` = размещение + анонс (пустой анонс = 0). **С ним
  сравнивается ориентир 550 EUR.** Написание не входит ни в одном случае —
  так сказано в промпте аудитора: ≤ 50 EUR пишет площадка и «это не
  считается нашим отдельным расходом», > 50 или пусто — пишем сами;
- `expected_spend` = `reference_total` + написание, если площадка пишет
  сама (≤ 50 EUR). Сколько реально уйдёт площадке;
- `we_write` = написание пусто или > 50 EUR.

`0` в цене написания — площадка пишет бесплатно, это `we_write = false`.

### `gray_scans`
Результат проверки на серые тематики.

| Поле | Тип | Описание |
|---|---|---|
| id | bigserial PK | |
| site_id | FK sites | |
| total_indexed | int | всего страниц в индексе |
| gray_hits | int | найдено по серым запросам |
| ratio | numeric(5,2) | доля в процентах |
| breakdown | jsonb | по категориям из `04-DOMAIN-RULES.md` §1.4, плюс расхождение estimated count и фактической выдачи |
| sample_urls | jsonb | примеры найденных URL для глазами |
| method | enum | `serp_api`, `manual` |
| checked_at | timestamptz | |

### `site_audits`
История вердиктов по площадке. Именно таблица, а не поле: аудит
повторяется, старый вердикт должен быть виден.

| Поле | Тип | Описание |
|---|---|---|
| id | bigserial PK | |
| site_id | FK sites | |
| verdict | enum | `yes`, `no`, `borderline` |
| score | smallint | 0–100, интегральная оценка гибкой зоны |
| blockers | jsonb | сработавшие стоп-факторы |
| strengths / weaknesses | jsonb | что сильно, что слабо |
| summary | text | человекочитаемое обоснование |
| missing_data | jsonb | каких данных не хватило — модель не заполняет score наугад |
| price_flag | jsonb | превышение ценового ориентира: решение всегда за человеком |
| dossier | jsonb | снимок досье (E4-02), на котором вынесен вердикт; его же берут карточка и E6-01 |
| author | enum | `human`, `llm`, `system` — последнее для стопов кодом и импорта |
| model | text | какая модель, если llm |
| run_id | uuid | |
| created_at | timestamptz | |

---

## Блок 2. Размещения и ссылки

### `placements`

| Поле | Тип | Описание |
|---|---|---|
| id | bigserial PK | |
| site_id | FK sites | |
| product_id | FK products | |
| article_url | text | появляется после публикации |
| status | enum | `planned`, `ordered`, `writing`, `review`, `published`, `rejected`, `cancelled` |
| collaborator_order_id | text | номер заявки |
| placement_type | enum | `guest_post`, `link_insertion` — колонка «Тип ссылки» из Excel |
| ad_label_requested | bool | пометку «реклама» заказали мы сознательно (`flow.txt`) — тогда `AD_LABEL` не нарушение |
| ordered_at / published_at | timestamptz | |
| price_paid_cents | int | фактически заплачено |
| currency | char(3) | |
| is_indexed | bool | null = не проверялось |
| indexed_checked_at | timestamptz | |
| announce_on_homepage | bool | |
| clicks_from_homepage | smallint | сколько кликов до статьи |
| comment | text | что не так с полученной статьёй |
| run_id | uuid | |
| created_at / updated_at | timestamptz | |

Индексы: `(site_id, product_id)`, `status`, `published_at`.

### `placement_links`
Конкретная ссылка внутри статьи.

| Поле | Тип | Описание |
|---|---|---|
| id | bigserial PK | |
| placement_id | FK placements | |
| keyword_id | FK keywords, nullable | null для безанкорки |
| anchor | text | текст ссылки как в задании |
| target_url | text | |
| anchor_type | enum | `exact`, `diluted` — ключ; `branded`, `url`, `generic` — безанкорка (бренд, голый адрес, нейтральное «here») |
| rel | text | атрибут `rel` как на странице (`nofollow sponsored`…); `NULL` — атрибута нет, ссылка dofollow. Заполняет краулер |
| char_offset | int | позиция от начала статьи в символах |
| context_sentence | text | предложение вокруг ссылки — для критика и разбора интеграции |
| link_index | smallint | первая ссылка, вторая… |
| extraction_test_passed | bool | пройден ли тест на извлечение |
| is_alive | bool | |
| last_checked_at | timestamptz | |
| first_seen_at / lost_at | timestamptz | когда появилась, когда пропала |

Индексы: `placement_id`, `keyword_id`, `(is_alive, last_checked_at)`.

---

## Блок 3. Ключи и позиции

### `keywords`

| Поле | Тип | Описание |
|---|---|---|
| id | bigserial PK | |
| product_id | FK products | |
| keyword | text | |
| target_url | text | целевая страница |
| volume | int | локальный объём |
| global_volume | int | |
| tool | enum | `Main`, `Video`, `Audio`, `Image`, `Doc` |
| page_type | text | колонка Type: «Главная», «Основные разделы», «Video (xxx-yyy)», «Остальное»… Нужна для долей портфеля (Q10) |
| anchor_type | enum | тип анкора, если ключ используется как анкор; в файле анкоров его нет |
| is_active | bool | |

Уникальность: `(product_id, keyword)`.

`links_placed` и `links_waiting` **не хранятся** — считаются из
`placement_links` join `placements` по статусу. Это устраняет главный
источник расхождений в текущей таблице.

### `keyword_positions`

| Поле | Тип | Описание |
|---|---|---|
| id | bigserial PK | |
| keyword_id | FK keywords | |
| position | smallint | null если вне топ-100 |
| country | char(2) | |
| source | enum | `ahrefs_api`, `serp_api`, `manual`, `csv_import` |
| checked_at | date | |

Уникальность: `(keyword_id, country, checked_at)`.

---

## Блок 4. Контент и пул знаний

### `articles`

| Поле | Тип | Описание |
|---|---|---|
| id | bigserial PK | |
| placement_id | FK placements | |
| origin | enum | `platform`, `copywriter`, `system` |
| status | enum | `draft`, `validating`, `revising`, `needs_human` (три круга правок не помогли), `accepted`, `rejected` |
| current_version_id | FK article_versions | |
| brief | jsonb | задание: тема, анкоры, целевые URL, требования |
| plan | jsonb | структура из E6-03: разделы, факты, места ссылок |
| angle | text | угол подачи: how-to, comparison, problem-analysis, case, checklist — для ротации |
| run_id | uuid | |
| created_at / updated_at | timestamptz | |

### `article_versions`

| Поле | Тип | Описание |
|---|---|---|
| id | bigserial PK | |
| article_id | FK articles | |
| version | smallint | |
| body | text | текст в markdown или html |
| word_count | int | |
| prompt_variant_id | FK prompt_variants | чем сгенерировано |
| facts_used | jsonb | id использованных фактов |
| examples_used / patterns_used | jsonb | какие примеры и конструкции попали в промпт — без этого E8-02 не пересчитает веса |
| embedding | real[] | вектор текста для E6-06; при росте объёма — перенос в pgvector |
| similarity_max | numeric(4,3) | максимальная близость к прошлым статьям |
| similar_to_version_id | FK article_versions | с какой статьёй эта близость — «ближайший сосед» |
| is_human_edit | bool | версия создана правкой человека, а не генерацией |
| created_at | timestamptz | |

### `article_reviews`
Вердикт человека по версии статьи (E8-01). Отдельная таблица, потому что
вердикт привязан к **версии**, а не к статье, и по одной статье их бывает
несколько.

| Поле | Тип | Описание |
|---|---|---|
| id | bigserial PK | |
| article_version_id | FK article_versions | |
| verdict | enum | `accepted`, `needs_revision`, `rejected` |
| reason_codes | text[] | коды правил из `rules` — свободный текст без кода бесполезен для статистики |
| comment | text | |
| reviewer | text | кто принял решение |
| created_at | timestamptz | |

### `facts`
Пул проверенных утверждений.

| Поле | Тип | Описание |
|---|---|---|
| id | bigserial PK | |
| layer | enum | `product`, `domain`, `stat`, `comparison` |
| statement | text | само утверждение |
| detail | text | развёрнутая формулировка |
| tags | text[] | audio, video, image, doc, general |
| source_url | text | |
| source_title | text | |
| evidence | text | дословный фрагмент источника, подтверждающий утверждение (P3) |
| confidence | numeric(3,2) | уверенность извлечения 0–1 |
| verified_at | date | когда проверяли |
| expires_at | date | срок годности |
| status | enum | `active`, `stale`, `retired`, `pending_review` |
| language | text | `en` по умолчанию; заложено сразу под другие языки (Q5) |
| supersedes_id | FK facts | предыдущая версия факта: обновление не затирает старое значение |
| embedding | real[] | для поиска дублей при вводе (E5-03); pgvector — позже |
| usage_count | int | денормализация для скорости |

### `fact_usages`
Где какой факт использовался — для ротации.

| Поле | Тип | Описание |
|---|---|---|
| id | bigserial PK | |
| fact_id | FK facts | |
| article_version_id | FK article_versions | |
| site_id | FK sites | |
| used_at | timestamptz | |

Ключевой запрос: «факты, не использовавшиеся в последних N статьях».

### `anchor_patterns`
Принятые конструкции предложений-носителей.

| Поле | Тип | Описание |
|---|---|---|
| id | bigserial PK | |
| pattern | text | шаблон с плейсхолдерами |
| example | text | реальный принятый пример |
| tool | enum | под какую категорию |
| anchor_type | enum | |
| extraction_test_passed | bool | |
| language | text | |
| score | numeric(4,3) | вес, см. E8 |
| times_used / times_accepted | int | |
| is_active | bool | `false` — выведена из оборота, но не удалена |

### `rules`
Правила ТЗ в машиночитаемом виде.

| Поле | Тип | Описание |
|---|---|---|
| id | bigserial PK | |
| code | text unique | `LINK_POSITION_FIRST`, `LINK_REL_SPAM`… |
| description | text | формулировка для промпта и для человека |
| severity | enum | `critical`, `medium`, `soft` |
| check_type | enum | `deterministic`, `llm`, `human` — последнее для того, что в v1 проверяет человек (заметность ссылки) |
| params | jsonb | пороги конкретной проверки: 1000 символов, 2 внутренние ссылки |
| is_active | bool | |

Смысл: пороги живут в базе, а не в коде. Поменялось требование — правка
строки, а не деплой.

### `domain_settings`
Пороги и списки, у которых нет критичности: зоны DR и трафика, ценовые
ориентиры, окно ротации фактов, порог близости статей, белый список
авторитетных доменов. Ключ — значение в JSONB.

Раньше они числились в `rules`, но правило проверки обязано иметь
критичность и тип проверки, а у порога «зелёная зона DR — от 50» их нет:
вставка в `rules` падала на ограничении `NOT NULL` (найдено 23.09.2026).

### `examples`
Принятые и отклонённые образцы для few-shot.

| Поле | Тип | Описание |
|---|---|---|
| id | bigserial PK | |
| kind | enum | `positive`, `negative` |
| scope | enum | `full_article`, `paragraph`, `anchor_sentence`, `intro` |
| body | text | |
| reason | text | почему принят или отклонён |
| tags | text[] | |
| language | text | |
| rule_code | text | какое правило нарушено (для negative) |
| score | numeric(4,3) | вес |
| times_used / times_accepted | int | |

---

## Блок 5. Промпты, оценка, обучение

### `prompt_templates` и `prompt_variants`

`prompt_templates` — задача и имя: `task` (`generate_article`, `critic`,
`classify_topic`…), `name`, `is_active`, `created_at`. Текста в нём нет.

`prompt_variants` — конкретная формулировка: `template_id`, `label`,
`body` (текст промпта), `times_used`, `times_accepted`, `is_active`.
Доля принятых не хранится, а вычисляется из двух счётчиков.

Важно: в `llm_calls` хранится **ссылка на вариант**, а не текст промпта.
Иначе база распухнет за месяцы.

### `validation_results`

| Поле | Тип | Описание |
|---|---|---|
| id | bigserial PK | |
| article_version_id | FK article_versions | |
| rule_code | text | |
| passed | bool | |
| severity | enum | |
| details | jsonb | что именно нашли, где |
| checked_by | enum | `deterministic`, `llm`, `human` |

### `golden_set` и `eval_runs`

`golden_set` — фиксированные задания с эталоном.
`eval_runs` — результат прогона: дата, что менялось, доля прохождения,
сравнение с предыдущим прогоном.

---

## Блок 6. Наблюдаемость

### `checks`
Единый журнал всех проверок. Полиморфный.

| Поле | Тип | Описание |
|---|---|---|
| id | bigserial PK | |
| entity_type | text | `site`, `placement`, `placement_link`, `fact` |
| entity_id | bigint | |
| check_type | text | `indexation`, `link_alive`, `gray_scan`, `price`, `fact_freshness` |
| status | enum | `ok`, `failed`, `warning`, `error` |
| result | jsonb | |
| performed_by | enum | `system`, `human` |
| run_id | uuid | |
| checked_at | timestamptz | |
| next_check_at | timestamptz | |

Индексы: `(entity_type, entity_id)`, `(check_type, next_check_at)`.

Второй индекс — сердце напоминаний: «что проверять сегодня» становится
одним запросом.

### `llm_calls`

| Поле | Тип | Описание |
|---|---|---|
| id | bigserial PK | |
| task | text | |
| model | text | |
| prompt_variant_id | FK, nullable | |
| input_tokens / output_tokens | int | |
| cost_cents | int | |
| currency | char(3) | `USD` по умолчанию |
| duration_ms | int | |
| status | enum | `ok`, `error`, `invalid_json`, `timeout` |
| response | jsonb | ответ целиком |
| entity_type / entity_id | | к чему относится |
| run_id | uuid | |
| created_at | timestamptz | |

### `task_runs`

| Поле | Тип | Описание |
|---|---|---|
| id | bigserial PK | |
| task_name | text | |
| status | enum | `running`, `success`, `failed` |
| started_at / finished_at | timestamptz | |
| duration_ms | int | |
| error | text | |
| payload | jsonb | |
| run_id | uuid | |

### `api_usage`

| Поле | Тип | Описание |
|---|---|---|
| id | bigserial PK | |
| provider | text | `ahrefs`, `dataforseo`, `voyage` — **без LLM**: их расход живёт в `llm_calls`, иначе посчитается дважды |
| endpoint | text | |
| units | int | юниты или запросы |
| cost_cents | int | |
| currency | char(3) | `USD` по умолчанию; размещения — в EUR, поэтому суммы по валютам не складываются |
| run_id | uuid | |
| created_at | timestamptz | |

---

## Отчётные представления

Для дашбордов и аналитики — обычные SQL-представления, а не ORM:

- `v_keyword_coverage` — ключ, последняя позиция (US), размещено, в ожидании;
- `v_site_funnel` — площадки по статусам, отдельно импортированные без решения;
- `v_monthly_spend` — расходы по месяцам и валютам: API, LLM по моделям, размещения;
- `v_article_funnel` — статьи системы по месяцам: всего → с первой попытки →
  приняты → приняты без правок человека;
- `v_overdue_checks` — сущности, у которых срок **последней** проверки прошёл;
- `v_link_health` — живость ссылок по возрасту размещения;
- `v_site_latest` — площадка «на сегодня»: последние метрики, цены,
  серость и вердикт, плюс `we_write`, `reference_total`, `expected_spend`.
  На нём стоят список площадок (E9-01), карточка и предфильтр.

Все семь есть в `schema.sql` и проверены на тестовых данных 23.09.2026.
