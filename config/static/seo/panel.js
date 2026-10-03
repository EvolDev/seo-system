/* Панель записи справа поверх списка (E9-11, ADR-048).
 *
 * Щелчок по записи в списке открывает её панелью справа: список остаётся на
 * месте. Ширина панели — по содержимому (seo/panel.css), но не дальше первой
 * колонки (домен, название): она видна всегда. Панелью открываются:
 * - ссылки с data-panel (адрес — значение атрибута, пустое — href): карточка
 *   площадки и решение по площадке в «Площадках», домен в разборе загрузки,
 *   ссылки внутри панели («Факты о площадке», «Сменить статус»);
 * - в списках с панелью (класс seo-panel-list у <body>, `panel = True` у
 *   админки) — ссылки строк на запись этого списка и «Добавить …» над ним.
 * Ctrl/Cmd/Shift и средняя кнопка — полная страница, как без скрипта.
 *
 * Содержимое — ответ сервера на запрос с заголовком X-Seo-Partial: страница
 * формы (из неё берём #content) или кусок без страницы (карточка, решение).
 * Стили, скрипты и виджеты формы поднимает seoNav.mount (seo/soft-nav.js).
 *
 * Главная форма — та, где низ панели («Сохранить», «Отмена»). Записали —
 * сервер отвечает JSON: панель закрывается, строка списка обновляется на
 * месте — экран перечитывается, но меняются только ячейки этой строки,
 * галочки остаются; перестала подходить под фильтры — строка блёклая до
 * обновления списка. Ошибка в форме — форма с подсказками остаётся в панели.
 * Формы карточки («Сделать рабочей», заметка) отвечают новой карточкой, а
 * строка обновится, когда с карточки уйдут.
 *
 * Esc, ✕, щелчок мимо панели, ↑/↓ (соседняя запись) и переход по ссылке при
 * несохранённых правках их не теряют: внизу панели вопрос «Сохранить ·
 * Не сохранять · Остаться».
 */
(function () {
  "use strict";

  if (window.seoPanel) return;

  var PARTIAL = { "X-Seo-Partial": "1" };
  // Экран уже — панель во всю ширину; панель уже — форма не помещается
  // (то же число — min-width в seo/panel.css).
  var FULL_WIDTH_BELOW = 900;
  var MIN_WIDTH = 480;
  var GAP = 12;
  // Первый показ ждёт содержимое, но не дольше: дальше — «Загружаю…».
  var REVEAL_MS = 400;
  // Что живёт поверх страницы, но относится к панели или своему окну:
  // выпадающее автодополнение, календарь, окно связанной записи, сообщения.
  var OVERLAYS = ".seo-panel, .select2-container, .calendarbox, .clockbox, .mfp-wrap, .mfp-bg, "
    + ".seo-toast-box, .seo-toasts, #djDebug";
  var STALE = "Больше не подходит под фильтры списка — уйдёт, когда список обновится";

  var panel = null;
  var body = null;
  // Что показано: адрес; строка списка, откуда открыли, и колонка ссылки —
  // по ним ↑/↓ находят соседнюю запись; key — ссылка в строке, по ней строка
  // находится в перечитанном списке; add — форма новой записи.
  var current = null;
  var styles = [];
  // Главная форма, какой её показали: с ней сравниваем, есть ли правки.
  var snapshot = null;
  // Формы карточки что-то записали: строку обновить, когда с неё уйдут.
  var changed = false;
  var controller = null;
  var busy = false;
  var asking = null;
  var askNext = null;
  var revealTimer = 0;
  // Что сделать после записи вместо закрытия (вопрос при ↑/↓ или переходе).
  var afterSave = null;

  function seoNav() {
    return window.seoNav && window.seoNav.enabled ? window.seoNav : null;
  }

  // ---------- Какие ссылки открывают панель ----------

  function panelUrl(link) {
    if (link.hasAttribute("data-panel")) return link.getAttribute("data-panel") || link.href;
    if (!document.body.classList.contains("seo-panel-list")) return null;
    if (link.classList.contains("addlink") && link.closest(".object-tools")) return link.href;
    if (link.closest("#result_list tbody") && recordUrl(link.href)) return link.href;
    return null;
  }

  // Запись этого же списка: <адрес списка><номер>/change/.
  function recordUrl(href) {
    var url = new URL(href, window.location.href);
    var list = window.location.pathname;
    return url.origin === window.location.origin && url.pathname.indexOf(list) === 0
      && /^\d+\/change\/$/.test(url.pathname.slice(list.length));
  }

  function linkKey(link) {
    return link.getAttribute("data-panel") || link.getAttribute("href");
  }

  // ---------- Панель ----------

  function build() {
    if (panel && panel.isConnected) return;
    panel = document.createElement("aside");
    panel.className = "seo-panel";
    panel.hidden = true;
    panel.tabIndex = -1;
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "false");
    panel.setAttribute("aria-label", "Запись");
    body = document.createElement("div");
    body.className = "seo-panel-body";
    panel.appendChild(body);
    document.body.appendChild(panel);
    panel.addEventListener("click", onPanelClick);
    panel.addEventListener("submit", onSubmit);
  }

  function isOpen() {
    return Boolean(panel && panel.isConnected && !panel.hidden);
  }

  function message(text) {
    body.textContent = "";
    var line = document.createElement("p");
    line.className = "seo-panel-message";
    line.textContent = text;
    body.appendChild(line);
  }

  // Предел ширины — до первой колонки строки (галочку действий не считаем);
  // саму ширину задаёт содержимое (seo/panel.css).
  function place() {
    if (!isOpen()) return;
    var width = window.innerWidth;
    var full = width < FULL_WIDTH_BELOW;
    panel.classList.toggle("seo-panel-full", full);
    if (full) {
      panel.style.maxWidth = "";
      return;
    }
    var cell = current && current.row && current.row.isConnected ? firstCell(current.row) : null;
    var room = width - GAP - (cell ? Math.ceil(cell.getBoundingClientRect().right) : 0);
    panel.style.maxWidth = Math.max(MIN_WIDTH, room) + "px";
  }

  function firstCell(row) {
    var cells = row.cells;
    if (!cells || !cells.length) return null;
    return cells[0].querySelector("input[type=checkbox]") && cells[1] ? cells[1] : cells[0];
  }

  function highlight(row) {
    document.querySelectorAll("tr.seo-panel-row").forEach(function (tr) {
      if (tr !== row) tr.classList.remove("seo-panel-row");
    });
    if (row) row.classList.add("seo-panel-row");
  }

  // ---------- Открыть ----------

  // inside — ссылка из самой панели: строка и колонка остаются прежними.
  function open(link, url, inside) {
    // Идёт запись — щелчок по другой записи подождёт: правки ещё не в базе.
    if (busy) return;
    if (dirty()) {
      ask(function () { open(link, url, inside); });
      return;
    }
    var row = inside && current ? current.row : rowOf(link);
    if (current && current.row && current.row !== row && changed) refreshRow(current);
    if (!current || current.row !== row) changed = false;
    if (isOpen() && current && current.url === new URL(url, window.location.href).href) return;
    var next = inside && current
      ? { row: current.row, column: current.column, key: current.key, opener: current.opener }
      : { row: row, column: row ? cellOf(link, row) : -1, key: linkKey(link), opener: link };
    next.url = new URL(url, window.location.href).href;
    next.add = /\/add\/$/.test(new URL(next.url).pathname);
    build();
    current = next;
    snapshot = null;
    hideAsk();
    var first = panel.hidden;
    panel.hidden = false;
    document.documentElement.classList.add("seo-panel-open");
    highlight(row);
    place();
    if (first) {
      // Невидимая, пока не пришло содержимое: ширина по нему, без рывка.
      body.textContent = "";
      panel.classList.add("seo-panel-pending");
      revealTimer = setTimeout(function () {
        message("Загружаю…");
        reveal();
      }, REVEAL_MS);
    } else if (!panel.contains(document.activeElement)) {
      panel.focus({ preventScroll: true });
    }
    request(current.url, {});
  }

  function reveal() {
    clearTimeout(revealTimer);
    if (!panel.classList.contains("seo-panel-pending")) return;
    panel.classList.remove("seo-panel-pending");
    panel.classList.add("seo-panel-in");
    setTimeout(function () { panel.classList.remove("seo-panel-in"); }, 400);
    if (!panel.contains(document.activeElement)) panel.focus({ preventScroll: true });
  }

  function rowOf(link) {
    var row = link.closest("tr");
    return row && row.parentNode && row.parentNode.tagName === "TBODY" ? row : null;
  }

  function cellOf(link, row) {
    var cell = link.closest("td, th");
    return cell && cell.parentNode === row ? cell.cellIndex : -1;
  }

  // ---------- Запрос и ответ ----------

  async function request(url, init, form) {
    var nav = seoNav();
    if (controller) controller.abort();
    controller = new AbortController();
    var mine = controller;
    panel.classList.add("seo-panel-loading");
    if (nav) nav.progress.start();
    try {
      var response = await fetch(url, Object.assign({
        headers: PARTIAL,
        credentials: "same-origin",
        signal: mine.signal,
      }, init));
      await handle(response, form, mine);
    } catch (error) {
      if (error && error.name === "AbortError") return;
      if (form) {
        if (nav) nav.toast("Не удалось сохранить: " + error.message);
      } else {
        message("Не удалось открыть: " + error.message + ".");
        reveal();
      }
    } finally {
      if (nav) nav.progress.done();
      if (controller === mine) {
        controller = null;
        busy = false;
        if (panel) panel.classList.remove("seo-panel-loading");
        if (form && form.isConnected) unbusy(form);
      }
    }
  }

  async function handle(response, form, mine) {
    var type = response.headers.get("Content-Type") || "";
    if (response.ok && type.indexOf("json") !== -1) {
      var data = await response.json();
      if (controller !== mine) return;
      if (data.saved) {
        saved(data);
        return;
      }
    }
    // Страница — форма (с подсказками, если в ней ошибки) или новая карточка.
    if (type.indexOf("text/html") !== -1 && response.ok) {
      var html = await response.text();
      if (controller !== mine) return;
      var keep = form ? snapshot : null;
      await show(html);
      // Правки после ошибки по-прежнему не сохранены: сравниваем с тем, что
      // было до них. Куда шли после записи — забываем: исправили ошибку и
      // нажали «Сохранить» — панель просто закроется.
      if (keep !== null) snapshot = keep;
      if (form) afterSave = null;
      if (form && form.hasAttribute("data-card-form")) changed = true;
      return;
    }
    throw new Error("сервер ответил " + response.status);
  }

  async function show(html) {
    // Ответ пришёл: «Загружаю…» уже не нужно — иначе таймер стёр бы содержимое,
    // которое как раз вставляется. Панель покажем, когда оно будет готово.
    clearTimeout(revealTimer);
    var doc = new DOMParser().parseFromString(html, "text/html");
    // Сессия кончилась — вход, после него эта же запись страницей.
    if (doc.body.classList.contains("login")) {
      window.location.assign(current ? current.url : window.location.href);
      return;
    }
    toastMessages(doc);
    var source = doc.getElementById("content") || doc.body;
    var added = await window.seoNav.mount(body, doc, source);
    styles = styles.concat(added);
    snapshot = serialize(mainForm());
    body.scrollTop = 0;
    reveal();
    if (!panel.contains(document.activeElement)) panel.focus({ preventScroll: true });
  }

  // Сообщения админки из полученной страницы: они уже прочитаны этим запросом
  // и на экране больше не появятся.
  function toastMessages(doc) {
    var nav = seoNav();
    if (!nav) return;
    doc.querySelectorAll(".messagelist li").forEach(function (item) {
      nav.toast(item.textContent.trim(), item.classList.contains("error") ? "error" : "success");
    });
  }

  function saved(data) {
    var nav = seoNav();
    var was = current;
    // Запись кончилась: дальше можно открыть следующую запись.
    busy = false;
    snapshot = null;
    if (nav) {
      nav.toast(data.message, "success");
      (data.notes || []).forEach(function (note) { nav.toast(note, "success"); });
    }
    var next = afterSave;
    afterSave = null;
    if (was && was.add) {
      // Новой строки на экране нет — перечитать список и показать её.
      close({ force: true });
      if (nav) nav.reload().then(function () { flashRecord(data.pk); });
      return;
    }
    changed = false;
    refreshRow(was);
    if (next) next();
    else close({ force: true });
  }

  // ---------- Закрыть ----------

  // force — без вопроса о правках; unload — уходим с экрана: строку не
  // обновлять и стили не снимать (новый экран снимет лишние сам, а нужные ему
  // уже стоят).
  function close(options) {
    options = options || {};
    if (!isOpen()) return;
    // Идёт запись — закрытие подождёт: оборванный запрос оставил бы строку
    // списка старой, хотя на сервере запись прошла.
    if (busy && !options.unload) return;
    if (!options.force && dirty()) {
      ask(function () { close({ force: true }); });
      return;
    }
    if (controller) controller.abort();
    hideAsk();
    afterSave = null;
    clearTimeout(revealTimer);
    panel.classList.remove("seo-panel-pending", "seo-panel-in");
    panel.hidden = true;
    document.documentElement.classList.remove("seo-panel-open");
    highlight(null);
    body.textContent = "";
    var was = current;
    current = null;
    snapshot = null;
    if (!options.unload) {
      styles.forEach(function (link) { link.remove(); });
      if (changed) refreshRow(was);
      var back = was && was.row && was.row.isConnected ? linkIn(was.row, was.column) : null;
      if (back) back.focus({ preventScroll: true });
    }
    styles = [];
    changed = false;
  }

  // ---------- Правки и вопрос о них ----------

  function mainForm() {
    var save = body ? body.querySelector(".seo-panel-foot [data-panel-save]") : null;
    return save ? save.closest("form") : null;
  }

  function serialize(form) {
    if (!form) return null;
    var pairs = [];
    formData(form).forEach(function (value, name) {
      pairs.push([name, typeof value === "string" ? value : value.name]);
    });
    return new URLSearchParams(pairs).toString();
  }

  function dirty() {
    if (!isOpen() || snapshot === null) return false;
    var form = mainForm();
    return Boolean(form) && serialize(form) !== snapshot;
  }

  function ask(next) {
    askNext = next;
    if (!asking) {
      asking = document.createElement("div");
      asking.className = "seo-panel-ask";
      asking.setAttribute("role", "alert");
      asking.innerHTML = '<span class="seo-panel-ask-text">Правки не сохранены.</span>'
        + '<button type="button" class="seo-btn seo-btn-primary" data-ask="save">Сохранить</button>'
        + '<button type="button" class="seo-btn" data-ask="drop">Не сохранять</button>'
        + '<button type="button" class="seo-btn" data-ask="stay">Остаться</button>';
    }
    var foot = body.querySelector(".seo-panel-foot");
    if (foot) foot.insertBefore(asking, foot.firstChild);
    else body.appendChild(asking);
    asking.querySelector("[data-ask=save]").focus({ preventScroll: true });
  }

  function hideAsk() {
    if (asking && asking.isConnected) asking.remove();
    askNext = null;
  }

  function answer(choice) {
    var next = askNext;
    hideAsk();
    if (choice === "stay" || !next) {
      panel.focus({ preventScroll: true });
      return;
    }
    if (choice === "drop") {
      snapshot = null;
      next();
      return;
    }
    // «Сохранить»: записали — дальше туда, куда шли; ошибка — остаёмся с ней.
    var form = mainForm();
    if (!form) return;
    afterSave = next;
    form.requestSubmit(form.querySelector("[data-panel-save]"));
  }

  // ---------- Формы в панели ----------

  function onSubmit(event) {
    var form = event.target;
    if (event.defaultPrevented || form.matches("[data-indexation-form], [data-no-panel]")) return;
    if ((form.getAttribute("method") || "get").toLowerCase() !== "post") return;
    event.preventDefault();
    if (busy || !current) return;
    busy = true;
    var submitter = event.submitter || null;
    // «Что дальше» — только у главной формы, отправленной из вопроса о правках.
    if (form !== mainForm()) afterSave = null;
    // Адрес отправки — от адреса записи, а не страницы под панелью: у формы
    // Django он бывает относительным («?_changelist_filters=…»).
    var action = form.getAttribute("action");
    var url = action ? new URL(action, current.url).href : current.url;
    form.querySelectorAll("button[type=submit], input[type=submit]").forEach(function (button) {
      button.disabled = true;
    });
    if (submitter && submitter.tagName === "BUTTON") submitter.classList.add("seo-btn-busy");
    request(url, { method: "POST", body: formData(form, submitter) }, form);
  }

  function unbusy(form) {
    form.querySelectorAll("button[type=submit], input[type=submit]").forEach(function (button) {
      button.disabled = false;
      button.classList.remove("seo-btn-busy");
    });
  }

  function formData(form, submitter) {
    try {
      return new FormData(form, submitter || undefined);
    } catch (error) {
      var data = new FormData(form);
      if (submitter && submitter.name) data.append(submitter.name, submitter.value);
      return data;
    }
  }

  // ---------- Щелчки ----------

  function modified(event) {
    return event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey;
  }

  // Внутри панели: закрыть, отмена, вопрос, ссылки в панель и прочь.
  function onPanelClick(event) {
    var target = event.target instanceof Element ? event.target : null;
    var el = target && target.closest("[data-panel-close], [data-panel-cancel], [data-ask], a[href]");
    if (!el || event.defaultPrevented) return;
    // Нажатие разобрано здесь — дальше его не пускаем: кнопка вопроса уже
    // убрана со страницы, и обработчик ниже принял бы его за щелчок мимо панели.
    var handled = true;
    if (el.hasAttribute("data-panel-close")) {
      close();
    } else if (el.hasAttribute("data-panel-cancel")) {
      close({ force: true });
    } else if (el.hasAttribute("data-ask")) {
      answer(el.getAttribute("data-ask"));
    } else if (!modified(event) && el.hasAttribute("data-panel")) {
      open(el, el.getAttribute("data-panel") || el.href, true);
    } else {
      handled = false;
    }
    if (handled) {
      event.preventDefault();
      event.stopPropagation();
      return;
    }
    if (!modified(event) && dirty() && leaves(el)) {
      // Ссылка уводит с экрана (seo/soft-nav.js) — сначала спросить о правках.
      event.preventDefault();
      var href = el.href;
      ask(function () {
        close({ force: true });
        if (seoNav()) seoNav().visit(href);
        else window.location.assign(href);
      });
    }
  }

  // Ссылка — переход, а не окно связанной записи или якорь.
  function leaves(link) {
    var href = link.getAttribute("href") || "";
    return href.charAt(0) !== "#" && !link.target && !link.hasAttribute("download")
      && !link.closest(".related-widget-wrapper, .related-lookup");
  }

  // Несохранённые правки и щелчок мимо панели: щелчок не пропускаем — сначала
  // вопрос; после ответа повторим его. Ловим до всех обработчиков страницы.
  window.addEventListener("click", function (event) {
    if (modified(event) || !dirty()) return;
    var target = event.target instanceof Element ? event.target : null;
    if (!target || target.closest(OVERLAYS)) return;
    var link = target.closest("a[href]");
    if (link && panelUrl(link)) return; // другая запись — спросит open()
    event.preventDefault();
    event.stopPropagation();
    ask(function () {
      close({ force: true });
      if (target.isConnected) target.click();
    });
  }, true);

  // Ссылка на запись — в панель; щелчок по списку — закрыть панель (без
  // правок: с правками щелчок остановлен выше).
  document.addEventListener("click", function (event) {
    if (event.defaultPrevented || event.button !== 0) return;
    var target = event.target instanceof Element ? event.target : null;
    // Убранное со страницы (кнопка, которая сменила содержимое панели) — не «мимо».
    if (!target || !target.isConnected || target.closest(OVERLAYS)) return;
    var link = target.closest("a[href]");
    var url = link ? panelUrl(link) : null;
    if (url && !modified(event) && seoNav()) {
      event.preventDefault();
      open(link, url, false);
      return;
    }
    if (isOpen()) close();
  });

  // ---------- Клавиши ----------

  document.addEventListener("keydown", function (event) {
    if (!isOpen() || event.defaultPrevented) return;
    if (event.key === "Escape") {
      // Сначала закрывается своё окно: связанная запись, автодополнение.
      if (document.querySelector(".mfp-wrap, .select2-container--open")) return;
      event.preventDefault();
      if (asking && asking.isConnected) answer("stay");
      else close();
      return;
    }
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return;
    var target = event.target instanceof Element ? event.target : null;
    // В поле стрелки — его: курсор, выбор в списке, переключатель статуса.
    if (target && target.closest("input, textarea, select, [contenteditable], .select2-container")) return;
    if (asking && asking.isConnected) return;
    if (move(event.key === "ArrowDown" ? 1 : -1)) event.preventDefault();
  });

  // Соседняя запись: следующая строка с такой же ссылкой в той же колонке.
  function move(step) {
    if (!current || !current.row || !current.row.isConnected || current.column < 0) return false;
    var rows = Array.prototype.slice.call(current.row.parentNode.rows);
    for (var i = rows.indexOf(current.row) + step; i >= 0 && i < rows.length; i += step) {
      var link = linkIn(rows[i], current.column);
      if (link) {
        rows[i].scrollIntoView({ block: "nearest" });
        open(link, panelUrl(link), false);
        return true;
      }
    }
    return false;
  }

  function linkIn(row, column) {
    var cell = row.cells && row.cells[column];
    if (!cell) return null;
    var links = cell.querySelectorAll("a[href]");
    for (var i = 0; i < links.length; i += 1) {
      if (panelUrl(links[i])) return links[i];
    }
    return null;
  }

  // ---------- Строка списка на месте ----------

  async function refreshRow(was) {
    var row = was && was.row;
    var nav = seoNav();
    if (!was || !nav) return;
    // Открыли не из строки (карточка со страницы площадки) — перечитать экран:
    // на нём то, что поменяли в панели, например рабочая цена.
    if (!row) {
      nav.reload();
      return;
    }
    if (!row.isConnected) return;
    // Строка не из списка админки (разбор загрузки) или список с полями прямо в
    // строках (общая форма с номерами строк) — перечитать экран целиком.
    if (!row.closest("#result_list") || document.querySelector("#changelist-form input[name$=TOTAL_FORMS]")) {
      nav.reload();
      return;
    }
    var response;
    try {
      response = await fetch(window.location.href, { credentials: "same-origin" });
    } catch (error) {
      return;
    }
    if (!response.ok || (response.headers.get("Content-Type") || "").indexOf("text/html") === -1) return;
    var doc = new DOMParser().parseFromString(await response.text(), "text/html");
    if (!row.isConnected) return;
    toastMessages(doc);
    var fresh = findRow(doc, was.key);
    if (!fresh) {
      row.classList.add("seo-row-stale");
      row.title = STALE;
      return;
    }
    if (fresh.cells.length !== row.cells.length) {
      nav.reload();
      return;
    }
    for (var i = 0; i < row.cells.length; i += 1) {
      // Галочку оставляем свою: штатный actions.js держит галочки, найденные при
      // загрузке страницы, а отмеченное человеком терять нельзя.
      if (row.cells[i].querySelector("input.action-select")) continue;
      row.cells[i].replaceWith(document.importNode(fresh.cells[i], true));
    }
    row.classList.remove("seo-row-stale");
    if (row.title === STALE) row.removeAttribute("title");
    flash(row);
  }

  function findRow(doc, key) {
    var links = doc.querySelectorAll("#result_list tbody a[href]");
    for (var i = 0; i < links.length; i += 1) {
      if (links[i].getAttribute("href") === key || links[i].getAttribute("data-panel") === key) {
        return links[i].closest("tr");
      }
    }
    return null;
  }

  function flash(row) {
    row.classList.remove("seo-row-flash");
    void row.offsetWidth; // перезапуск подсветки
    row.classList.add("seo-row-flash");
    setTimeout(function () { row.classList.remove("seo-row-flash"); }, 2000);
  }

  // Новая запись после перечитывания списка — подсветить её строку.
  function flashRecord(pk) {
    var tail = "/" + pk + "/change/";
    var links = document.querySelectorAll("#result_list tbody a[href]");
    for (var i = 0; i < links.length; i += 1) {
      var path = new URL(links[i].href).pathname;
      if (path.slice(-tail.length) === tail) {
        flash(links[i].closest("tr"));
        return;
      }
    }
  }

  // ---------- Прочее ----------

  // Переход на другой экран — панель закрыть молча: правки уже спрошены при
  // щелчке, а «Назад» браузера не остановить.
  document.addEventListener("seo:unload", function () {
    if (isOpen()) close({ force: true, unload: true });
  });

  var resizing = 0;
  window.addEventListener("resize", function () {
    cancelAnimationFrame(resizing);
    resizing = requestAnimationFrame(place);
  });

  // F5, закрытие вкладки — вопрос браузера, свой здесь показать нельзя.
  window.addEventListener("beforeunload", function (event) {
    if (!dirty()) return;
    event.preventDefault();
    event.returnValue = "";
  });

  window.seoPanel = { isOpen: isOpen };
})();
