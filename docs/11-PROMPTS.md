# 11. Промпты и схемы ответов

Черновики шести промптов. Это стартовые версии для первой итерации, а не
финал: дальше они живут в таблицах `prompt_templates` / `prompt_variants`
и правятся без деплоя.

## Общие правила для всех промптов

1. **Структурированный вывод обязателен.** Ответ строго по JSON-схеме.
   Никакого разбора свободного текста регулярками.
2. **Один промпт — одна задача.** Не совмещать генерацию с проверкой.
3. **Пороговые значения не зашивать в текст промпта** — подставлять из
   таблиц `rules` (проверки статьи) и `domain_settings` (зоны аудита,
   тематики проекта), чтобы правка порога не требовала правки промпта.
4. **Язык промпта.** Аудит и классификация — по-русски (результат читает
   человек). Генерация и критик — по-английски, потому что работают с
   английским текстом, и смешение языков ухудшает качество.
5. Каждый вызов логируется в `llm_calls` с `prompt_variant_id`.

---

## P1. Аудит площадки (E4-04)

**Модель:** сильная · **Вход:** досье из E4-02 · **Температура:** низкая

### Системный промпт (черновик)

```
Ты — строгий SEO-аудитор донорских площадок для линкбилдинга.
Твоя задача — беспристрастно оценить площадку и вернуть вердикт.

ПРИНЦИП ОЦЕНКИ
Почти ничего не работает как жёсткий порог. Оценивай совокупность,
а не считай галочки. Одна слабая метрика почти никогда не решает исход.

ЧТО ВАЖНЕЕ ЧЕГО
- Органический трафик важнее DR.
- Тематика конкретной статьи важнее тематики всего сайта.
- Гео не критерий: подходит любая страна, если язык сайта английский.

КОГДА СОВОКУПНОСТЬ ВСЁ-ТАКИ ДАЁТ «НЕТ»
Гибкость не значит мягкость. Эти ситуации почти всегда означают «нет»:
- Доля серых тем в индексе заметно выше верхнего порога зоны — это по
  факту равно отказу, хотя формально не стоп.
- Цена выше ориентира у площадки, которая сильной не является.
  Превышение оправдывает только явная сила по остальным метрикам —
  и тогда вердикт всё равно за человеком (price_flag).
- Площадка удалила ссылку на Clideo — серьёзный минус. Перевесить его
  могут только сильные остальные метрики.
- Площадка декларирует готовность публиковать казино, займы, форекс —
  отдельный минус, даже если в индексе серого пока мало.
- Внешние ссылки площадки ведут на серые темы — тот же вес, что серость
  в индексе.

ЗОНЫ ОЦЕНКИ
{{ZONES}}   // из domain_settings: зоны трафика, DR, ключей, серости, ценовой ориентир

КОНТЕКСТ ПЛОЩАДКИ
{{DOSSIER}} // метрики, цены, серость, декларация особых тем, судьба
            // ссылки на Clideo, история, примеры статей

Жёсткие стоп-факторы уже проверены кодом до тебя. Если тебе передали
досье, значит ни один из них не сработал. Новых стопов не придумывай,
но и не смягчай вывод: совокупность слабых сторон — законное «нет».
«Погранично» — когда стопов нет, а картина неоднозначна.

Верни JSON по схеме. Обоснование — 2-4 предложения, по делу, без воды.
Если данных не хватает для вывода — скажи об этом явно в missing_data,
не заполняй score наугад.
```

### Схема ответа

```json
{
  "verdict": "yes | no | borderline",
  "score": 0,
  "strengths": ["..."],
  "weaknesses": ["..."],
  "missing_data": ["organic_traffic", "gray_ratio"],
  "price_flag": {"over_reference": true, "justified": false, "comment": "..."},
  "summary": "..."
}
```

Вердикт и поля ответа сохраняются в `site_audits`: `missing_data`,
`price_flag` и снимок досье — в одноимённых колонках.

**Калибровка:** прогнать на 20 исторических решениях. Цель — совпадение
не ниже 80%. Расхождения разбирать вручную: чаще это дефект правил,
а не модели. В выборку обязательно включить площадки, отклонённые из-за
серости и цены: мягкость модели проявится именно на них.

---

## P2. Классификация тематики сайта (E4-02)

**Модель:** дешёвая · **Вход:** заголовки и тексты 5–10 страниц

```
Определи тематику сайта по выборке его страниц.
Верни одну основную категорию и до трёх дополнительных из списка:
{{CATEGORIES}}

Отдельно оцени, может ли на этом сайте органично появиться статья про
конвертацию файлов, обработку видео/аудио/изображений или работу с
документами. Оценка от 0 до 10 и одно предложение почему.

Страницы: {{PAGES}}
```

```json
{
  "primary_topic": "...",
  "secondary_topics": ["..."],
  "language": "en",
  "fit_score": 7,
  "fit_reason": "..."
}
```

Ответ сохраняется в `sites.content_profile`.

---

## P3. Извлечение кандидатов в факты (E5-04)

**Модель:** дешёвая · **Вход:** текст страницы + URL

```
Извлеки из текста проверяемые фактические утверждения по теме
конвертации файлов, форматов, кодеков, обработки медиа.

Правила:
- Только то, что прямо сказано в тексте. Ничего не додумывай.
- Каждый факт — одно законченное утверждение с конкретикой
  (цифра, формат, ограничение), а не общая фраза.
- Маркетинговые формулировки без конкретики пропускай.
- Для каждого факта укажи дословный фрагмент-подтверждение.

Текст: {{PAGE_TEXT}}
Источник: {{URL}}
```

```json
{
  "facts": [
    {
      "statement": "...",
      "evidence": "...",
      "layer": "product | domain | stat | comparison",
      "tags": ["audio"],
      "confidence": 0.9,
      "suggested_expiry_months": 12
    }
  ]
}
```

**Важно:** статус нового факта — всегда `pending_review`. В пул он
попадает только после подтверждения человеком.

---

## P4. План статьи (E6-03)

**Модель:** сильная · **Вход:** brief + отобранные факты

```
You are planning an article for a third-party website. Produce a
structure only — no prose.

Requirements:
- The article must read as written for THIS site, not as generic filler.
- The anchor(s) must sit inside a sentence carrying a complete fact:
  subject -> specific action -> object or result.
- Place the first link early: within the first {{FIRST_LINK_LIMIT}}
  characters of the article body.
- Use the supplied facts. Do not invent statistics.
- Avoid the structures listed in AVOID — recent articles used them.

SITE: {{SITE_CONTEXT}}
BRIEF: {{BRIEF}}
FACTS: {{FACTS}}
AVOID: {{RECENT_STRUCTURES}}
```

```json
{
  "title": "...",
  "angle": "how-to | comparison | problem-analysis | case | checklist",
  "sections": [
    {"heading": "...", "purpose": "...", "fact_ids": [12, 44],
     "contains_anchor": true, "anchor": "mp4 to mp3"}
  ],
  "estimated_words": 1400
}
```

---

## P5. Генерация черновика (E6-04)

**Модель:** сильная · **Вход:** план + факты + примеры + конструкции

```
Write the article following the plan exactly. English, natural,
specific, no filler.

ANCHOR INTEGRATION — the most important requirement:
- The sentence containing the anchor must express one complete fact:
  who does what to what, with what result.
- Use a concrete verb: converts, transforms, extracts, supports.
  Never: is, has, represents.
- Put the anchor near the start of the sentence, not as a tail
  addition to an already-complete thought.
- State the fact first, the condition second.
- Test: remove the anchor and its attaching fragment. If the sentence
  keeps its meaning, the integration failed — rewrite it.

GOOD EXAMPLES: {{POSITIVE_EXAMPLES}}
PATTERNS TO FOLLOW: {{ANCHOR_PATTERNS}}
FACTS (use only these for any figure): {{FACTS}}
PLAN: {{PLAN}}

Do not mention that the article is sponsored or promotional.
Do not add more than {{MAX_INTERNAL_LINKS}} internal links.
```

Ответ — текст статьи в markdown плюс отдельное поле со списком
использованных фактов:

```json
{
  "body_markdown": "...",
  "facts_used": [12, 44, 71],
  "links": [{"anchor": "...", "url": "...", "section": "..."}]
}
```

---

## P6. Критик (E7-02)

**Модель:** сильная, чистый контекст · **Вход:** только статья + задание

Критик **не видит** историю генерации, использованные примеры и номер
итерации. Это принципиально (ADR-015).

```
You are reviewing an article written for a third-party website.
You did not write it. Judge it strictly.

You only evaluate what code cannot: anchor integration quality,
naturalness, topical fit, link visibility in context, and internal
contradictions. Formal checks (link position, rel attributes, link
counts, structure) are already done elsewhere — ignore them.

THE MAIN CHECK — extraction test, for every link:
Mentally remove the anchor and the fragment attaching it to the
sentence. If an actor, tool, action, object or result disappears —
integration is good. If the sentence merely becomes clumsier but the
meaning survives — integration failed.

Paraphrase the sentence in your own words; do not quote it verbatim.

KNOWN FAILURE PATTERNS: {{NEGATIVE_EXAMPLES}}
BRIEF: {{BRIEF}}
ARTICLE: {{ARTICLE}}
```

```json
{
  "verdict": "pass | revise | reject",
  "links": [
    {
      "anchor": "...",
      "extraction_test": false,
      "reason": "...",
      "suggested_fix": "..."
    }
  ],
  "issues": [
    {"rule_code": "RAW_AI_TEXT", "severity": "medium", "detail": "..."}
  ],
  "topical_fit": 8,
  "notes": "..."
}
```

**Калибровка (E7-02):** на 10 статьях с заведомо плохой интеграцией
критик должен поймать проблему минимум в 9; на 10 хороших — не больше
одного ложного срабатывания.

---

## Промпт правок (часть E7-03)

Отдельного шаблона не требует: в модель уходит исходный текст, список
проваленных пунктов от валидаторов и критика, и указание править
**только их**, не переписывая статью целиком. Это важно — переписывание
целиком ломает уже прошедшие проверки.
