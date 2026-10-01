/* Проверка индексации без перезагрузки страницы (E2-03).
 *
 * Откуда запускается: кнопка ↻ в колонке «в индексе», действие «Проверить
 * индексацию» для отмеченных строк списка, кнопка в карточке. Всё — одна
 * пачка проверок:
 * 1. POST …/check-indexation/ с `ids` — сервер ставит проверки в очередь и
 *    отвечает id задач;
 * 2. скрипт спрашивает состояние всей пачки одним запросом
 *    (…/indexation-status/?t=<размещение>:<задача>…): сначала раз в 1,5 с,
 *    через полминуты — раз в 5 с;
 * 3. готовая проверка сразу обновляет свою строку или поля карточки.
 *
 * Окна в правом нижнем углу: пока идёт — «Проверяю…» с ходом и последней
 * неудачной попыткой; в конце — итог: зелёное — всё в индексе, жёлтое —
 * не в индексе или пауза, красное — проверку выполнить не удалось.
 */
(function () {
  "use strict";

  var POLL_FAST_MS = 1500;
  var POLL_SLOW_MS = 5000;
  var FAST_FOR_MS = 30 * 1000;
  // Ни одна проверка так долго не началась — воркер не запущен?
  var QUEUED_WARN_MS = 60 * 1000;
  // Повторы после неудач идут через 1 и 2 минуты; дольше не ждём на странице.
  var GIVE_UP_MS = 6 * 60 * 1000;
  var TOAST_MS = 8000;
  var NAMES_SHOWN = 5;

  function csrfToken() {
    var input = document.querySelector("input[name=csrfmiddlewaretoken]");
    if (input) return input.value;
    var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  function toastBox() {
    var box = document.querySelector(".seo-toasts");
    if (!box) {
      box = document.createElement("div");
      box.className = "seo-toasts";
      box.setAttribute("role", "status");
      box.setAttribute("aria-live", "polite");
      document.body.appendChild(box);
    }
    return box;
  }

  // Окно, которое можно менять на месте. kind: info | success | warning | error.
  // info висит, пока идёт проверка; error — пока не закроют; остальные гаснут сами.
  function toast(kind, title, text) {
    var item = document.createElement("div");
    var body = document.createElement("div");
    var head = document.createElement("strong");
    var line = document.createElement("span");
    var close = document.createElement("button");
    var timer = null;
    body.className = "seo-toast-text";
    close.type = "button";
    close.className = "seo-toast-close";
    close.setAttribute("aria-label", "Закрыть");
    close.textContent = "×";
    close.addEventListener("click", function () { item.remove(); });
    body.appendChild(head);
    body.appendChild(line);
    item.appendChild(body);
    item.appendChild(close);
    toastBox().appendChild(item);

    function set(newKind, newTitle, newText) {
      item.className = "seo-toast is-" + newKind;
      head.textContent = newTitle;
      line.textContent = newText || "";
      if (timer) clearTimeout(timer);
      timer = null;
      if (newKind === "success" || newKind === "warning") {
        timer = setTimeout(function () { item.remove(); }, TOAST_MS);
      }
    }
    set(kind, title, text);
    return { set: set };
  }

  function sleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  async function fetchJson(url, options) {
    var response = await fetch(url, Object.assign({
      credentials: "same-origin",
      headers: { "Accept": "application/json", "X-CSRFToken": csrfToken() },
    }, options || {}));
    var data = {};
    try { data = await response.json(); } catch (e) { /* не JSON — ниже ошибка по коду */ }
    if (!response.ok) {
      throw new Error(data.error || "Сервер ответил " + response.status + ".");
    }
    return data;
  }

  function plural(n, one, few, many) {
    var mod10 = n % 10;
    var mod100 = n % 100;
    if (mod10 === 1 && mod100 !== 11) return one;
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
    return many;
  }

  function names(items) {
    var list = items.slice(0, NAMES_SHOWN).map(function (item) { return item.name; });
    var rest = items.length - list.length;
    return list.join(", ") + (rest > 0 ? " и ещё " + rest : "");
  }

  // items: [{id, name, view}]; checkUrl/statusUrl — адреса пачки; history — для
  // карточки; noUrl — сколько отмеченных строк без адреса статьи не попало в пачку.
  async function run(items, checkUrl, statusUrl, history, noUrl) {
    var single = items.length === 1;
    var total = items.length;
    items.forEach(function (item) { item.view.busy(true); });
    var progress = toast(
      "info",
      single ? "Проверяю индексацию: " + items[0].name : "Проверяю индексацию: 0 из " + total,
      "Поиск в Google по адресу статьи."
    );

    var started;
    try {
      var form = new FormData();
      items.forEach(function (item) { form.append("ids", item.id); });
      started = await fetchJson(checkUrl, { method: "POST", body: form });
    } catch (error) {
      progress.set("error", "Проверка не запущена", error.message);
      items.forEach(function (item) { item.view.busy(false); });
      return;
    }

    var pending = [];
    items.forEach(function (item) {
      item.task = started.tasks[item.id];
      if (item.task) pending.push(item);
      else item.view.busy(false); // без адреса статьи — сервер не поставил
    });
    var skipped = total - pending.length + (noUrl || 0);
    var toCheck = pending.length;
    var done = [];
    var begin = Date.now();
    var lastError = "";

    while (pending.length) {
      var elapsed = Date.now() - begin;
      if (elapsed > GIVE_UP_MS) break;
      await sleep(elapsed < FAST_FOR_MS ? POLL_FAST_MS : POLL_SLOW_MS);
      var query = pending.map(function (item) {
        return "t=" + encodeURIComponent(item.id + ":" + item.task);
      });
      if (history) query.push("history=1");
      var answer;
      try {
        answer = await fetchJson(statusUrl + "?" + query.join("&"));
      } catch (error) {
        continue; // сеть моргнула — спросим ещё раз
      }
      var started_any = false;
      pending = pending.filter(function (item) {
        var state = answer.items[item.id];
        if (!state) return true;
        if (state.state !== "queued") started_any = true;
        var errors = state.errors || [];
        if (errors.length) lastError = errors[errors.length - 1].error;
        if (state.state === "success" || state.state === "failed" || state.state === "waiting") {
          item.state = state;
          item.view.done(state);
          item.view.busy(false);
          done.push(item);
          return false;
        }
        return true;
      });
      if (!pending.length) break;
      var text = lastError
        ? "Неудачная попытка: " + lastError + " Повтор — автоматически."
        : "Поиск в Google по адресу статьи.";
      if (!started_any && !done.length && Date.now() - begin > QUEUED_WARN_MS) {
        text = "Проверка больше минуты ждёт в очереди — похоже, не запущен воркер." +
          " Подробности — «Служебное» → «Запуски задач».";
      }
      progress.set(
        "info",
        single
          ? "Проверяю индексацию: " + items[0].name
          : "Проверяю индексацию: " + done.length + " из " + toCheck,
        text
      );
    }

    pending.forEach(function (item) { item.view.busy(false); });
    finish(progress, done, pending, skipped, single);
  }

  function finish(progress, done, unfinished, skipped, single) {
    var failed = done.filter(function (item) { return item.state.state === "failed"; });
    var paused = done.filter(function (item) { return item.state.state === "waiting"; });
    var checked = done.filter(function (item) { return item.state.state === "success"; });
    var notIndexed = checked.filter(function (item) { return item.state.indexed === false; });
    var indexed = checked.length - notIndexed.length;
    var tail = skipped ? " Без адреса статьи, не проверены: " + skipped + "." : "";

    if (failed.length) {
      progress.set(
        "error",
        single
          ? "Не удалось проверить: " + failed[0].name
          : "Не удалось проверить: " + failed.length + " из " + (done.length + unfinished.length),
        failed[0].state.error + (single ? "" : " (" + names(failed) + ")") + tail
      );
      return;
    }
    if (unfinished.length) {
      progress.set(
        "warning",
        "Проверка идёт дольше обычного: готово " + done.length + " из " +
          (done.length + unfinished.length),
        "Остальные результаты появятся на странице после обновления." + tail
      );
      return;
    }
    if (paused.length) {
      var waiting = paused[0].state.waiting;
      progress.set(
        "warning",
        "Проверка на паузе до " + waiting.until,
        waiting.reason + ". Выполнится сама, результат появится на странице позже." + tail
      );
      return;
    }
    if (!checked.length) {
      progress.set("warning", "Проверять нечего", "У выбранных размещений нет адреса статьи.");
      return;
    }
    if (single) {
      var item = checked[0];
      if (item.state.indexed) {
        progress.set("success", item.name + " — в индексе", "Проверено " + item.state.checked_at + ".");
      } else {
        progress.set("warning", item.name + " — нет в индексе", "Проверено " + item.state.checked_at + ".");
      }
      return;
    }
    var title = "Проверено " + checked.length + " " +
      plural(checked.length, "размещение", "размещения", "размещений");
    if (!notIndexed.length) {
      progress.set("success", title + ": все в индексе", tail.trim());
    } else {
      progress.set(
        "warning",
        title + ": в индексе " + indexed + ", нет в индексе " + notIndexed.length,
        "Нет в индексе: " + names(notIndexed) + "." + tail
      );
    }
  }

  // Строка списка: ячейки «в индексе» и «индексация проверена».
  function rowView(button) {
    var id = button.getAttribute("data-placement");
    var value = document.querySelector('[data-indexed="' + id + '"]');
    var checkedAt = document.querySelector('[data-indexed-at="' + id + '"]');
    var before = value ? value.innerHTML : "";
    return {
      busy: function (on) {
        button.disabled = on;
        button.classList.toggle("is-busy", on);
        if (value && on) value.innerHTML = '<span class="seo-busy-text">проверяю…</span>';
        if (value && !on && value.querySelector(".seo-busy-text")) value.innerHTML = before;
      },
      done: function (state) {
        if (value) value.innerHTML = before = state.indexed_html;
        if (checkedAt) checkedAt.textContent = state.checked_at;
      },
    };
  }

  function rowItem(button) {
    return {
      id: button.getAttribute("data-placement"),
      name: button.getAttribute("data-name"),
      view: rowView(button),
    };
  }

  // Карточка: поля только для чтения и история проверок.
  function cardView(form) {
    var button = form.querySelector("button");
    var label = button.textContent;
    function field(name) {
      return document.querySelector(".field-" + name + " .readonly");
    }
    return {
      busy: function (on) {
        button.disabled = on;
        button.textContent = on ? "Проверяю…" : label;
      },
      done: function (state) {
        var indexed = field("is_indexed");
        var checkedAt = field("indexed_checked_at");
        var history = field("indexation_history");
        if (indexed) indexed.innerHTML = state.indexed_html;
        if (checkedAt) checkedAt.textContent = state.checked_at;
        if (history && state.history_html) history.innerHTML = state.history_html;
      },
    };
  }

  // Кнопка ↻ в строке.
  document.addEventListener("click", function (event) {
    var button = event.target.closest(".seo-index-check");
    if (!button || button.disabled) return;
    event.preventDefault();
    run(
      [rowItem(button)],
      button.getAttribute("data-check-url"),
      button.getAttribute("data-status-url"),
      false
    );
  });

  document.addEventListener("submit", function (event) {
    // Кнопка в карточке.
    var card = event.target.closest("form[data-indexation-form]");
    if (card) {
      event.preventDefault();
      if (card.querySelector("button").disabled) return;
      run(
        [{ id: card.getAttribute("data-placement"), name: card.getAttribute("data-name"),
           view: cardView(card) }],
        card.getAttribute("data-check-url"),
        card.getAttribute("data-status-url"),
        true
      );
      return;
    }
    // Действие «Проверить индексацию» для отмеченных строк списка.
    var list = event.target.closest("#changelist-form");
    if (!list) return;
    var action = list.querySelector("select[name=action]");
    var across = list.querySelector("input[name=select_across]");
    if (!action || action.value !== "check_indexation_action") return;
    // «Выбрать все на всех страницах» — строк нет на экране: обычный путь с переходом.
    if (across && across.value === "1") return;
    var selected = Array.prototype.map.call(
      list.querySelectorAll("input[name=_selected_action]:checked"),
      function (box) { return box.value; }
    );
    if (!selected.length) return; // Django сам скажет, что ничего не выбрано
    event.preventDefault();
    var buttons = [];
    selected.forEach(function (id) {
      var button = list.querySelector('.seo-index-check[data-placement="' + id + '"]');
      if (button && !button.disabled) buttons.push(button);
    });
    if (!buttons.length) {
      toast("warning", "Проверять нечего", "У выбранных размещений нет адреса статьи.");
      return;
    }
    run(
      buttons.map(rowItem),
      buttons[0].getAttribute("data-check-url"),
      buttons[0].getAttribute("data-status-url"),
      false,
      selected.length - buttons.length
    );
  });
})();
