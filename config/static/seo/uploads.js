/* Экраны «Загрузки» без перезагрузки страницы (E1-08, ADR-044).
 *
 * Шаги «файл → колонки → сводка → разбор», вкладки, порядок и страницы
 * разбора подгружаются запросом: меняется содержимое страницы и адрес в
 * браузере, «назад» работает. Кнопки записи и пересчёта — JSON-запрос,
 * лоадер в блоке со счётчиком секунд, опрос состояния загрузки; по
 * готовности обновляются только цифры и история, введённое остаётся.
 *
 * Подгрузка страниц, история и полоска вверху экрана — общие для всей
 * админки, seo/soft-nav.js (E9-09, ADR-046): экраны загрузки зовут
 * window.seoNav и навешивают обработчики по событию seo:load, снимают —
 * по seo:unload. Здесь — свои лоадеры: прогресс отправки большого файла,
 * крутилка в нажатой кнопке, мерцание обновляемых блоков.
 *
 * Фильтры каталога: поле «любое из» с поиском по значениям файла и
 * облачками, «от — до», облачка из прошлых загрузок, живой счётчик.
 * Выбранное запоминается в браузере для каждой загрузки.
 *
 * Разбор: «Сделать рабочей», «Оставить», «Удалить из базы», «Заблокировать»
 * в строке и для отмеченных, клавиши, отмена (U). Без скрипта всё работает
 * обычными переходами.
 */
(function () {
  "use strict";

  var POLL_MS = 1500;
  var TOAST_MS = 8000;
  var COUNT_DELAY_MS = 400;
  var DROPDOWN_LIMIT = 12;
  var AJAX = { "X-Seo-Ajax": "1" };

  // Что снять при уходе с экрана (seo:unload): опрос, клавиши разбора.
  var cleanups = [];

  // ---------- Общее ----------

  function csrfToken() {
    var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    if (match) return decodeURIComponent(match[1]);
    var input = document.querySelector("input[name=csrfmiddlewaretoken]");
    return input ? input.value : "";
  }

  async function request(url, options) {
    var response = await fetch(url, Object.assign({ credentials: "same-origin" }, options));
    var type = response.headers.get("Content-Type") || "";
    var body = type.indexOf("json") !== -1 ? await response.json().catch(function () { return {}; }) : null;
    if (!response.ok) throw new Error((body && body.error) || "Сервер ответил " + response.status);
    return { response: response, body: body };
  }

  async function postJson(url, data) {
    var result = await request(url, {
      method: "POST",
      headers: Object.assign({ "Content-Type": "application/json", "X-CSRFToken": csrfToken() }, AJAX),
      body: JSON.stringify(data),
    });
    return result.body || {};
  }

  function sleep(ms) { return new Promise(function (resolve) { setTimeout(resolve, ms); }); }

  function plural(n, one, few, many) {
    var mod10 = n % 10, mod100 = n % 100;
    if (mod10 === 1 && mod100 !== 11) return one;
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
    return many;
  }

  function storage() {
    try { return window.localStorage; } catch (error) { return null; }
  }

  // ---------- Лоадеры ----------

  // Полоска вверху экрана — общая (seo/soft-nav.js).
  var topbar = {
    start: function () { if (window.seoNav) window.seoNav.progress.start(); },
    done: function () { if (window.seoNav) window.seoNav.progress.done(); },
  };

  function busyButton(button, on) {
    if (!button) return;
    button.classList.toggle("seo-btn-busy", on);
    button.disabled = on;
  }

  // Лоадер в блоке: крутилка, текст и сколько секунд прошло.
  function inlineLoader(node, text, hint) {
    if (!node) return { stop: function () {}, set: function () {} };
    var started = Date.now();
    node.hidden = false;
    node.textContent = "";
    var spin = document.createElement("span");
    spin.className = "seo-spin";
    var label = document.createElement("b");
    label.textContent = text;
    var clock = document.createElement("span");
    clock.className = "seo-sub";
    var sub = document.createElement("div");
    sub.className = "seo-sub";
    sub.textContent = hint || "";
    var box = document.createElement("div");
    box.appendChild(label);
    box.appendChild(document.createTextNode(" "));
    box.appendChild(clock);
    box.appendChild(sub);
    node.appendChild(spin);
    node.appendChild(box);
    var timer = setInterval(function () {
      var seconds = Math.round((Date.now() - started) / 1000);
      clock.textContent = seconds + " с";
    }, 1000);
    return {
      set: function (value) { label.textContent = value; },
      stop: function () { clearInterval(timer); node.hidden = true; node.textContent = ""; },
    };
  }

  // ---------- Всплывающее сообщение ----------

  var toastBox = null;
  function toast(text, actionLabel, onAction, kind) {
    if (!toastBox) {
      toastBox = document.createElement("div");
      toastBox.className = "seo-toast-box";
      toastBox.setAttribute("role", "status");
      document.body.appendChild(toastBox);
    }
    var item = document.createElement("div");
    item.className = "seo-toast" + (kind ? " seo-toast-" + kind : "");
    var span = document.createElement("span");
    span.textContent = text;
    item.appendChild(span);
    if (actionLabel) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "seo-btn";
      button.textContent = actionLabel;
      button.addEventListener("click", function () { item.remove(); onAction(); });
      item.appendChild(button);
    }
    toastBox.appendChild(item);
    setTimeout(function () { item.remove(); }, kind === "error" ? TOAST_MS * 2 : TOAST_MS);
  }

  // ---------- Переходы — общие (seo/soft-nav.js) ----------

  function go(url) {
    if (window.seoNav) window.seoNav.visit(new URL(url, window.location.href).href);
    else window.location.href = url;
  }

  // Ответ на отправку файла получен здесь (ради хода отправки) — показать его.
  function show(html, url) {
    if (window.seoNav) window.seoNav.render(html, url);
    else window.location.href = url;
  }

  // Адрес отправки — атрибутом: у формы с полем name="action" свойство
  // form.action — это поле, а не адрес.
  function actionOf(form) { return form.getAttribute("action") || window.location.href; }

  // ---------- Шаг «Файл»: отправка с прогрессом ----------

  function initForm(form) {
    var rows = form.querySelectorAll("[data-seller-row]");
    var countryRow = form.querySelector("[data-country-row]");
    // Подписи даты: у выгрузки Ahrefs это дата замера, а не цен.
    var swaps = Array.prototype.map.call(form.querySelectorAll("[data-ahrefs]"), function (node) {
      return { node: node, plain: node.textContent, ahrefs: node.getAttribute("data-ahrefs") };
    });
    function sync() {
      var checked = form.querySelector("input[name=kind]:checked");
      var kind = checked ? checked.value : "price_list";
      rows.forEach(function (row) { row.hidden = kind !== "price_list"; });
      if (countryRow) countryRow.hidden = kind !== "ahrefs_batch";
      swaps.forEach(function (swap) { swap.node.textContent = kind === "ahrefs_batch" ? swap.ahrefs : swap.plain; });
      // У выгрузки Ahrefs шагов три: колонок и разбора нет.
      var plainSteps = document.querySelector("[data-steps-plain]");
      var ahrefsSteps = document.querySelector("[data-steps-ahrefs]");
      if (plainSteps && ahrefsSteps) {
        plainSteps.hidden = kind === "ahrefs_batch";
        ahrefsSteps.hidden = kind !== "ahrefs_batch";
      }
    }
    // Страна выгрузки — общий выбор с флагами и поиском (seo/country-picker.js).
    var picker = form.querySelector("[data-country-picker]");
    if (picker && window.seoCountryPicker) window.seoCountryPicker(picker);
    form.querySelectorAll("input[name=kind]").forEach(function (input) {
      input.addEventListener("change", sync);
    });
    sync();

    var progress = form.querySelector("[data-upload-progress]");
    var bar = form.querySelector("[data-upload-bar]");
    var text = form.querySelector("[data-upload-text]");
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var button = form.querySelector("button[type=submit]");
      busyButton(button, true);
      progress.hidden = false;
      topbar.start();
      // XMLHttpRequest, а не fetch: только он показывает ход отправки файла.
      var xhr = new XMLHttpRequest();
      xhr.open("POST", actionOf(form));
      xhr.setRequestHeader("X-CSRFToken", csrfToken());
      xhr.upload.addEventListener("progress", function (e) {
        if (!e.lengthComputable) return;
        var percent = Math.round(e.loaded * 100 / e.total);
        bar.style.width = percent + "%";
        text.textContent = percent < 100
          ? "Отправляю файл… " + percent + "%"
          : "Файл отправлен, читаю колонки…";
      });
      xhr.addEventListener("load", function () {
        topbar.done();
        if (xhr.status >= 400) {
          busyButton(button, false);
          toast("Не получилось: сервер ответил " + xhr.status, null, null, "error");
          return;
        }
        show(xhr.responseText, xhr.responseURL || window.location.href);
      });
      xhr.addEventListener("error", function () {
        topbar.done();
        busyButton(button, false);
        progress.hidden = true;
        toast("Не удалось отправить файл — проверьте соединение", null, null, "error");
      });
      xhr.send(new FormData(form));
    });
  }

  // ---------- Ожидание задачи ----------

  async function waitState(url, loader) {
    for (;;) {
      await sleep(POLL_MS);
      try {
        var result = await request(url);
        var state = result.body;
        if (!state.busy) return state;
        if (loader && state.label) loader.set(state.label + "…");
      } catch (error) {
        // Сеть мигнула — спросим ещё раз.
      }
    }
  }

  function initPolling(box) {
    var label = box.querySelector("[data-state-label]");
    var alive = true;
    cleanups.push(function () { alive = false; });
    waitState(box.getAttribute("data-state-url"), {
      set: function (value) { if (label) label.textContent = value; },
    }).then(function (state) {
      if (alive && state.next) go(state.next);
    });
  }

  // ---------- Кнопки записи и пересчёта ----------

  var RUN_TEXT = {
    known: ["Обновляю площадки в базе…", "Цены, метрики, описание — около 15–20 секунд на каталог."],
    new: ["Добавляю новые площадки…", "Несколько тысяч площадок — около 20–60 секунд."],
    recheck: ["Пересчитываю сводку…", "Около 10 секунд."],
    all: ["Записываю в базу…", "Прайс — секунды. Потом откроется разбор."],
  };

  function initRunForms(root) {
    root.querySelectorAll("form[data-run-form]").forEach(function (form) {
      form.addEventListener("submit", async function (event) {
        event.preventDefault();
        var kind = form.getAttribute("data-run-form");
        var button = form.querySelector("button[type=submit]");
        var panel = form.closest("[data-panel]") || form;
        var loader = inlineLoader(panel.querySelector("[data-loader]"), RUN_TEXT[kind][0], RUN_TEXT[kind][1]);
        // Пока идёт запись, остальные кнопки ждут: вторую запись сразу не начать.
        var snapshot = Array.prototype.map.call(root.querySelectorAll(".seo-run-btn"), function (b) {
          return [b, b.disabled];
        });
        snapshot.forEach(function (pair) { pair[0].disabled = true; });
        busyButton(button, true);
        topbar.start();
        var finished = false;
        try {
          var result = await request(actionOf(form), {
            method: "POST",
            headers: Object.assign({ "X-CSRFToken": csrfToken() }, AJAX),
            body: new FormData(form),
          });
          var state = await waitState(result.body.state_url, loader);
          if (state.status === "failed") throw new Error(state.error || "запись не удалась");
          if (kind === "all") { finished = true; go(form.getAttribute("data-done-url") || state.next); return; }
          await refreshCatalog();
          finished = true;
          var done = { known: "Площадки в базе обновлены", new: "Новые площадки добавлены", recheck: "Сводка пересчитана" }[kind];
          if (kind === "recheck") toast(done);
          else toast(done, "Открыть разбор", function () { go(window.location.pathname.replace("summary/", "review/")); });
        } catch (error) {
          toast("Не получилось: " + error.message, null, null, "error");
        } finally {
          loader.stop();
          topbar.done();
          button.classList.remove("seo-btn-busy");
          if (!(finished && kind === "all")) {
            snapshot.forEach(function (pair) {
              // Кнопку, которую обновила сводка, не трогаем — у неё уже свежее состояние.
              if (finished && pair[0].hasAttribute("data-swap")) return;
              pair[0].disabled = pair[1];
            });
          }
        }
      });
    });
  }

  // Каталог после записи: новые цифры, история, облачка — без перезагрузки,
  // фильтры и галочки не трогаются.
  async function refreshCatalog() {
    var blocks = document.querySelectorAll("[data-swap]");
    blocks.forEach(function (block) { block.classList.add("seo-refreshing"); });
    try {
      var result = await request(window.location.href);
      var doc = new DOMParser().parseFromString(await result.response.text(), "text/html");
      blocks.forEach(function (block) {
        var fresh = doc.querySelector('[data-swap="' + block.getAttribute("data-swap") + '"]');
        if (!fresh) return;
        block.innerHTML = fresh.innerHTML;
        block.hidden = fresh.hidden;
        if (fresh.hasAttribute("disabled")) block.setAttribute("disabled", "");
        else block.removeAttribute("disabled");
      });
      var total = doc.querySelector("[data-new-total]");
      document.querySelectorAll("[data-new-total]").forEach(function (node) {
        if (total) node.textContent = total.textContent;
      });
      var data = doc.getElementById("seo-filter-data");
      if (data && catalogApi) catalogApi.refresh(JSON.parse(data.textContent));
    } finally {
      blocks.forEach(function (block) { block.classList.remove("seo-refreshing"); });
    }
  }

  // ---------- Фильтры каталога ----------

  var catalogApi = null;

  function initCatalog(box) {
    var dataNode = document.getElementById("seo-filter-data");
    var fields = dataNode ? JSON.parse(dataNode.textContent) : [];
    var countUrl = box.getAttribute("data-count-url");
    var form = document.querySelector("[data-new-form]");
    var uploadId = form.getAttribute("data-upload");
    var storeKey = "seo-upload-filters:" + uploadId;
    var hidden = form.querySelector("[data-filters-json]");
    var passedNode = form.querySelector("[data-passed]");
    var droppedNode = form.querySelector("[data-dropped]");
    var textNode = form.querySelector("[data-filter-text]");
    var submit = form.querySelector("[data-new-submit]");
    var spec = {};
    var timer = null;
    var asked = 0;
    var widgets = {};
    var restoring = false;

    function number(text) {
      var cleaned = String(text === null || text === undefined ? "" : text).replace(/[\s €$]/g, "").replace(",", ".");
      if (!cleaned) return null;
      var value = Number(cleaned);
      return isFinite(value) ? value : null;
    }

    function changed() {
      hidden.value = JSON.stringify(spec);
      if (restoring) return;
      var store = storage();
      try { if (store) store.setItem(storeKey, hidden.value); } catch (error) { /* браузер не дал */ }
      clearTimeout(timer);
      timer = setTimeout(recount, COUNT_DELAY_MS);
    }

    async function recount() {
      var mine = ++asked;
      form.classList.add("seo-counting");
      try {
        var result = await postJson(countUrl, { filters: spec });
        if (mine !== asked) return; // пришёл ответ на старый запрос
        passedNode.textContent = result.passed.toLocaleString("ru-RU");
        var dropped = result.total - result.passed;
        droppedNode.textContent = dropped ? " · отсеется " + dropped.toLocaleString("ru-RU") : "";
        textNode.textContent = result.text;
        if (submit && !submit.classList.contains("seo-btn-busy")) submit.disabled = result.passed === 0;
      } catch (error) {
        textNode.textContent = "Не удалось пересчитать: " + error.message;
      } finally {
        if (mine === asked) form.classList.remove("seo-counting");
      }
    }

    function chip(text, onClick) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "seo-chip seo-chip-btn";
      button.textContent = text;
      button.addEventListener("click", onClick);
      return button;
    }

    function initAny(node, field) {
      var tags = node.querySelector("[data-tags]");
      var input = node.querySelector("[data-input]");
      var dropdown = node.querySelector("[data-dropdown]");
      var chipsNode = node.querySelector("[data-chips]");
      var chosen = [];
      var active = -1;

      function render() {
        tags.querySelectorAll(".seo-tag").forEach(function (tag) { tag.remove(); });
        chosen.forEach(function (value) {
          var tag = document.createElement("span");
          tag.className = "seo-tag";
          tag.textContent = value;
          var remove = document.createElement("button");
          remove.type = "button";
          remove.setAttribute("aria-label", "Убрать " + value);
          remove.textContent = "×";
          remove.addEventListener("click", function (event) { event.stopPropagation(); drop(value); });
          tag.appendChild(remove);
          tags.insertBefore(tag, input);
        });
        if (chosen.length) spec[field.key] = chosen.slice();
        else delete spec[field.key];
        changed();
      }
      function add(value) {
        value = String(value).trim();
        if (!value) return;
        var known = field.choices.find(function (c) { return c[0].toLowerCase() === value.toLowerCase(); });
        if (known) value = known[0];
        if (chosen.indexOf(value) === -1) chosen.push(value);
        input.value = "";
        render();
        // Список остаётся открытым: можно сразу выбрать следующее значение.
        if (document.activeElement === input) show();
      }
      function drop(value) {
        chosen = chosen.filter(function (v) { return v !== value; });
        render();
      }
      function hide() { dropdown.hidden = true; dropdown.textContent = ""; active = -1; }
      function show() {
        var query = input.value.trim().toLowerCase();
        var options = field.choices.filter(function (c) {
          return chosen.indexOf(c[0]) === -1 && (!query || c[0].toLowerCase().indexOf(query) !== -1);
        }).slice(0, DROPDOWN_LIMIT);
        dropdown.textContent = "";
        active = -1;
        if (!options.length) { hide(); return; }
        options.forEach(function (option) {
          var item = document.createElement("li");
          item.setAttribute("data-value", option[0]);
          item.textContent = option[0] + " ";
          var count = document.createElement("span");
          count.className = "seo-sub";
          count.textContent = "— " + option[1].toLocaleString("ru-RU");
          item.appendChild(count);
          item.addEventListener("mousedown", function (event) { event.preventDefault(); add(option[0]); });
          dropdown.appendChild(item);
        });
        dropdown.hidden = false;
      }
      function highlight(step) {
        var items = dropdown.querySelectorAll("li");
        if (!items.length) return;
        active = (active + step + items.length) % items.length;
        items.forEach(function (item, i) { item.classList.toggle("seo-active", i === active); });
        items[active].scrollIntoView({ block: "nearest" });
      }
      input.addEventListener("input", show);
      input.addEventListener("focus", show);
      input.addEventListener("click", show);
      // Щелчок по полосе прокрутки переводит фокус на список (tabindex=-1) —
      // тогда список остаётся открытым; закрываем, только если фокус ушёл совсем.
      dropdown.tabIndex = -1;
      function leave(event) {
        var to = event.relatedTarget;
        if (to === input || dropdown.contains(to)) return;
        hide();
      }
      input.addEventListener("blur", leave);
      dropdown.addEventListener("blur", leave);
      dropdown.addEventListener("keydown", function (event) {
        if (event.key === "Escape") { hide(); input.focus(); }
        else if (event.key.length === 1) input.focus();
      });
      input.addEventListener("keydown", function (event) {
        if (event.key === "ArrowDown") { event.preventDefault(); if (dropdown.hidden) show(); highlight(1); }
        else if (event.key === "ArrowUp") { event.preventDefault(); highlight(-1); }
        else if (event.key === "Enter") {
          event.preventDefault();
          var items = dropdown.querySelectorAll("li");
          var pick = active >= 0 ? items[active] : (input.value.trim() ? items[0] : null);
          add(pick ? pick.getAttribute("data-value") : input.value);
        } else if (event.key === "Backspace" && !input.value && chosen.length) {
          drop(chosen[chosen.length - 1]);
        } else if (event.key === "Escape") {
          hide();
        }
      });
      tags.addEventListener("click", function (event) {
        if (event.target === tags) { input.focus(); show(); }
      });
      function renderChips(items) {
        chipsNode.textContent = "";
        items.forEach(function (item) { chipsNode.appendChild(chip(item.text, function () { add(item.value); })); });
      }
      renderChips(field.chips);
      return {
        set: function (values) { chosen = values.slice(); render(); },
        refresh: function (fresh) { field.choices = fresh.choices; renderChips(fresh.chips); },
      };
    }

    function initRange(node, field) {
      var min = node.querySelector("[data-min]");
      var max = node.querySelector("[data-max]");
      var chipsNode = node.querySelector("[data-chips]");
      function read() {
        var low = number(min.value);
        var high = number(max.value);
        if (low === null && high === null) delete spec[field.key];
        else spec[field.key] = { min: low, max: high };
        changed();
      }
      min.addEventListener("input", read);
      max.addEventListener("input", read);
      function renderChips(items) {
        chipsNode.textContent = "";
        items.forEach(function (item) {
          chipsNode.appendChild(chip(item.text, function () {
            (item.bound === "max" ? max : min).value = item.value;
            read();
          }));
        });
      }
      renderChips(field.chips);
      return {
        set: function (rule) {
          min.value = rule && rule.min !== null && rule.min !== undefined ? rule.min : "";
          max.value = rule && rule.max !== null && rule.max !== undefined ? rule.max : "";
          read();
        },
        refresh: function (fresh) { renderChips(fresh.chips); },
      };
    }

    fields.forEach(function (field) {
      var node = box.querySelector('[data-filter="' + field.key + '"]');
      if (!node) return;
      widgets[field.key] = field.kind === "any" ? initAny(node, field) : initRange(node, field);
    });

    // Фильтр, выбранный раньше для этой загрузки, — из памяти браузера.
    var store = storage();
    var saved = null;
    try { saved = store ? JSON.parse(store.getItem(storeKey) || "null") : null; } catch (error) { saved = null; }
    if (saved && typeof saved === "object") {
      restoring = true;
      Object.keys(saved).forEach(function (key) { if (widgets[key]) widgets[key].set(saved[key]); });
      restoring = false;
      changed();
    }

    // Отправка не ждёт отложенного счётчика: записывается фильтр, что на экране.
    form.addEventListener("submit", function () { hidden.value = JSON.stringify(spec); }, true);
    catalogApi = {
      refresh: function (freshFields) {
        freshFields.forEach(function (fresh) { if (widgets[fresh.key]) widgets[fresh.key].refresh(fresh); });
        recount();
      },
    };
    cleanups.push(function () { catalogApi = null; clearTimeout(timer); });
  }

  // ---------- Разбор ----------

  var ACT_TEXT = {
    fix: "Рабочая цена сменилась",
    keep: "Оставлена прежняя цена",
    delete: "Удалено из базы",
    block: "Заблокировано",
  };

  function initReview(table) {
    var decideUrl = table.getAttribute("data-decide-url");
    var undoUrl = table.getAttribute("data-undo-url");
    var buttons = document.querySelector("template[data-decide-buttons]");
    var bulk = document.querySelector("[data-bulk]");
    var bulkCount = document.querySelector("[data-bulk-count]");
    var selectAll = document.querySelector("[data-select-all]");
    var decidable = !!document.querySelector('[data-bulk-act="fix"]');
    var rows = Array.prototype.slice.call(table.querySelectorAll("tbody tr[data-item]"));
    var focus = 0;
    var history = [];
    var busy = false;

    function rowOf(element) { return element.closest("tr[data-item]"); }
    function idOf(row) { return row.getAttribute("data-item"); }
    function pending(row) { return row.getAttribute("data-state") === "pending"; }
    function checkbox(row) { return row.querySelector("[data-select]"); }
    function selected() {
      return rows.filter(function (row) { var box = checkbox(row); return box && box.checked; });
    }

    function setFocus(index) {
      if (!rows.length) return;
      focus = Math.max(0, Math.min(rows.length - 1, index));
      rows.forEach(function (row, i) { row.classList.toggle("seo-focus", i === focus); });
      rows[focus].scrollIntoView({ block: "nearest" });
    }

    function syncBulk() {
      if (!bulk) return;
      var count = selected().length;
      bulk.hidden = count === 0;
      bulkCount.textContent = String(count);
    }

    function render(row, state) {
      row.setAttribute("data-state", state.state);
      if (state.state === "removed") {
        // Удалённая или заблокированная — уходит из разбора.
        row.classList.add("seo-removing");
        setTimeout(function () {
          row.hidden = true;
          rows = rows.filter(function (r) { return r !== row; });
          setFocus(Math.min(focus, rows.length - 1));
          syncBulk();
        }, 250);
        return;
      }
      row.hidden = false;
      row.classList.remove("seo-removing");
      if (rows.indexOf(row) === -1) {
        rows.push(row);
        rows.sort(function (a, b) {
          return a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1;
        });
      }
      row.classList.toggle("seo-done", state.state === "working" || state.state === "kept");
      var cell = row.querySelector("[data-decision]");
      cell.textContent = "";
      if (state.state === "pending" && buttons) {
        cell.appendChild(buttons.content.cloneNode(true));
        return;
      }
      var chipNode = document.createElement("span");
      chipNode.className = "seo-chip" + (state.state === "working" || state.state === "auto" ? " seo-down" : "");
      chipNode.textContent = state.label;
      cell.appendChild(chipNode);
    }

    function update(result) {
      Object.keys(result.rows || {}).forEach(function (id) {
        var row = table.querySelector('tr[data-item="' + id + '"]');
        if (row) render(row, result.rows[id]);
      });
      var done = document.querySelector("[data-done]");
      var need = document.querySelector("[data-need]");
      var bar = document.querySelector("[data-bar]");
      if (done) done.textContent = result.done;
      if (need) need.textContent = result.need;
      if (bar) bar.style.width = (result.need ? Math.round(result.done * 100 / result.need) : 100) + "%";
      Object.keys(result.tabs || {}).forEach(function (key) {
        var badge = document.querySelector('[data-tab="' + key + '"] [data-badge]');
        if (badge) badge.textContent = result.tabs[key];
      });
      syncBulk();
    }

    async function decide(targetRows, action) {
      var removing = action === "delete" || action === "block";
      var list = removing ? targetRows : targetRows.filter(pending);
      var ids = list.map(idOf);
      if (!ids.length || busy) return;
      busy = true;
      list.forEach(function (row) { row.classList.add("seo-busy-row"); });
      topbar.start();
      try {
        var result = await postJson(decideUrl, { action: action, items: ids });
        update(result);
        (result.problems || []).forEach(function (text) { toast(text, null, null, "error"); });
        if ((result.undo || []).length) {
          history.push(result.undo);
          var n = result.undo.length;
          toast(ACT_TEXT[action] + (n > 1 ? ": " + n : ""), "Отменить", undo);
        }
        list.forEach(function (row) { var box = checkbox(row); if (box) box.checked = false; });
        if (selectAll) selectAll.checked = false;
        syncBulk();
      } catch (error) {
        toast("Не получилось: " + error.message, null, null, "error");
      } finally {
        busy = false;
        topbar.done();
        list.forEach(function (row) { row.classList.remove("seo-busy-row"); });
      }
    }

    async function undo() {
      var last = history.pop();
      if (!last || busy) return;
      busy = true;
      topbar.start();
      try {
        var result = await postJson(undoUrl, { undo: last });
        update(result);
        toast("Решение отменено");
      } catch (error) {
        history.push(last);
        toast("Не получилось отменить: " + error.message, null, null, "error");
      } finally {
        busy = false;
        topbar.done();
      }
    }

    table.addEventListener("click", function (event) {
      var button = event.target.closest("[data-act]");
      if (!button) return;
      var row = rowOf(button);
      setFocus(rows.indexOf(row));
      decide([row], button.getAttribute("data-act"));
    });
    table.addEventListener("change", function (event) {
      if (event.target.matches("[data-select]")) syncBulk();
    });
    if (selectAll) {
      selectAll.addEventListener("change", function () {
        rows.forEach(function (row) { var box = checkbox(row); if (box) box.checked = selectAll.checked; });
        syncBulk();
      });
    }
    if (bulk) {
      bulk.addEventListener("click", function (event) {
        var button = event.target.closest("[data-bulk-act]");
        if (!button) return;
        var act = button.getAttribute("data-bulk-act");
        if (act === "clear") {
          rows.forEach(function (row) { var box = checkbox(row); if (box) box.checked = false; });
          if (selectAll) selectAll.checked = false;
          syncBulk();
          return;
        }
        decide(selected(), act);
      });
    }

    function onKey(event) {
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      var target = event.target instanceof Element ? event.target : null;
      if (target && target.closest("input, textarea, select, [contenteditable]")) return;
      if (document.documentElement.classList.contains("seo-panel-open")) return; // открыта панель
      var key = event.key.toLowerCase();
      var row = rows[focus];
      if (key === "j" || key === "о") setFocus(focus + 1);
      else if (key === "k" || key === "л") setFocus(focus - 1);
      else if ((key === "a" || key === "ф") && row && decidable) decide([row], "fix");
      else if ((key === "s" || key === "ы") && row && decidable) decide([row], "keep");
      else if ((key === "d" || key === "в") && row) decide([row], "delete");
      else if ((key === "b" || key === "и") && row) decide([row], "block");
      else if ((key === "x" || key === "ч") && row) {
        var box = checkbox(row);
        if (box) { box.checked = !box.checked; syncBulk(); }
      } else if ((key === "o" || key === "щ") && row) {
        var link = row.querySelector("[data-open]");
        if (link) link.click();
      } else if (key === "u" || key === "г") undo();
      else return;
      event.preventDefault();
    }
    document.addEventListener("keydown", onKey);
    // Цену поменяли в карточке площадки — разбор перечитает общая подгрузка
    // (seo/panel.js → seoNav.reload()), с прокруткой на месте.
    cleanups.push(function () {
      document.removeEventListener("keydown", onKey);
    });
    setFocus(0);
  }

  // ---------- Сборка страницы ----------

  function initPage() {
    var root = document.querySelector("main#content-start") || document;
    var form = root.querySelector("[data-upload-form]");
    if (form) initForm(form);
    var box = root.querySelector("[data-state-url]");
    if (box) initPolling(box);
    initRunForms(root);
    var filters = root.querySelector("[data-filters]");
    if (filters) initCatalog(filters);
    var table = root.querySelector("table[data-review]");
    if (table) initReview(table);
  }

  // Экран показан (и первый раз, и после подгрузки) — навесить обработчики;
  // уходим с экрана — снять (seo/soft-nav.js).
  document.addEventListener("seo:load", initPage);
  document.addEventListener("seo:unload", function () {
    cleanups.splice(0).forEach(function (fn) { fn(); });
  });
})();
