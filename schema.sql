-- ============================================================
-- Система автоматизации линкбилдинга — схема PostgreSQL 16
-- Версия 1.1 от 23.09.2026 (проверена применением на PostgreSQL 16.13)
--
-- Это опорный DDL. При работе через Django миграции генерируются
-- из моделей, но схема должна соответствовать этому файлу.
-- Перед применением миграции всегда смотреть sqlmigrate.
-- ============================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------- Перечисления ----------

CREATE TYPE site_status AS ENUM
    ('new','auditing','approved','rejected','placed','blacklisted');
CREATE TYPE metric_source AS ENUM
    ('ahrefs_api','serp_api','manual','csv_import','collaborator_api');
CREATE TYPE audit_verdict AS ENUM ('yes','no','borderline');
CREATE TYPE audit_author AS ENUM ('human','llm','system');
CREATE TYPE placement_status AS ENUM
    ('planned','ordered','writing','review','published','rejected','cancelled');
-- exact — точный ключ; diluted — ключ внутри фразы; branded, url, generic — безанкорка
-- (бренд, голый URL, нейтральное «here»), как во вкладке «Распределение безанкорки».
CREATE TYPE anchor_type AS ENUM ('exact','diluted','branded','url','generic');
CREATE TYPE tool_category AS ENUM ('Main','Video','Audio','Image','Doc');
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

-- ---------- Блок 1. Площадки ----------

CREATE TABLE products (
    id          bigserial PRIMARY KEY,
    name        text NOT NULL,
    domain      text NOT NULL UNIQUE,
    is_active   boolean NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now()
);

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
    content_profile     jsonb,              -- результат P2: тематика, fit_score
    status              site_status NOT NULL DEFAULT 'new',
    reject_reason       text,
    notes               text,
    content_selector    text,               -- ручной CSS-селектор тела статьи для краулера
    imported_undecided  boolean NOT NULL DEFAULT false,
    is_deleted          boolean NOT NULL DEFAULT false,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_sites_status ON sites(status) WHERE is_deleted = false;

CREATE TABLE site_metrics (
    id              bigserial PRIMARY KEY,
    site_id         bigint NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    dr              smallint,
    organic_traffic integer,
    us_traffic      integer,
    top_geo         text,
    top_geo_traffic integer,
    total_keywords  integer,
    source          metric_source NOT NULL DEFAULT 'manual',
    raw             jsonb,
    checked_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_metrics_site ON site_metrics(site_id, checked_at DESC);

CREATE TABLE site_prices (
    id               bigserial PRIMARY KEY,
    site_id          bigint NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    placement_cents  integer,
    announce_cents   integer,
    writing_cents    integer,
    currency         char(3) NOT NULL DEFAULT 'EUR',
    source           metric_source NOT NULL DEFAULT 'manual',
    checked_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_prices_site ON site_prices(site_id, checked_at DESC);

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
CREATE INDEX idx_audits_site ON site_audits(site_id, created_at DESC);

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
    announce_on_homepage   boolean,
    clicks_from_homepage   smallint,
    comment                text,
    run_id                 uuid,
    created_at             timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_placements_site ON placements(site_id, product_id);
CREATE INDEX idx_placements_status ON placements(status);
CREATE INDEX idx_placements_pub ON placements(published_at DESC);

CREATE TABLE keywords (
    id           bigserial PRIMARY KEY,
    product_id   bigint NOT NULL REFERENCES products(id),
    keyword      text NOT NULL,
    target_url   text NOT NULL,
    volume       integer,
    global_volume integer,
    tool         tool_category,
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
    char_offset             integer,
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

CREATE TABLE prompt_templates (
    id          bigserial PRIMARY KEY,
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
    pattern                 text NOT NULL,
    example                 text,
    tool                    tool_category,
    anchor_type             anchor_type,
    extraction_test_passed  boolean NOT NULL DEFAULT true,
    language                text NOT NULL DEFAULT 'en',
    score                   numeric(4,3) NOT NULL DEFAULT 0.5,
    times_used              integer NOT NULL DEFAULT 0,
    times_accepted          integer NOT NULL DEFAULT 0,
    is_active               boolean NOT NULL DEFAULT true
);

CREATE TABLE rules (
    id           bigserial PRIMARY KEY,
    code         text NOT NULL UNIQUE,
    description  text NOT NULL,
    severity     rule_severity NOT NULL,
    check_type   rule_check_type NOT NULL,
    params       jsonb,
    is_active    boolean NOT NULL DEFAULT true
);

-- Пороги аудита, контентные настройки и списки (белый список доменов).
-- У них нет критичности, поэтому они не живут в rules.
CREATE TABLE domain_settings (
    key          text PRIMARY KEY,
    value        jsonb NOT NULL,
    description  text,
    updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE examples (
    id              bigserial PRIMARY KEY,
    kind            example_kind NOT NULL,
    scope           example_scope NOT NULL,
    body            text NOT NULL,
    reason          text,
    tags            text[],
    language        text NOT NULL DEFAULT 'en',
    rule_code       text REFERENCES rules(code),
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

CREATE TABLE api_usage (
    id          bigserial PRIMARY KEY,
    provider    text NOT NULL,
    endpoint    text,
    units       integer,
    cost_cents  integer,
    currency    char(3) NOT NULL DEFAULT 'USD',
    run_id      uuid,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_usage_provider ON api_usage(provider, created_at DESC);

-- ---------- Представления ----------

CREATE VIEW v_keyword_coverage AS
SELECT
    k.id,
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
SELECT status, imported_undecided, count(*) AS sites
FROM sites
WHERE NOT is_deleted
GROUP BY status, imported_undecided;

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

-- Площадка «на сегодня»: последние метрики, цены, серость и вердикт.
-- we_write и reference_total вычисляются здесь и больше нигде (одна точка правды).
-- reference_total — то, что сравнивается с ориентиром 550 EUR: размещение + анонс.
-- Написание в него не входит: ≤ 50 EUR — пишет площадка, > 50 или пусто — пишем мы
-- (промпт аудитора). expected_spend — сколько заплатим площадке на самом деле.
CREATE VIEW v_site_latest AS
SELECT s.id, s.domain, s.status, s.language, s.topics, s.declared_topics,
       s.links_allowed, s.link_type, s.marks_as_ad, s.imported_undecided,
       m.dr, m.organic_traffic, m.total_keywords, m.top_geo, m.checked_at AS metrics_at,
       pr.placement_cents, pr.announce_cents, pr.writing_cents, pr.checked_at AS prices_at,
       (pr.writing_cents IS NULL OR pr.writing_cents > 5000) AS we_write,
       coalesce(pr.placement_cents, 0) + coalesce(pr.announce_cents, 0) AS reference_total_cents,
       coalesce(pr.placement_cents, 0) + coalesce(pr.announce_cents, 0)
         + CASE WHEN pr.writing_cents <= 5000 THEN pr.writing_cents ELSE 0 END AS expected_spend_cents,
       g.ratio AS gray_ratio, a.verdict AS last_verdict, a.score AS last_score
FROM sites s
LEFT JOIN LATERAL (SELECT * FROM site_metrics x WHERE x.site_id = s.id
                   ORDER BY checked_at DESC LIMIT 1) m ON true
LEFT JOIN LATERAL (SELECT * FROM site_prices x WHERE x.site_id = s.id
                   ORDER BY checked_at DESC LIMIT 1) pr ON true
LEFT JOIN LATERAL (SELECT * FROM gray_scans x WHERE x.site_id = s.id
                   ORDER BY checked_at DESC LIMIT 1) g ON true
LEFT JOIN LATERAL (SELECT * FROM site_audits x WHERE x.site_id = s.id
                   ORDER BY created_at DESC LIMIT 1) a ON true
WHERE NOT s.is_deleted;
