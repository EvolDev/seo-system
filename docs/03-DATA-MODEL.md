# 03. Модель данных

Готовый DDL — в `schema.sql` (версия 1.4, проверена применением на
PostgreSQL 16: 31 таблица, 8 представлений). Здесь — смысл каждой
таблицы и обоснование неочевидных решений. Django-миграции дают ту же
схему; известные расхождения, которые даёт Django, — ADR-029.

## Принципы

1. **Факты и снапшоты разделены.** `sites` хранит стабильные атрибуты,
   `site_metrics` и `site_prices` — историю замеров. Метрика никогда не
   перезаписывается.
2. **Продукт — сущность первого класса.** Convertio и Clideo равноправны.
   «Пример статьи на Clideo» из старой таблицы — это обычное размещение
   другого продукта, проверяемое тем же чекером.
3. **Деньги — целые числа в центах + код валюты.** Никакого float.
4. **Итоговая цена не хранится, а считается** — до размещения в
   представлениях (`reference_total` в `v_site_latest`, `expected_spend`
   в `v_product_site_latest`, см. ниже), после — из
   `placements.price_paid_cents`.
5. **Статусы — перечисления, обязательные.** Пустой статус запрещён:
   в рабочей таблице на 17.09.2026 было 530 строк из 568 без статуса, и
   восстановить их намерение уже невозможно.
6. **Сырые ответы API кладём в JSONB.** Через полгода понадобится поле,
   которого сегодня нет в схеме, — оно уже будет в истории.
7. **Одна точка правды для производных признаков.** Последние метрики
   и цены — только в `v_site_latest`, «пишем ли мы статью» и итог
   цены — только в `v_product_site_latest`. Не отдельными колонками,
   которые разъедутся при обновлении цен.
8. **Несколько продуктов (ADR-030).** Три слоя:
   - площадка сама по себе — факты, которые от продукта не зависят:
     `sites`, метрики, цены, серость. Хранятся один раз;
   - площадка для продукта — решение по ней: статус, причина отказа,
     соответствие тематике (`product_sites`), вердикт (`site_audits`).
     Своё для каждого продукта;
   - правила, настройки и знания — `product_id` пусто: общее для всех
     продуктов, заполнен — локальное. Работая с продуктом, видим общее
     и его локальное; подробности — «Общее и локальное» в блоке 4.

   Размещения и ключи привязаны к продукту всегда, через них — статьи,
   ссылки и позиции.

---

## Блок 1. Площадки и метрики

### `products`
Продвигаемые продукты — отправная точка работы. Продукт заводит человек
в админке; с его появлением у каждой площадки появляется строка в
`product_sites`.

| Поле | Тип | Описание |
|---|---|---|
| id | bigserial PK | |
| name | text | «Convertio» |
| domain | text unique | `convertio.co` |
| is_active | bool | Clideo может быть неактивен, но история нужна |

### `sites`
Площадка-донор. Стабильные атрибуты, меняющиеся редко и не зависящие от
продукта. Решение по площадке — в `product_sites`.

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
| marks_as_ad | bool | «Пометка о рекламе статья» = Да: площадка по своим условиям помечает статьи как рекламу. Это оговорено заранее, поэтому **не стоп-фактор** и не условие предфильтра (ADR-034) |
| notes | text | свободный комментарий о площадке |
| content_selector | text | ручной CSS-селектор тела статьи, если эвристика краулера промахивается (E2-04) |
| is_deleted | bool | мягкое удаление |
| created_at / updated_at | timestamptz | |

Индексы: `domain`.

Признак «пишем ли мы статью» здесь **не хранится** — он вычисляется из
последних цен и порога продукта в `v_product_site_latest`.

### `product_sites`
Площадка в работе продукта: решение по ней для конкретного продукта.
Одна и та же площадка может подойти Convertio и не подойти продукту из
другой ниши.

| Поле | Тип | Описание |
|---|---|---|
| id | bigserial PK | |
| product_id | FK products | |
| site_id | FK sites | |
| status | enum | `new`, `auditing`, `approved`, `rejected`, `placed`, `blacklisted`. `placed` — есть опубликованное размещение **этого** продукта |
| reject_reason | text | почему отклонили для этого продукта |
| content_profile | jsonb | результат классификации P2 под этот продукт: основная тематика, fit_score, пояснение |
| imported_undecided | bool | площадка пришла из таблицы, решения по ней там не было. Снимается, когда решение появляется |
| created_at / updated_at | timestamptz | |

Уникальность: `(site_id, product_id)`. Индекс: `(product_id, status)`.

Строка есть для **каждой** пары продукт × площадка: новая площадка
получает строки под все продукты, новый продукт — под все площадки,
статус `new`. Создаёт их одна функция; повторный вызов ничего не
дублирует. Поэтому «площадки продукта со статусом X» — простой фильтр,
без проверки «а есть ли строка».

Общего чёрного списка («не работаем ни под каким продуктом») пока нет:
когда понадобится — колонка в `sites`, без переделки.

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

Расчётные значения — только в представлениях, не в колонках:
- `reference_total` = размещение + анонс (пустой анонс = 0), в
  `v_site_latest`. **С ним сравнивается ценовой ориентир продукта**
  (у Convertio 550 EUR). Написание не входит ни в одном случае — так
  сказано в промпте аудитора: ≤ порога пишет площадка и «это не
  считается нашим отдельным расходом», > порога или пусто — пишем сами;
- `we_write` = написание пусто или > порога, в `v_product_site_latest`;
- `expected_spend` = `reference_total` + написание, если площадка пишет
  сама. Сколько реально уйдёт площадке, в `v_product_site_latest`.

Порог написания — `writing_eur` из настройки `PRICE_REFERENCE` продукта
(у Convertio 50 EUR). Он зависит от продукта, поэтому `we_write` и
`expected_spend` живут в представлении продукта, а не площадки.

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
повторяется, старый вердикт должен быть виден. Аудит всегда идёт под
конкретный продукт: тематика, ценовой ориентир и стоп по удалённой
ссылке зависят от продукта. Стоп-факторы, которые от продукта не
зависят (nofollow), при аудите под каждый продукт находятся заново —
это проверка кодом, бесплатная.

| Поле | Тип | Описание |
|---|---|---|
| id | bigserial PK | |
| site_id | FK sites | |
| product_id | FK products | под какой продукт аудит |
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

### `site_lists` и `site_list_items`
Рабочий список площадок (ADR-033): одна загрузка таблицы или выгрузки
каталога, например «Сентябрь 2026». Работа идёт по выбранному списку,
старые списки остаются для истории.

`site_lists`:

| Поле | Тип | Описание |
|---|---|---|
| id | bigserial PK | |
| name | text unique | «Сентябрь 2026» |
| source | text | откуда пришёл список: имя файла, API |
| created_at | timestamptz | |

`site_list_items` — площадка в списке:

| Поле | Тип | Описание |
|---|---|---|
| id | bigserial PK | |
| list_id | FK site_lists | |
| site_id | FK sites | |
| first_seen | bool | площадки не было в базе до этого списка — замена колонке «Новая?» из таблицы |
| added_at | timestamptz | когда площадка попала в список |

Уникальность: `(list_id, site_id)`. Индекс: `site_id`.

Площадка в базе одна: в новый список она попадает со всей историей —
статусом по продуктам, аудитами, размещениями. Список общий для всех
продуктов: выгрузка каталога к продукту не привязана, статус в списке
показывается по выбранному продукту.

«Уже работали» для продукта — у площадки для него статус не `new`, есть
аудит или размещение этого продукта. Размещение другого продукта (статья
Clideo) сюда не входит — его показывают ссылкой рядом. Фильтр по списку
и по «уже работали / новые для нас» — в списке площадок (E9-01).

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
| skip_checks | bool | «не проверять»: без плановых проверок индексации и живости; кнопка работает (E2-03, `schema.sql` 1.6) |
| announce_on_homepage | bool | |
| clicks_from_homepage | smallint | сколько кликов до статьи |
| comment | text | что не так с полученной статьёй |
| run_id | uuid | по умолчанию из текущей цепочки (ADR-031): импорт или задача проставляют сами, ручной ввод — пусто |
| created_at / updated_at | timestamptz | |

Индексы: `(site_id, product_id)`, `status`, `published_at`.

### `placement_links`
Конкретная ссылка внутри статьи.

| Поле | Тип | Описание |
|---|---|---|
| id | bigserial PK | |
| placement_id | FK placements | |
| keyword_id | FK keywords, nullable | null для безанкорки; ключ того же продукта, что и размещение (проверяет форма) |
| anchor | text | текст ссылки как в задании |
| target_url | text | |
| anchor_type | enum | `exact`, `diluted` — ключ; `branded`, `url`, `generic` — безанкорка (бренд, голый адрес, нейтральное «here») |
| rel | text | атрибут `rel` как на странице (`nofollow sponsored`…); `NULL` — атрибута нет, ссылка dofollow. Заполняет краулер |
| char_offset | int | знаков до анкора с пробелами, как «Статистика» Word (ADR-032); по нему правила `LINK_POSITION_FIRST` и `LINK_POSITION_SECOND` |
| char_offset_no_spaces | int | то же без пробелов — для сверки с Word |
| context_sentence | text | предложение вокруг ссылки — для критика и разбора интеграции |
| link_index | smallint | первая ссылка, вторая… |
| extraction_test_passed | bool | пройден ли тест на извлечение |
| is_alive | bool | |
| last_checked_at | timestamptz | |
| first_seen_at / lost_at | timestamptz | когда появилась, когда пропала |

Индексы: `placement_id`, `keyword_id`, `(is_alive, last_checked_at)`.

**Позиция ссылки** (ADR-032) — сколько знаков стоит перед первым
символом анкора, если текст статьи с самого начала вставить в Word.
Заголовок входит; меню, подвал и сайдбар — нет. Теги и разметка
Markdown не считаются, границы абзацев, заголовков, пунктов списка и
переносы строк — тоже (Word не считает знаки абзаца). Пробелы — как
их показывает браузер: несколько подряд в исходнике — один. Пример:
заголовок «How to convert video», абзац «Converting is easy.», абзац
«Use mp4 to mp3 …» — 43 знака с пробелами, 37 без.

**`lost_at` пишется один раз** — при первом подтверждении пропажи.
Повторные проверки его не перезаписывают, вернувшаяся ссылка не
стирает: площадка её удаляла, на это смотрит стоп `STOP_LINK_REMOVED`.

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
| tool | text | раздел сайта продукта. У Convertio: `Main`, `Video`, `Audio`, `Image`, `Doc`. Список — локальная настройка продукта `TOOL_CATEGORIES`: разделы у каждого продукта свои |
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
| product_id | FK products, nullable | пусто — общий факт (про форматы, статистика), заполнен — про конкретный продукт |
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
| product_id | FK products, nullable | пусто — общая конструкция |
| pattern | text | шаблон с плейсхолдерами |
| example | text | реальный принятый пример |
| tool | text | под какой раздел продукта |
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
| code | text | `LINK_POSITION_FIRST`, `LINK_REL_SPAM`… |
| product_id | FK products, nullable | пусто — общее правило, заполнен — локальное |
| description | text | формулировка для промпта и для человека |
| severity | enum | `critical`, `medium`, `soft` |
| check_type | enum | `deterministic`, `llm`, `human` — последнее для того, что в v1 проверяет человек (заметность ссылки) |
| params | jsonb | пороги конкретной проверки: 1000 символов, 2 внутренние ссылки |
| is_active | bool | |

Смысл: пороги живут в базе, а не в коде. Поменялось требование — правка
строки, а не деплой.

Уникальность: `(code, product_id)` с `NULLS NOT DISTINCT` — двух общих
правил с одним кодом тоже быть не может (Postgres 15+).

### `domain_settings`
Пороги и списки, у которых нет критичности: зоны DR и трафика, ценовые
ориентиры, окно ротации фактов, порог близости статей, белый список
авторитетных доменов. Ключ — значение в JSONB, плюс `product_id`:
пусто — общее значение, заполнен — локальное. Уникальность —
`(key, product_id)` с `NULLS NOT DISTINCT`. Какие ключи общие, а какие
локальные, — `13-CONFIG.md` §2. Локальные значения человек вводит на
странице продукта в админке; переопределить для продукта можно любую
известную настройку, стёртое локальное значение удаляется — продукт
возвращается к общему (ADR-035).

Раньше они числились в `rules`, но правило проверки обязано иметь
критичность и тип проверки, а у порога «зелёная зона DR — от 50» их нет:
вставка в `rules` падала на ограничении `NOT NULL` (найдено 23.09.2026).

### Общее и локальное

У правил, настроек и знаний (`rules`, `domain_settings`,
`prompt_templates`, `facts`, `examples`, `anchor_patterns`,
`golden_set`) есть `product_id`. Пусто — общее для всех продуктов,
заполнен — локальное для продукта. Работая с продуктом, видим общее и
его локальное:

- где выбирается **одно значение** — настройка, правило, промпт задачи —
  локальное перекрывает общее с тем же ключом или кодом. Выключить общее
  правило для продукта — локальная строка с тем же кодом и
  `is_active = false`;
- где нужен **набор** — факты, примеры, конструкции анкоров, эталонные
  задания — общее и локальное объединяются.

Выбор делается в одном месте кода на каждую таблицу, не в каждом
потребителе.

### `examples`
Принятые и отклонённые образцы для few-shot.

| Поле | Тип | Описание |
|---|---|---|
| id | bigserial PK | |
| product_id | FK products, nullable | пусто — общий пример |
| kind | enum | `positive`, `negative` |
| scope | enum | `full_article`, `paragraph`, `anchor_sentence`, `intro` |
| body | text | |
| reason | text | почему принят или отклонён |
| tags | text[] | |
| language | text | |
| rule_code | text | какое правило нарушено (для negative). Без внешнего ключа на `rules`: код уникален только вместе с продуктом. Проверка — в приложении, как у `validation_results` |
| score | numeric(4,3) | вес |
| times_used / times_accepted | int | |

---

## Блок 5. Промпты, оценка, обучение

### `prompt_templates` и `prompt_variants`

`prompt_templates` — задача и имя: `product_id` (пусто — общий),
`task` (`generate_article`, `critic`, `classify_topic`…), `name`,
`is_active`, `created_at`. Текста в нём нет. Локальный шаблон задачи
перекрывает общий.

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

`golden_set` — фиксированные задания с эталоном; `product_id` пусто —
общее задание.
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

`result` проверки индексации (`placement`, `indexation`, E2-03, ADR-042):
`url` — проверенный адрес статьи, `queries` — запросы к выдаче,
`found_by` — `site` или `url` (каким запросом нашлась), `position` — место
в выдаче, `manual` — запущена кнопкой; при неудаче — `seen` (что было в
выдаче вместо статьи), `failing_days` и, один раз на серию неудач, `alert`.
`next_check_at` — полночь дня проверки плюс срок из `INDEXATION_SCHEDULE`.

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
| task_name | text | имя задачи Celery (`heartbeat`) или команды (`import_workbook`) |
| status | enum | `running`, `success`, `failed`; у задачи очереди `running` — до конца всех попыток |
| started_at / finished_at | timestamptz | от старта первой попытки до конца последней |
| duration_ms | int | |
| error | text | текст последней ошибки, без трассировки |
| payload | jsonb | параметры запуска. У задач очереди — `task_id` Celery, `args`, `kwargs`, `attempt` (номер последней попытки), `errors` прежних попыток, `redelivered` (возвращалась в очередь после остановки воркера), ADR-039 |
| run_id | uuid | |

Одна строка — один запуск задачи, а не попытка: повтор и возврат в
очередь после остановки воркера продолжают строку. Пишет базовый класс
задачи `QueueTask` (`config/queue.py`) и команда импорта таблицы.

### `api_usage`

| Поле | Тип | Описание |
|---|---|---|
| id | bigserial PK | |
| provider | text | `serper`, `ahrefs`, `voyage` — **без LLM**: их расход живёт в `llm_calls`, иначе посчитается дважды |
| endpoint | text | |
| units | int | юниты, запросы или кредиты провайдера |
| cost_cents | numeric(14,4) | центы с долями: запрос к выдаче стоит десятую долю цента (ADR-040) |
| currency | char(3) | `USD` по умолчанию; размещения — в EUR, поэтому суммы по валютам не складываются |
| run_id | uuid | |
| created_at | timestamptz | |

---

## Отчётные представления

Для дашбордов и аналитики — обычные SQL-представления, а не ORM:

- `v_keyword_coverage` — ключ продукта, последняя позиция (US), размещено, в ожидании;
- `v_site_funnel` — площадки по продуктам и статусам, отдельно
  импортированные без решения;
- `v_monthly_spend` — расходы по месяцам и валютам: API, LLM по моделям, размещения;
- `v_article_funnel` — статьи системы по месяцам: всего → с первой попытки →
  приняты → приняты без правок человека;
- `v_overdue_checks` — сущности, у которых срок **последней** проверки прошёл;
- `v_link_health` — живость ссылок по возрасту размещения;
- `v_site_latest` — площадка «на сегодня», только факты: последние
  метрики, цены, серость и `reference_total`. От продукта не зависит;
- `v_product_site_latest` — площадка в работе продукта: строка
  `product_sites`, данные из `v_site_latest`, `we_write` и
  `expected_spend` по порогу продукта, последний аудит под этот
  продукт, число его опубликованных размещений и список других наших
  продуктов, уже размещённых на площадке. На нём стоят список площадок
  продукта (E9-01), карточка и предфильтр.

Все восемь есть в `schema.sql`; семь проверены на тестовых данных
23.09.2026, `v_product_site_latest` и новый `v_site_funnel` —
27.09.2026. В базе с E1-05 — семь: `v_article_funnel`
создаётся вместе с таблицами статей, которых пока нет (`v_overdue_checks`
— с E1-03).

---

## Срез для convertio-ai-editor

**Запланировано, в `schema.sql` пока нет** — появится миграцией в
E10-07. Решение и контракт — ADR-027.

`ai_editor_sites` — view только для чтения из convertio-ai-editor.
Строится поверх `v_product_site_latest` по продукту Convertio,
производные признаки заново не считает. Роль Postgres `ai_editor_ro` имеет `SELECT` только на это view.

Поля — контракт: тело view меняется вместе с нашей схемой, имена и
смысл полей — только по согласованию с ai-editor.

Предварительный состав, уточняется в E10-07:

| Поле | Откуда |
|---|---|
| domain | `sites.domain` |
| topics, site_type | `sites` |
| language | `sites.language` |
| top_geo | последние метрики через `v_site_latest` |
| article_examples | примеры статей площадки из досье E4-02 (`site_audits.dossier`) |
| requirements | требования площадки: объём, длина заголовка, число ссылок, что не принимают — из досье E4-02 и `sites.links_allowed` |
| anchors | анкоры и слоты с целевыми URL (E3-02, `placement_links`) |
| anchor_positions | последняя позиция ключа каждого анкора в поиске с датой замера: `position`, `country`, `checked_at` из `keyword_positions`. По ней ai-editor выбирает тему под анкор в зоне 10–20 |

Какие площадки попадают в срез (все или только `approved`/`placed`) —
решается в E10-07.
