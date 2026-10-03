-- ============================================================
-- Система автоматизации линкбилдинга — схема PostgreSQL 16
-- Версия 1.11 от 03.10.2026 — история смены статусов площадки и размещения (ADR-049)
-- Версия 1.10 от 02.10.2026 — статусы площадки: просмотрено, заявка отправлена, отбрасываю, отказала площадка (ADR-047)
-- Версия 1.9 от 02.10.2026 — выгрузки Ahrefs Batch Analysis, трафик по странам (ADR-045)
-- Версия 1.8 от 01.10.2026 — загрузка файлов продавцов и каталога Collaborator (ADR-044)
-- Версия 1.7 от 01.10.2026 — продавцы, рабочая цена площадки, заметки, курсы валют (ADR-043)
-- Версия 1.6 от 01.10.2026 — размещение можно убрать из плановых проверок (E2-03)
-- Версия 1.5 от 30.09.2026 — стоимость API в центах с долями (ADR-040)
-- Версия 1.4 от 27.09.2026 — рабочие списки площадок (ADR-033)
-- Версия 1.3 от 27.09.2026 — позиция ссылки в двух вариантах, как в Word (ADR-032)
-- Версия 1.2 от 27.09.2026 — несколько продуктов (ADR-030)
-- (проверена применением на PostgreSQL 16: 39 таблиц и заглушка auth_user, 10 представлений, 3 функции, 4 триггера)
--
-- Это опорный DDL. При работе через Django миграции генерируются
-- из моделей, но схема должна соответствовать этому файлу. Известные
-- расхождения, которые даёт Django, перечислены в ADR-029.
-- Перед применением миграции всегда смотреть sqlmigrate.
--
-- Продукты (ADR-030): площадка хранит только факты о себе, решение по ней
-- своё для каждого продукта (product_sites). У правил, настроек и знаний
-- product_id пусто — общее для всех продуктов, заполнен — локальное.
-- ============================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Пользователи — таблица Django (django.contrib.auth), её создаёт Django. Здесь
-- заглушка: только колонка, на которую ссылаются автор заметки и сотрудник у
-- размещения, чтобы схема применялась сама по себе (ADR-029, уточнение 01.10.2026).
CREATE TABLE auth_user (
    id  integer PRIMARY KEY
);

-- ---------- Перечисления ----------

-- Порядок — как в окне статуса: путь в работу, отказы, аудит; по нему сортирует
-- колонка «статус». Как статус движется сам — apps/sites/statuses.py (ADR-047).
CREATE TYPE site_status AS ENUM
    ('new','viewed','approved','ordered','placed','discarded','declined','blacklisted','auditing');
CREATE TYPE metric_source AS ENUM
    ('ahrefs_api','serp_api','manual','csv_import','collaborator_api','ahrefs_batch');
CREATE TYPE audit_verdict AS ENUM ('yes','no','borderline');
CREATE TYPE audit_author AS ENUM ('human','llm','system');
CREATE TYPE placement_status AS ENUM
    ('planned','ordered','writing','review','published','rejected','cancelled');
-- exact — точный ключ; diluted — ключ внутри фразы; branded, url, generic — безанкорка
-- (бренд, голый URL, нейтральное «here»), как во вкладке «Распределение безанкорки».
CREATE TYPE anchor_type AS ENUM ('exact','diluted','branded','url','generic');
CREATE TYPE article_origin AS ENUM ('platform','copywriter','system');
CREATE TYPE article_status AS ENUM
    ('draft','validating','revising','needs_human','accepted','rejected');
CREATE TYPE fact_layer AS ENUM ('product','domain','stat','comparison');
CREATE TYPE fact_status AS ENUM ('active','stale','retired','pending_review');
CREATE TYPE rule_severity AS ENUM ('critical','medium','soft');
CREATE TYPE rule_check_type AS ENUM ('deterministic','llm','human');
CREATE TYPE example_kind AS ENUM ('positive','negative');
CREATE TYPE example_scope AS ENUM
    ('full_article','paragraph','anchor_sentence','intro');
CREATE TYPE check_status AS ENUM ('ok','failed','warning','error');
CREATE TYPE performer AS ENUM ('system','human');
CREATE TYPE llm_status AS ENUM ('ok','error','invalid_json','timeout');
CREATE TYPE task_status AS ENUM ('running','success','failed');
CREATE TYPE placement_type AS ENUM ('guest_post','link_insertion');
CREATE TYPE review_verdict AS ENUM ('accepted','needs_revision','rejected');
-- Загрузка файла (ADR-044): что за файл, где он в работе, вкладка разбора.
CREATE TYPE upload_kind AS ENUM ('price_list','collaborator_catalog','ahrefs_batch');
CREATE TYPE upload_status AS ENUM ('new','checking','checked','writing','done','failed');
CREATE TYPE review_group AS ENUM
    ('cheaper','changed','new','rejected','pricier','other_service','same');
-- Откуда смена статуса (ADR-049): панель и полная форма админки, система по
-- размещению (ADR-047), импорт таблицы, разбор загрузки, миграция. Пусто — код
-- не отметил, откуда.
CREATE TYPE status_source AS ENUM
    ('panel','form','placement','import','upload','migration');

-- ---------- Блок 1. Площадки ----------

CREATE TABLE products (
    id          bigserial PRIMARY KEY,
    name        text NOT NULL,
    domain      text NOT NULL UNIQUE,
    is_active   boolean NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Продавец площадок: перекупщик со своим прайсом или каталог (ADR-041, ADR-043).
-- Collaborator — тоже продавец, ровно один с is_collaborator.
CREATE TABLE sellers (
    id               bigserial PRIMARY KEY,
    name             text NOT NULL,
    contacts         text,
    notes            text,
    currency         char(3) NOT NULL DEFAULT 'EUR',    -- валюта прайсов по умолчанию
    is_collaborator  boolean NOT NULL DEFAULT false,
    metrics_trusted  boolean NOT NULL DEFAULT false,    -- его DR и трафик — как наши замеры
    column_map       jsonb,                             -- разметка колонок прайса: заголовок → поле (ADR-044)
    created_at       timestamptz NOT NULL DEFAULT now()
);
-- Имя без учёта регистра: «Athena Smith» и «Athena smith» — один продавец.
CREATE UNIQUE INDEX sellers_name_key ON sellers (lower(name));
CREATE UNIQUE INDEX sellers_collaborator_key ON sellers (is_collaborator) WHERE is_collaborator;

-- Площадка сама по себе: факты, которые не зависят от продукта.
-- Статус, причина отказа и соответствие тематике — в product_sites.
CREATE TABLE sites (
    id                  bigserial PRIMARY KEY,
    domain              text NOT NULL UNIQUE,
    collaborator_url    text,
    source              text,
    language            text,               -- основной язык (первый в списке Collaborator)
    languages           text[],             -- все языки сайта как в карточке
    topics              text[],             -- категории Collaborator, мультизначение
    declared_topics     text[],             -- «Особые тематики»: что площадка готова принимать
    site_type           text,
    links_allowed       smallint,
    link_type           text,               -- заявленный тип ссылки: dofollow / nofollow
    marks_as_ad         boolean,            -- «Пометка о рекламе статья» = Да
    content_selector    text,               -- ручной CSS-селектор тела статьи для краулера
    price_id            bigint,             -- рабочая цена: предложение из site_prices (ADR-043)
    is_deleted          boolean NOT NULL DEFAULT false,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

-- Площадка в работе продукта. Строка есть для каждой пары продукт × площадка:
-- новая площадка получает строки под все продукты, новый продукт — под все
-- площадки, со статусом new.
CREATE TABLE product_sites (
    id                  bigserial PRIMARY KEY,
    product_id          bigint NOT NULL REFERENCES products(id),
    site_id             bigint NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    status              site_status NOT NULL DEFAULT 'new',
    reject_reason       text,
    content_profile     jsonb,              -- результат P2 под этот продукт: тематика, fit_score
    imported_undecided  boolean NOT NULL DEFAULT false,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (site_id, product_id)
);
CREATE INDEX idx_product_sites_status ON product_sites(product_id, status);

CREATE TABLE site_metrics (
    id              bigserial PRIMARY KEY,
    site_id         bigint NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    dr              smallint,
    organic_traffic integer,
    top_geo         text,
    top_geo_traffic integer,
    total_keywords  integer,
    source          metric_source NOT NULL DEFAULT 'manual',
    seller_id       bigint REFERENCES sellers(id),     -- замер со слов продавца; пусто — наш
    raw             jsonb,
    checked_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_metrics_site ON site_metrics(site_id, checked_at DESC);

-- Трафик и ключи площадки в одной стране — снимок (ADR-045). Пишет выгрузка Ahrefs
-- Batch Analysis под страну, позже — Ahrefs API (E2-07). Топ-регион выгрузки «все
-- страны» — в site_metrics: по нему трафик других стран не узнать. Замер всегда наш.
CREATE TABLE site_country_metrics (
    id              bigserial PRIMARY KEY,
    site_id         bigint NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    country         char(2) NOT NULL,                  -- код страны строчными, как у Ahrefs: us, gb
    organic_traffic integer,
    total_keywords  integer,
    source          metric_source NOT NULL DEFAULT 'manual',
    raw             jsonb,
    checked_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_country_metrics_site ON site_country_metrics(site_id, country, checked_at DESC);

-- Предложение продавца на дату: одна услуга — одна цена (ADR-043). Снимок: новая
-- цена — новая строка. Сравнивается только цена услуги; написание, анонс и серая
-- цена — справочно. reviewed_at пусто — по предложению ещё не решили.
CREATE TABLE site_prices (
    id               bigserial PRIMARY KEY,
    site_id          bigint NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    seller_id        bigint NOT NULL REFERENCES sellers(id),
    placement_type   placement_type NOT NULL DEFAULT 'guest_post',
    placement_cents  integer,                           -- цена услуги
    announce_cents   integer,
    writing_cents    integer,
    gray_cents       integer,                           -- серая цена: площадка принимает серые тематики
    currency         char(3) NOT NULL DEFAULT 'EUR',
    extra            jsonb,                             -- «прочие данные» строки файла: заголовок → значение
    source           metric_source NOT NULL DEFAULT 'manual',
    reviewed_at      timestamptz,
    checked_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_prices_site ON site_prices(site_id, seller_id, placement_type, checked_at DESC);
CREATE INDEX idx_prices_pending ON site_prices(site_id) WHERE reviewed_at IS NULL;
ALTER TABLE sites ADD FOREIGN KEY (price_id) REFERENCES site_prices(id);

CREATE TABLE gray_scans (
    id             bigserial PRIMARY KEY,
    site_id        bigint NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    total_indexed  integer,
    gray_hits      integer,
    ratio          numeric(5,2),
    breakdown      jsonb,
    sample_urls    jsonb,
    method         metric_source NOT NULL DEFAULT 'serp_api',
    checked_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_gray_site ON gray_scans(site_id, checked_at DESC);

CREATE TABLE site_audits (
    id          bigserial PRIMARY KEY,
    site_id     bigint NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    product_id  bigint NOT NULL REFERENCES products(id), -- аудит всегда под продукт
    verdict     audit_verdict NOT NULL,
    score       smallint CHECK (score BETWEEN 0 AND 100),
    blockers    jsonb,
    strengths   jsonb,
    weaknesses  jsonb,
    summary     text,
    missing_data jsonb,                    -- каких данных не хватило для вывода
    price_flag  jsonb,                     -- превышение ориентира: решение за человеком
    dossier     jsonb,                     -- снимок досье, на котором вынесен вердикт
    author      audit_author NOT NULL,     -- system — стоп-фактор кодом или импорт
    model       text,
    run_id      uuid,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_audits_site ON site_audits(site_id, product_id, created_at DESC);

-- Рабочий список площадок: одна загрузка таблицы или выгрузки каталога
-- (ADR-033). Площадка в базе одна, в списки она входит со всей историей.
-- Список общий для всех продуктов, статус в нём — по выбранному продукту.
CREATE TABLE site_lists (
    id          bigserial PRIMARY KEY,
    name        text NOT NULL UNIQUE,       -- «Сентябрь 2026»
    source      text,                       -- откуда: имя файла, API
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE site_list_items (
    id          bigserial PRIMARY KEY,
    list_id     bigint NOT NULL REFERENCES site_lists(id) ON DELETE CASCADE,
    site_id     bigint NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    first_seen  boolean NOT NULL DEFAULT false, -- площадки не было в базе до этого списка
    added_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (list_id, site_id)
);
CREATE INDEX idx_list_items_site ON site_list_items(site_id);

-- Заметки о площадке — история: не правятся и не удаляются (ADR-043). Автор —
-- пользователь, или источник — файл («таблица линкбилдинга»). Продукт — если
-- заметка про решение под него.
CREATE TABLE site_notes (
    id          bigserial PRIMARY KEY,
    site_id     bigint NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    product_id  bigint REFERENCES products(id),
    body        text NOT NULL,
    author_id   integer REFERENCES auth_user(id),
    source      text,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_site_notes_site ON site_notes(site_id, created_at DESC);

-- Курс ЕЦБ: сколько единиц валюты за 1 евро на дату. Для сравнения цен в разных
-- валютах; деньги в разных валютах не складываются (ADR-043).
CREATE TABLE exchange_rates (
    id          bigserial PRIMARY KEY,
    currency    char(3) NOT NULL,
    rate_date   date NOT NULL,
    rate        numeric(14,6) NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (currency, rate_date)
);

-- Загрузка файла продавца или каталога Collaborator (ADR-044): файл, разметка
-- колонок, сводка до записи, итог записи. Результат — рабочий список. Выгрузка
-- Ahrefs Batch Analysis (ADR-045) — без продавца и рабочего списка, со страной.
CREATE TABLE uploads (
    id            bigserial PRIMARY KEY,
    kind          upload_kind NOT NULL,
    seller_id     bigint REFERENCES sellers(id),  -- пусто только у выгрузки Ahrefs: замер наш
    prices_date   date NOT NULL,                -- дата цен (у Ahrefs — замера): на неё пишутся снимки
    country       char(2),                      -- страна выгрузки Ahrefs; пусто — все страны
    file_name     text NOT NULL,                -- имя файла у пользователя
    file_path     text NOT NULL,                -- путь от папки загрузок, её видит воркер
    file_sha256   char(64) NOT NULL,            -- тот же файл повторно — та же загрузка
    header_row    integer,                      -- строка заголовков, с 1
    columns       jsonb,                        -- колонки файла: заголовок, примеры, догадка
    mapping       jsonb,                        -- разметка этой загрузки: заголовок → поле
    currency      char(3),                      -- валюта цен файла
    status        upload_status NOT NULL DEFAULT 'new',
    summary       jsonb,                        -- сводка до записи
    result        jsonb,                        -- итог записи: счётчики, дубли, ошибки
    error         text,
    site_list_id  bigint REFERENCES site_lists(id),
    run_id        uuid,
    author_id     integer REFERENCES auth_user(id),   -- кто загрузил
    created_at    timestamptz NOT NULL DEFAULT now(),
    written_at    timestamptz,
    CONSTRAINT uploads_seller_check CHECK (seller_id IS NOT NULL OR kind = 'ahrefs_batch')
);

-- Строка разбора загрузки: предложение из файла и с чем его сравнили. Вкладка —
-- по положению на момент записи; решено ли — по reviewed_at предложения.
CREATE TABLE upload_items (
    id              bigserial PRIMARY KEY,
    upload_id       bigint NOT NULL REFERENCES uploads(id) ON DELETE CASCADE,
    site_id         bigint NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    price_id        bigint NOT NULL REFERENCES site_prices(id),  -- предложение из файла
    ref_price_id    bigint REFERENCES site_prices(id),           -- рабочая цена до загрузки
    review_group    review_group NOT NULL,
    needs_decision  boolean NOT NULL DEFAULT false,  -- при записи ждало решения человека
    auto_applied    boolean NOT NULL DEFAULT false,  -- рабочей стало само: первая цена или тот же продавец
    site_created    boolean NOT NULL DEFAULT false,  -- площадку создала эта загрузка
    line            integer,                         -- строка файла
    source_value    text                             -- адрес из файла, если там не домен
);
CREATE INDEX idx_upload_items_upload ON upload_items(upload_id, review_group);

-- ---------- Блок 2. Размещения ----------

CREATE TABLE placements (
    id                     bigserial PRIMARY KEY,
    site_id                bigint NOT NULL REFERENCES sites(id),
    product_id             bigint NOT NULL REFERENCES products(id),
    article_url            text,
    status                 placement_status NOT NULL DEFAULT 'planned',
    collaborator_order_id  text,
    placement_type         placement_type,
    ad_label_requested     boolean NOT NULL DEFAULT false, -- пометку «реклама» заказали мы
    ordered_at             timestamptz,
    published_at           timestamptz,
    price_paid_cents       integer,
    currency               char(3) DEFAULT 'EUR',
    is_indexed             boolean,
    indexed_checked_at     timestamptz,
    skip_checks            boolean NOT NULL DEFAULT false, -- «не проверять»: без плановых проверок
    announce_on_homepage   boolean,
    clicks_from_homepage   smallint,
    comment                text,
    seller_id              bigint REFERENCES sellers(id),    -- через кого куплено
    employee_id            integer REFERENCES auth_user(id), -- кто из сотрудников вёл
    run_id                 uuid,
    created_at             timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_placements_site ON placements(site_id, product_id);
CREATE INDEX idx_placements_status ON placements(status);
CREATE INDEX idx_placements_pub ON placements(published_at DESC);

-- История смены статусов (ADR-049): строку пишет триггер при каждой смене
-- статуса, «кто и откуда» — отметка кода в переменных транзакции seo.change_*
-- (config/changes.py). Правка без смены статуса строки не даёт.
-- Не правятся и не удаляются.

-- Статус площадки у продукта. from_status пусто — строка создана сразу не «Новой».
-- placement_id — размещение, по которому система сменила статус (ADR-047).
CREATE TABLE site_status_changes (
    id               bigserial PRIMARY KEY,
    product_site_id  bigint NOT NULL REFERENCES product_sites(id),
    from_status      site_status,
    to_status        site_status NOT NULL,
    source           status_source,
    actor_id         integer REFERENCES auth_user(id),
    placement_id     bigint REFERENCES placements(id),
    run_id           uuid,
    changed_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_site_status_changes ON site_status_changes(product_site_id, changed_at DESC);

-- Статус размещения. from_status пусто — размещение создано.
CREATE TABLE placement_status_changes (
    id            bigserial PRIMARY KEY,
    placement_id  bigint NOT NULL REFERENCES placements(id),
    from_status   placement_status,
    to_status     placement_status NOT NULL,
    source        status_source,
    actor_id      integer REFERENCES auth_user(id),
    run_id        uuid,
    changed_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_placement_status_changes ON placement_status_changes(placement_id, changed_at DESC);

CREATE TABLE keywords (
    id           bigserial PRIMARY KEY,
    product_id   bigint NOT NULL REFERENCES products(id),
    keyword      text NOT NULL,
    target_url   text NOT NULL,
    volume       integer,
    global_volume integer,
    tool         text,                     -- раздел сайта продукта; список — настройка TOOL_CATEGORIES
    page_type    text,                     -- колонка Type: «Главная», «Video (xxx-yyy)»…
    anchor_type  anchor_type,
    is_active    boolean NOT NULL DEFAULT true,
    UNIQUE (product_id, keyword)
);
CREATE INDEX idx_keywords_tool ON keywords(tool) WHERE is_active;

CREATE TABLE placement_links (
    id                      bigserial PRIMARY KEY,
    placement_id            bigint NOT NULL REFERENCES placements(id) ON DELETE CASCADE,
    keyword_id              bigint REFERENCES keywords(id),
    anchor                  text NOT NULL,
    target_url              text NOT NULL,
    anchor_type             anchor_type,
    rel                     text,          -- атрибут rel как на странице; NULL — нет атрибута
    char_offset             integer,       -- знаков до анкора с пробелами, как в Word (ADR-032)
    char_offset_no_spaces   integer,       -- то же без пробелов
    context_sentence        text,          -- предложение вокруг ссылки, для критика
    link_index              smallint,
    extraction_test_passed  boolean,
    is_alive                boolean,
    last_checked_at         timestamptz,
    first_seen_at           timestamptz,
    lost_at                 timestamptz
);
CREATE INDEX idx_links_placement ON placement_links(placement_id);
CREATE INDEX idx_links_keyword ON placement_links(keyword_id);
CREATE INDEX idx_links_health ON placement_links(is_alive, last_checked_at);

CREATE TABLE keyword_positions (
    id          bigserial PRIMARY KEY,
    keyword_id  bigint NOT NULL REFERENCES keywords(id) ON DELETE CASCADE,
    position    smallint,
    country     char(2) NOT NULL DEFAULT 'US',
    source      metric_source NOT NULL DEFAULT 'ahrefs_api',
    checked_at  date NOT NULL DEFAULT current_date,
    UNIQUE (keyword_id, country, checked_at)
);
CREATE INDEX idx_positions_kw ON keyword_positions(keyword_id, checked_at DESC);

-- ---------- Блок 3. Контент ----------
-- product_id в промптах, правилах, настройках и знаниях: пусто — общее для
-- всех продуктов, заполнен — локальное (ADR-030). Там, где выбирается одно
-- значение (промпт задачи, правило, настройка), локальное перекрывает общее
-- с тем же кодом; там, где набор (факты, примеры, конструкции), — объединение.

CREATE TABLE prompt_templates (
    id          bigserial PRIMARY KEY,
    product_id  bigint REFERENCES products(id),
    task        text NOT NULL,
    name        text NOT NULL,
    is_active   boolean NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE prompt_variants (
    id              bigserial PRIMARY KEY,
    template_id     bigint NOT NULL REFERENCES prompt_templates(id),
    label           text NOT NULL,
    body            text NOT NULL,
    times_used      integer NOT NULL DEFAULT 0,
    times_accepted  integer NOT NULL DEFAULT 0,
    is_active       boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE articles (
    id                  bigserial PRIMARY KEY,
    placement_id        bigint REFERENCES placements(id) ON DELETE CASCADE,
    origin              article_origin NOT NULL,
    status              article_status NOT NULL DEFAULT 'draft',
    current_version_id  bigint,
    brief               jsonb,
    plan                jsonb,              -- структура статьи из E6-03
    angle               text,               -- угол подачи: how-to, comparison, ...
    run_id              uuid,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE article_versions (
    id                  bigserial PRIMARY KEY,
    article_id          bigint NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    version             smallint NOT NULL,
    body                text NOT NULL,
    word_count          integer,
    prompt_variant_id   bigint REFERENCES prompt_variants(id),
    facts_used          jsonb,
    examples_used       jsonb,              -- id примеров, попавших в промпт (для E8-02)
    patterns_used       jsonb,              -- id анкорных конструкций
    embedding           real[],             -- для E6-06; при росте — pgvector
    similarity_max      numeric(4,3),
    similar_to_version_id bigint REFERENCES article_versions(id), -- ближайший «сосед»
    is_human_edit       boolean NOT NULL DEFAULT false,
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (article_id, version)
);

ALTER TABLE articles
    ADD CONSTRAINT fk_current_version
    FOREIGN KEY (current_version_id) REFERENCES article_versions(id);

CREATE TABLE article_reviews (
    id                  bigserial PRIMARY KEY,
    article_version_id  bigint NOT NULL REFERENCES article_versions(id) ON DELETE CASCADE,
    verdict             review_verdict NOT NULL,
    reason_codes        text[],             -- коды правил из rules
    comment             text,
    reviewer            text NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_reviews_version ON article_reviews(article_version_id);

CREATE TABLE facts (
    id            bigserial PRIMARY KEY,
    product_id    bigint REFERENCES products(id),
    layer         fact_layer NOT NULL,
    statement     text NOT NULL,
    detail        text,
    tags          text[],
    source_url    text,
    source_title  text,
    evidence      text,                    -- дословный фрагмент-подтверждение (P3)
    confidence    numeric(3,2),
    verified_at   date,
    expires_at    date,
    status        fact_status NOT NULL DEFAULT 'pending_review',
    language      text NOT NULL DEFAULT 'en',
    supersedes_id bigint REFERENCES facts(id), -- предыдущая версия факта (E5-06)
    embedding     real[],
    usage_count   integer NOT NULL DEFAULT 0,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_facts_tags ON facts USING gin(tags);
CREATE INDEX idx_facts_expiry ON facts(expires_at) WHERE status = 'active';

CREATE TABLE fact_usages (
    id                  bigserial PRIMARY KEY,
    fact_id             bigint NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
    article_version_id  bigint NOT NULL REFERENCES article_versions(id) ON DELETE CASCADE,
    site_id             bigint REFERENCES sites(id),
    used_at             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_fact_usage ON fact_usages(fact_id, used_at DESC);

CREATE TABLE anchor_patterns (
    id                      bigserial PRIMARY KEY,
    product_id              bigint REFERENCES products(id),
    pattern                 text NOT NULL,
    example                 text,
    tool                    text,
    anchor_type             anchor_type,
    extraction_test_passed  boolean NOT NULL DEFAULT true,
    language                text NOT NULL DEFAULT 'en',
    score                   numeric(4,3) NOT NULL DEFAULT 0.5,
    times_used              integer NOT NULL DEFAULT 0,
    times_accepted          integer NOT NULL DEFAULT 0,
    is_active               boolean NOT NULL DEFAULT true
);

-- Локальное правило с тем же кодом перекрывает общее: другие params или
-- is_active = false — правило выключено для продукта.
-- NULLS NOT DISTINCT: двух общих правил с одним кодом тоже быть не может.
CREATE TABLE rules (
    id           bigserial PRIMARY KEY,
    code         text NOT NULL,
    product_id   bigint REFERENCES products(id),
    description  text NOT NULL,
    severity     rule_severity NOT NULL,
    check_type   rule_check_type NOT NULL,
    params       jsonb,
    is_active    boolean NOT NULL DEFAULT true,
    UNIQUE NULLS NOT DISTINCT (code, product_id)
);

-- Пороги аудита, контентные настройки и списки (белый список доменов).
-- У них нет критичности, поэтому они не живут в rules.
-- Локальное значение продукта перекрывает общее с тем же ключом.
CREATE TABLE domain_settings (
    id           bigserial PRIMARY KEY,
    key          text NOT NULL,
    product_id   bigint REFERENCES products(id),
    value        jsonb NOT NULL,
    description  text,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE NULLS NOT DISTINCT (key, product_id)
);

CREATE TABLE examples (
    id              bigserial PRIMARY KEY,
    product_id      bigint REFERENCES products(id),
    kind            example_kind NOT NULL,
    scope           example_scope NOT NULL,
    body            text NOT NULL,
    reason          text,
    tags            text[],
    language        text NOT NULL DEFAULT 'en',
    rule_code       text,                  -- код из rules; без внешнего ключа: код уникален только вместе с продуктом
    score           numeric(4,3) NOT NULL DEFAULT 0.5,
    times_used      integer NOT NULL DEFAULT 0,
    times_accepted  integer NOT NULL DEFAULT 0,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_examples_tags ON examples USING gin(tags);

CREATE TABLE validation_results (
    id                  bigserial PRIMARY KEY,
    article_version_id  bigint NOT NULL REFERENCES article_versions(id) ON DELETE CASCADE,
    rule_code           text NOT NULL,
    passed              boolean NOT NULL,
    severity            rule_severity NOT NULL,
    details             jsonb,
    checked_by          rule_check_type NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_validation_version ON validation_results(article_version_id);

CREATE TABLE golden_set (
    id          bigserial PRIMARY KEY,
    product_id  bigint REFERENCES products(id),
    name        text NOT NULL,
    brief       jsonb NOT NULL,
    expectation jsonb NOT NULL,
    is_active   boolean NOT NULL DEFAULT true
);

CREATE TABLE eval_runs (
    id            bigserial PRIMARY KEY,
    changed_what  text,
    pass_rate     numeric(5,2),
    details       jsonb,
    run_id        uuid,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- ---------- Блок 4. Наблюдаемость ----------
-- Деньги: расходы LLM пишутся ТОЛЬКО в llm_calls, остальные API — ТОЛЬКО в
-- api_usage. Иначе себестоимость статьи посчитается дважды.

CREATE TABLE checks (
    id            bigserial PRIMARY KEY,
    entity_type   text NOT NULL,
    entity_id     bigint NOT NULL,
    check_type    text NOT NULL,
    status        check_status NOT NULL,
    result        jsonb,
    performed_by  performer NOT NULL DEFAULT 'system',
    run_id        uuid,
    checked_at    timestamptz NOT NULL DEFAULT now(),
    next_check_at timestamptz
);
CREATE INDEX idx_checks_entity ON checks(entity_type, entity_id);
CREATE INDEX idx_checks_due ON checks(check_type, next_check_at);

CREATE TABLE llm_calls (
    id                 bigserial PRIMARY KEY,
    task               text NOT NULL,
    model              text NOT NULL,
    prompt_variant_id  bigint REFERENCES prompt_variants(id),
    input_tokens       integer,
    output_tokens      integer,
    cost_cents         integer,
    currency           char(3) NOT NULL DEFAULT 'USD',
    duration_ms        integer,
    status             llm_status NOT NULL,
    response           jsonb,
    entity_type        text,
    entity_id          bigint,
    run_id             uuid,
    created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_llm_task ON llm_calls(task, created_at DESC);
CREATE INDEX idx_llm_run ON llm_calls(run_id);

CREATE TABLE task_runs (
    id           bigserial PRIMARY KEY,
    task_name    text NOT NULL,
    status       task_status NOT NULL,
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    duration_ms  integer,
    error        text,
    payload      jsonb,
    run_id       uuid
);
CREATE INDEX idx_taskruns_name ON task_runs(task_name, started_at DESC);

-- Стоимость — центы с долями: запрос к выдаче стоит десятую долю цента,
-- в целых центах он был бы нулём (ADR-040). numeric точен, это не float.
CREATE TABLE api_usage (
    id          bigserial PRIMARY KEY,
    provider    text NOT NULL,
    endpoint    text,
    units       integer,
    cost_cents  numeric(14,4),
    currency    char(3) NOT NULL DEFAULT 'USD',
    run_id      uuid,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_usage_provider ON api_usage(provider, created_at DESC);

-- ---------- Функции ----------

-- Курс для пересчёта в евро: сколько единиц валюты за 1 евро, последний известный
-- (ADR-043). Евро — 1; курса нет — пусто, и сумма в евро тоже пуста.
CREATE FUNCTION eur_rate(cur char(3)) RETURNS numeric
LANGUAGE sql STABLE AS $$
    SELECT CASE WHEN cur = 'EUR' THEN 1
                ELSE (SELECT r.rate FROM exchange_rates r WHERE r.currency = cur
                      ORDER BY r.rate_date DESC LIMIT 1) END
$$;

-- Строка истории статуса (ADR-049). Отметку «откуда, кто, по какому размещению,
-- run_id» код ставит set_config(…, true) — до конца транзакции; не поставил —
-- пусто. current_setting(…, true) — пусто, а не ошибка, если отметки не было.
CREATE FUNCTION log_site_status_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO site_status_changes
        (product_site_id, from_status, to_status, source, actor_id, placement_id, run_id)
    VALUES (
        NEW.id,
        CASE WHEN TG_OP = 'UPDATE' THEN OLD.status END,
        NEW.status,
        NULLIF(current_setting('seo.change_source', true), '')::status_source,
        NULLIF(current_setting('seo.change_actor', true), '')::integer,
        NULLIF(current_setting('seo.change_placement', true), '')::bigint,
        NULLIF(current_setting('seo.change_run_id', true), '')::uuid
    );
    RETURN NULL;
END
$$;

CREATE FUNCTION log_placement_status_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO placement_status_changes
        (placement_id, from_status, to_status, source, actor_id, run_id)
    VALUES (
        NEW.id,
        CASE WHEN TG_OP = 'UPDATE' THEN OLD.status END,
        NEW.status,
        NULLIF(current_setting('seo.change_source', true), '')::status_source,
        NULLIF(current_setting('seo.change_actor', true), '')::integer,
        NULLIF(current_setting('seo.change_run_id', true), '')::uuid
    );
    RETURN NULL;
END
$$;

-- ---------- Триггеры ----------

-- Смена статуса — строка истории; тот же статус ещё раз — нет (ADR-049). Строки
-- «Новая», которые создаёт система для каждой пары продукт × площадка, — не смена.
CREATE TRIGGER product_sites_status_insert AFTER INSERT ON product_sites
    FOR EACH ROW WHEN (NEW.status <> 'new') EXECUTE FUNCTION log_site_status_change();
CREATE TRIGGER product_sites_status_update AFTER UPDATE OF status ON product_sites
    FOR EACH ROW WHEN (OLD.status IS DISTINCT FROM NEW.status)
    EXECUTE FUNCTION log_site_status_change();
CREATE TRIGGER placements_status_insert AFTER INSERT ON placements
    FOR EACH ROW EXECUTE FUNCTION log_placement_status_change();
CREATE TRIGGER placements_status_update AFTER UPDATE OF status ON placements
    FOR EACH ROW WHEN (OLD.status IS DISTINCT FROM NEW.status)
    EXECUTE FUNCTION log_placement_status_change();

-- ---------- Представления ----------

CREATE VIEW v_keyword_coverage AS
SELECT
    k.id,
    k.product_id,
    k.keyword,
    k.tool,
    k.volume,
    k.target_url,
    (SELECT position FROM keyword_positions kp
      WHERE kp.keyword_id = k.id AND kp.country = 'US'
      ORDER BY checked_at DESC LIMIT 1) AS last_position,
    COUNT(pl.id) FILTER (WHERE p.status = 'published') AS links_placed,
    COUNT(pl.id) FILTER (WHERE p.status IN ('planned','ordered','writing','review')) AS links_waiting
FROM keywords k
LEFT JOIN placement_links pl ON pl.keyword_id = k.id
LEFT JOIN placements p ON p.id = pl.placement_id
WHERE k.is_active
GROUP BY k.id;

-- Срок берётся из ПОСЛЕДНЕЙ проверки. Минимум по всей истории давал вечные
-- «просрочки» по уже выполненным проверкам (найдено тестом 23.09.2026).
CREATE VIEW v_overdue_checks AS
SELECT *
FROM (
    SELECT DISTINCT ON (entity_type, entity_id, check_type)
           entity_type, entity_id, check_type, status,
           checked_at AS last_checked, next_check_at AS due_at
    FROM checks
    ORDER BY entity_type, entity_id, check_type, checked_at DESC
) latest
WHERE due_at < now();

CREATE VIEW v_link_health AS
SELECT
    s.domain,
    p.id AS placement_id,
    p.published_at,
    pl.anchor,
    pl.is_alive,
    pl.last_checked_at,
    now() - p.published_at AS age
FROM placement_links pl
JOIN placements p ON p.id = pl.placement_id
JOIN sites s ON s.id = p.site_id
WHERE p.status = 'published';

CREATE VIEW v_site_funnel AS
SELECT ps.product_id, ps.status, ps.imported_undecided, count(*) AS sites
FROM product_sites ps
JOIN sites s ON s.id = ps.site_id
WHERE NOT s.is_deleted
GROUP BY ps.product_id, ps.status, ps.imported_undecided;

-- Валюты не складываются: API и LLM — в долларах, размещения — в евро.
CREATE VIEW v_monthly_spend AS
SELECT date_trunc('month', created_at)::date AS month,
       provider AS item, currency, sum(cost_cents) AS cost_cents
FROM api_usage
GROUP BY 1, 2, 3
UNION ALL
SELECT date_trunc('month', created_at)::date, 'llm:' || model, currency, sum(cost_cents)
FROM llm_calls
GROUP BY 1, 2, 3
UNION ALL
SELECT date_trunc('month', published_at)::date, 'placements',
       coalesce(currency, 'EUR'), sum(price_paid_cents)
FROM placements
WHERE status = 'published' AND published_at IS NOT NULL
GROUP BY 1, 3;

-- «С первой попытки» = у версии 1 нет проваленных проверок critical/medium.
CREATE VIEW v_article_funnel AS
SELECT date_trunc('month', a.created_at)::date AS month,
       count(*) AS articles,
       count(*) FILTER (WHERE NOT EXISTS (
           SELECT 1 FROM article_versions v
           JOIN validation_results r ON r.article_version_id = v.id
           WHERE v.article_id = a.id AND v.version = 1
             AND NOT r.passed AND r.severity IN ('critical','medium'))
         AND EXISTS (SELECT 1 FROM article_versions v
                     WHERE v.article_id = a.id AND v.version = 1)
       ) AS first_pass,
       count(*) FILTER (WHERE a.status = 'accepted') AS accepted,
       count(*) FILTER (WHERE a.status = 'accepted' AND NOT EXISTS (
           SELECT 1 FROM article_versions v
           WHERE v.article_id = a.id AND v.is_human_edit)
       ) AS accepted_without_edits
FROM articles a
WHERE a.origin = 'system'
GROUP BY 1;

-- Текущие предложения площадки: последнее предложение каждого продавца за каждую
-- услугу; цена услуги — ещё и в евро по последнему курсу ЕЦБ, только для сравнения
-- и показа (ADR-043). Отсюда пометки в v_site_latest и список предложений на экранах.
CREATE VIEW v_site_offers AS
SELECT o.id, o.site_id, o.seller_id, sl.name AS seller, o.placement_type,
       o.placement_cents, o.currency,
       round(o.placement_cents / eur_rate(o.currency))::integer AS placement_eur_cents,
       o.announce_cents, o.writing_cents, o.gray_cents, o.extra,
       o.reviewed_at, o.checked_at
FROM (SELECT DISTINCT ON (x.site_id, x.seller_id, x.placement_type) *
      FROM site_prices x
      ORDER BY x.site_id, x.seller_id, x.placement_type, x.checked_at DESC, x.id DESC) o
JOIN sellers sl ON sl.id = o.seller_id;

-- Площадка «на сегодня»: метрики, рабочая цена, пометки разбора, серость, заметки.
-- Только то, что не зависит от продукта; статус и вердикт — в v_product_site_latest.
-- Метрики — последний доверенный замер (наш или от продавца с metrics_trusted), нет
-- такого — последний со слов продавца, metrics_trusted = false (ADR-043).
-- Топ-регион и его трафик — из последнего замера, где они есть, в том же порядке
-- доверия: замер каталога без гео их не стирает (ADR-045).
-- Цена — рабочая (sites.price_id). Евро — eur_rate(), только для сравнения и
-- показа. reference_total — то, что сравнивается с ценовым ориентиром продукта:
-- размещение + анонс, в евро; написание в него не входит никогда (промпт
-- аудитора). Нет рабочей цены или курса — пусто.
-- Пометки разбора — по цене услуги, без написания и анонса:
-- new_price — текущее предложение того же продавца за ту же услугу, если оно не
-- рабочее; cheaper — самое дешёвое текущее предложение другого продавца за ту же
-- услугу, если оно дешевле рабочей в евро; *_pending — по нему ещё не решили;
-- offers_pending — у площадки есть неразобранные предложения.
-- Заметки — одним подзапросом на все площадки, а не подзапросом на строку: на
-- 45 000 площадках каталога тот давал полный просмотр site_notes на каждую (E1-08).
CREATE VIEW v_site_latest AS
SELECT s.id, s.domain, s.language, s.topics, s.declared_topics,
       s.links_allowed, s.link_type, s.marks_as_ad,
       m.dr, m.organic_traffic, m.total_keywords,
       tg.top_geo, tg.top_geo_traffic, tg.checked_at AS top_geo_at, m.checked_at AS metrics_at,
       m.trusted AS metrics_trusted, ms.name AS metrics_seller,
       pr.id AS price_id, pr.seller_id AS price_seller_id, ps.name AS price_seller,
       pr.placement_type AS price_type,
       pr.placement_cents, pr.announce_cents, pr.writing_cents,
       pr.currency AS price_currency, pr.checked_at AS prices_at,
       round(pr.placement_cents / eur_rate(pr.currency))::integer AS placement_eur_cents,
       round(pr.writing_cents / eur_rate(pr.currency))::integer AS writing_eur_cents,
       round((pr.placement_cents + coalesce(pr.announce_cents, 0)) / eur_rate(pr.currency))::integer
         AS reference_total_cents,
       np.id AS new_price_id, np.placement_cents AS new_price_cents,
       np.currency AS new_price_currency, np.reviewed_at IS NULL AS new_price_pending,
       ch.id AS cheaper_id, ch.seller AS cheaper_seller, ch.placement_cents AS cheaper_cents,
       ch.currency AS cheaper_currency, ch.placement_eur_cents AS cheaper_eur_cents,
       ch.reviewed_at IS NULL AS cheaper_pending,
       EXISTS (SELECT 1 FROM site_prices x
               WHERE x.site_id = s.id AND x.reviewed_at IS NULL) AS offers_pending,
       g.ratio AS gray_ratio,
       coalesce(ln.notes, 0) AS notes_count, ln.body AS last_note, ln.created_at AS last_note_at
FROM sites s
LEFT JOIN LATERAL (SELECT x.dr, x.organic_traffic, x.total_keywords, x.checked_at,
                          x.seller_id, x.seller_id IS NULL OR xs.metrics_trusted AS trusted
                   FROM site_metrics x LEFT JOIN sellers xs ON xs.id = x.seller_id
                   WHERE x.site_id = s.id
                   ORDER BY x.seller_id IS NULL OR xs.metrics_trusted DESC, x.checked_at DESC
                   LIMIT 1) m ON true
LEFT JOIN sellers ms ON ms.id = m.seller_id
LEFT JOIN LATERAL (SELECT x.top_geo, x.top_geo_traffic, x.checked_at
                   FROM site_metrics x LEFT JOIN sellers xs ON xs.id = x.seller_id
                   WHERE x.site_id = s.id AND x.top_geo IS NOT NULL
                   ORDER BY x.seller_id IS NULL OR xs.metrics_trusted DESC, x.checked_at DESC
                   LIMIT 1) tg ON true
LEFT JOIN site_prices pr ON pr.id = s.price_id
LEFT JOIN sellers ps ON ps.id = pr.seller_id
LEFT JOIN LATERAL (SELECT o.id, o.placement_cents, o.currency, o.reviewed_at
                   FROM v_site_offers o
                   WHERE o.site_id = s.id AND o.seller_id = pr.seller_id
                     AND o.placement_type = pr.placement_type AND o.id <> pr.id) np ON true
LEFT JOIN LATERAL (SELECT o.id, o.seller, o.placement_cents, o.currency, o.placement_eur_cents,
                          o.reviewed_at
                   FROM v_site_offers o
                   WHERE o.site_id = s.id AND o.placement_type = pr.placement_type
                     AND o.seller_id <> pr.seller_id
                     AND o.placement_eur_cents < round(pr.placement_cents / eur_rate(pr.currency))
                   ORDER BY o.placement_eur_cents, o.id LIMIT 1) ch ON true
LEFT JOIN LATERAL (SELECT * FROM gray_scans x WHERE x.site_id = s.id
                   ORDER BY checked_at DESC LIMIT 1) g ON true
LEFT JOIN (SELECT DISTINCT ON (x.site_id) x.site_id, x.body, x.created_at,
                  count(*) OVER (PARTITION BY x.site_id) AS notes
           FROM site_notes x
           ORDER BY x.site_id, x.created_at DESC, x.id DESC) ln ON ln.site_id = s.id
WHERE NOT s.is_deleted;

-- Площадка в работе продукта «на сегодня»: статус, последний аудит под этот
-- продукт, его опубликованные размещения и другие наши продукты, уже
-- размещённые на площадке (ADR-030).
-- we_write и expected_spend вычисляются здесь и больше нигде (одна точка правды),
-- от рабочей цены, в евро (ADR-043).
-- Порог написания — writing_eur из настройки PRICE_REFERENCE: локальное значение
-- продукта перекрывает общее. ≤ порога — пишет площадка, > порога или пусто —
-- пишем мы. У вставки ссылки писать нечего — we_write пусто. expected_spend —
-- сколько заплатим площадке на самом деле. Настройки или цены нет — пусто:
-- порог не угадываем.
CREATE VIEW v_product_site_latest AS
SELECT ps.id, ps.product_id, ps.site_id, ps.status, ps.reject_reason, ps.imported_undecided,
       l.domain, l.language, l.topics, l.declared_topics,
       l.links_allowed, l.link_type, l.marks_as_ad,
       l.dr, l.organic_traffic, l.total_keywords, l.top_geo, l.top_geo_traffic, l.top_geo_at,
       l.metrics_at,
       l.metrics_trusted, l.metrics_seller,
       l.price_id, l.price_seller_id, l.price_seller, l.price_type,
       l.placement_cents, l.announce_cents, l.writing_cents, l.price_currency, l.prices_at,
       l.placement_eur_cents, l.writing_eur_cents, l.reference_total_cents,
       CASE WHEN l.price_type = 'link_insertion' THEN NULL
            ELSE l.writing_eur_cents IS NULL OR l.writing_eur_cents > w.writing_cents END AS we_write,
       l.reference_total_cents
         + CASE WHEN w.writing_cents IS NULL THEN NULL
                WHEN l.writing_eur_cents <= w.writing_cents THEN l.writing_eur_cents
                ELSE 0 END AS expected_spend_cents,
       l.new_price_id, l.new_price_cents, l.new_price_currency, l.new_price_pending,
       l.cheaper_id, l.cheaper_seller, l.cheaper_cents, l.cheaper_currency,
       l.cheaper_eur_cents, l.cheaper_pending, l.offers_pending,
       l.gray_ratio,
       l.notes_count, l.last_note, l.last_note_at,
       a.verdict AS last_verdict, a.score AS last_score, a.created_at AS audited_at,
       pp.published AS placements_published,
       op.names AS other_products_placed
FROM product_sites ps
JOIN v_site_latest l ON l.id = ps.site_id
LEFT JOIN LATERAL (SELECT (x.value->>'writing_eur')::integer * 100 AS writing_cents
                   FROM domain_settings x
                   WHERE x.key = 'PRICE_REFERENCE'
                     AND (x.product_id = ps.product_id OR x.product_id IS NULL)
                   ORDER BY x.product_id NULLS LAST LIMIT 1) w ON true
LEFT JOIN LATERAL (SELECT * FROM site_audits x
                   WHERE x.site_id = ps.site_id AND x.product_id = ps.product_id
                   ORDER BY created_at DESC LIMIT 1) a ON true
LEFT JOIN LATERAL (SELECT count(*) AS published FROM placements x
                   WHERE x.site_id = ps.site_id AND x.product_id = ps.product_id
                     AND x.status = 'published') pp ON true
LEFT JOIN LATERAL (SELECT array_agg(DISTINCT pr.name ORDER BY pr.name) AS names
                   FROM placements x JOIN products pr ON pr.id = x.product_id
                   WHERE x.site_id = ps.site_id AND x.product_id <> ps.product_id
                     AND x.status = 'published') op ON true;

-- Последний замер площадки по каждой стране (ADR-045): колонки «трафик» и «ключи»
-- выбранного региона в «Площадках». Страна здесь есть, только если под неё грузили
-- выгрузку страны.
CREATE VIEW v_site_country_latest AS
SELECT DISTINCT ON (x.site_id, x.country)
       x.id, x.site_id, x.country, x.organic_traffic, x.total_keywords, x.source, x.checked_at
FROM site_country_metrics x
ORDER BY x.site_id, x.country, x.checked_at DESC, x.id DESC;
