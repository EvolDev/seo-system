/* Сделки Collaborator и сверка с размещениями (E11-01, ADR-079, ADR-080).
 *
 * «Обновить из Collaborator» (кнопка над «Размещениями» и на экране сверки):
 * POST …/collaborator/refresh/ ставит задачу и отдаёт ключ, дальше скрипт
 * спрашивает ход по ключу (…/refresh/status/?key=) и показывает окно внизу
 * справа: «Забираю сделки: страница 3 из 10», в конце — итог и «Открыть сверку».
 * Без ключа API сервер сразу отвечает, чего не хватает.
 *
 * Экран сверки: вкладки групп (выбранная — в адресе после #, переживает
 * перечитывание экрана), «Заполнить отмеченные», «Взять из Collaborator»,
 * «Оставить как в системе», «Вернуть в сверку» — запросом без перезагрузки,
 * итог — сообщением, после — экран перечитывается (seoNav.reload).
 *
 * Панель размещения: «Заполнить из Collaborator» ставит значения сделки в поля
 * формы и подсвечивает их — сохраняет человек. «Поставить «Размещено»» и
 * «Поставить «Отказ»» в сверке открывают размещение панелью и нажимают нужную
 * кнопку статуса (дата ставится сама, как от щелчка); у «Отказ» курсор — в
 * комментарии: почему, пишет человек.
 */
(function () {
  "use strict";

  if (window.seoCollaborator) return;
  window.seoCollaborator = true;

  var POLL_MS = 1000;
  var GIVE_UP_MS = 3 * 60 * 1000;
  // Задача так долго не началась — воркер не запущен?
  var QUEUED_WARN_MS = 30 * 1000;
  var DONE_MS = 15000;
  // Нажали «Поставить «…»» — столько ждём, пока панель откроет размещение.
  var PENDING_MS = 15000;

  function nav() { return window.seoNav || null; }

  function csrfToken() {
    var input = document.querySelector("input[name=csrfmiddlewaretoken]");
    if (input) return input.value;
    var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  async function post(url, form) {
    var response = await fetch(url, {
      method: "POST",
      body: form || new FormData(),
      credentials: "same-origin",
      headers: { "X-CSRFToken": csrfToken(), "X-Requested-With": "XMLHttpRequest" },
    });
    var data = {};
    try { data = await response.json(); } catch (error) { data = {}; }
    if (!response.ok && !data.error) data.error = "Сервер ответил " + response.status + ".";
    return data;
  }

  function sleep(ms) { return new Promise(function (resolve) { setTimeout(resolve, ms); }); }

  // --- Окно хода «Обновить из Collaborator» ---

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

  function windowToast() {
    var item = document.createElement("div");
    var body = document.createElement("div");
    var head = document.createElement("strong");
    var line = document.createElement("span");
    var link = document.createElement("a");
    var close = document.createElement("button");
    var timer = null;
    body.className = "seo-toast-text";
    link.className = "seo-collab-toast-link";
    link.hidden = true;
    close.type = "button";
    close.className = "seo-toast-close";
    close.setAttribute("aria-label", "Закрыть");
    close.textContent = "×";
    close.addEventListener("click", function () { item.remove(); });
    link.addEventListener("click", function () { item.remove(); });
    body.appendChild(head);
    body.appendChild(line);
    body.appendChild(link);
    item.appendChild(body);
    item.appendChild(close);
    toastBox().appendChild(item);
    return {
      set: function (kind, title, text, href, label) {
        item.className = "seo-toast is-" + kind;
        head.textContent = title;
        line.textContent = text || "";
        link.hidden = !href;
        if (href) { link.href = href; link.textContent = label || "Открыть"; }
        if (timer) clearTimeout(timer);
        timer = null;
        if (kind === "success" || kind === "warning") {
          timer = setTimeout(function () { item.remove(); }, DONE_MS);
        }
      },
    };
  }

  var refreshing = false;

  async function refresh(button) {
    if (refreshing) return;
    refreshing = true;
    button.disabled = true;
    var box = windowToast();
    box.set("info", "Collaborator", "Ставлю в очередь…");
    try {
      var started = await post(button.getAttribute("data-url"));
      if (started.error) {
        box.set("error", "Collaborator", started.error);
        return;
      }
      await watch(box, button.getAttribute("data-status-url"), started.key);
    } catch (error) {
      box.set("error", "Collaborator", "Не удалось связаться с сервером: " + error.message);
    } finally {
      refreshing = false;
      button.disabled = false;
    }
  }

  async function watch(box, statusUrl, key) {
    var since = Date.now();
    var onScreen = Boolean(document.querySelector("[data-collab]"));
    while (Date.now() - since < GIVE_UP_MS) {
      await sleep(POLL_MS);
      var url = statusUrl + (statusUrl.indexOf("?") === -1 ? "?" : "&") + "key=" + encodeURIComponent(key);
      var state;
      try {
        var response = await fetch(url, { credentials: "same-origin" });
        state = await response.json();
      } catch (error) {
        continue;
      }
      if (state.state === "done") {
        box.set("success", "Сделки забраны", state.text || "", onScreen ? "" : state.url, "Открыть сверку");
        if (onScreen && nav()) nav().reload();
        return;
      }
      if (state.state === "failed") {
        box.set("error", "Collaborator", state.error || "Забрать сделки не удалось.");
        return;
      }
      if (state.state === "running" && state.pages) {
        box.set("info", "Collaborator", "Забираю сделки: страница " + state.page + " из " + state.pages + "…");
      } else if (Date.now() - since > QUEUED_WARN_MS) {
        box.set("warning", "Collaborator", "Задача ждёт в очереди — запущен ли воркер?");
      } else {
        box.set("info", "Collaborator", "Жду очередь…");
      }
    }
    box.set("error", "Collaborator", "Итога нет слишком долго. Загляните в «Запуски задач».");
  }

  // --- Экран сверки ---

  function screen() { return document.querySelector("[data-collab]"); }

  function setupTabs(root) {
    var tabs = Array.prototype.slice.call(root.querySelectorAll("[data-collab-tab]"));
    var panels = Array.prototype.slice.call(root.querySelectorAll("[data-collab-panel]"));
    if (!tabs.length) return;
    root.classList.add("has-tabs");
    // Вкладка — в адресе после # («#fill»): не id раздела, чтобы браузер не
    // прокручивал к нему и шапка с вкладками оставалась на виду.
    var wanted = window.location.hash.replace(/^#/, "");
    var known = tabs.some(function (tab) { return tab.getAttribute("data-collab-tab") === wanted; });
    if (!known) {
      // Первая вкладка, где есть что делать.
      var busy = tabs.filter(function (tab) {
        var count = tab.querySelector("b");
        return count && count.textContent.trim() !== "0";
      });
      wanted = (busy[0] || tabs[0]).getAttribute("data-collab-tab");
    }
    show(tabs, panels, wanted);
  }

  function show(tabs, panels, name) {
    tabs.forEach(function (tab) {
      var on = tab.getAttribute("data-collab-tab") === name;
      tab.classList.toggle("is-active", on);
      tab.setAttribute("aria-selected", on ? "true" : "false");
    });
    panels.forEach(function (panel) { panel.hidden = panel.getAttribute("data-collab-panel") !== name; });
  }

  function picked(root) {
    return Array.prototype.slice.call(root.querySelectorAll("[data-collab-fill-form] input[name=deal]:checked"));
  }

  function updatePicked(root) {
    var note = root.querySelector("[data-collab-picked]");
    if (note) {
      var count = picked(root).length;
      note.textContent = count ? "отмечено: " + count : "отметьте строки";
    }
  }

  async function act(url, form) {
    var data = await post(url, form);
    var n = nav();
    if (!n) return;
    if (data.error) {
      n.toast(data.error, "error");
      return;
    }
    var action = data.undo_url
      ? { label: "Отменить…", run: function () { n.visit(data.undo_url); } }
      : undefined;
    n.toast(data.message || "Готово.", data.level || "success", action);
    n.reload();
  }

  document.addEventListener("click", function (event) {
    var target = event.target instanceof Element ? event.target : null;
    if (!target) return;
    var refreshButton = target.closest("[data-collab-refresh]");
    if (refreshButton) {
      event.preventDefault();
      refresh(refreshButton);
      return;
    }
    var root = screen();
    var tab = target.closest("[data-collab-tab]");
    if (tab && root && root.contains(tab)) {
      event.preventDefault();
      var name = tab.getAttribute("data-collab-tab");
      show(
        Array.prototype.slice.call(root.querySelectorAll("[data-collab-tab]")),
        Array.prototype.slice.call(root.querySelectorAll("[data-collab-panel]")),
        name
      );
      history.replaceState(history.state, "", "#" + name);
      return;
    }
    var take = target.closest("[data-collab-take]");
    if (take && root) {
      event.preventDefault();
      take.disabled = true;
      var form = new FormData();
      form.append("deal", take.getAttribute("data-deal"));
      form.append("field", take.getAttribute("data-field"));
      act(root.getAttribute("data-take-url"), form);
      return;
    }
    var keep = target.closest("[data-collab-keep]");
    if (keep && root) {
      event.preventDefault();
      keep.disabled = true;
      var data = new FormData();
      data.append("deal", keep.getAttribute("data-deal"));
      data.append("field", keep.getAttribute("data-field"));
      if (keep.getAttribute("data-back") === "1") data.append("back", "1");
      act(root.getAttribute("data-keep-url"), data);
      return;
    }
    var fill = target.closest("[data-collab-fill-form-panel]");
    if (fill) {
      event.preventDefault();
      fillForm(fill);
    }
    // Захват: вкладки — ссылки, и подгрузка страниц (soft-nav.js) не должна их увидеть.
  }, true);

  // «Поставить «…»» — запомнить, какую кнопку статуса нажать в открывшейся панели.
  var pending = null;
  document.addEventListener("click", function (event) {
    var target = event.target instanceof Element ? event.target : null;
    var link = target && target.closest("[data-collab-status]");
    if (link) {
      pending = { status: link.getAttribute("data-collab-status"), url: link.getAttribute("href"), at: Date.now() };
    }
  }, true);

  function applyPending() {
    if (!pending || Date.now() - pending.at > PENDING_MS) {
      pending = null;
      return;
    }
    var panel = document.querySelector(".seo-panel");
    var radio = panel && panel.querySelector('input[type=radio][name=status][value="' + pending.status + '"]');
    if (!radio) return;
    var wanted = pending.status;
    pending = null;
    if (!radio.checked) radio.click();
    if (wanted === "rejected") {
      var comment = panel.querySelector("textarea[name=comment]");
      if (comment) {
        comment.scrollIntoView({ block: "center" });
        comment.focus();
      }
    }
  }

  document.addEventListener("change", function (event) {
    var target = event.target;
    var root = screen();
    if (!root || !(target instanceof HTMLInputElement)) return;
    if (target.matches("[data-collab-all]")) {
      root.querySelectorAll("[data-collab-fill-form] input[name=deal]").forEach(function (box) {
        box.checked = target.checked;
      });
    }
    if (target.matches("[data-collab-all], [data-collab-fill-form] input[name=deal]")) updatePicked(root);
  });

  document.addEventListener("submit", function (event) {
    var form = event.target instanceof HTMLFormElement ? event.target : null;
    var root = screen();
    if (!form || !root || !form.matches("[data-collab-fill-form]")) return;
    event.preventDefault();
    event.stopPropagation();
    var boxes = picked(root);
    var n = nav();
    if (!boxes.length) {
      if (n) n.toast("Отметьте строки, которые заполнить.", "warning");
      return;
    }
    var data = new FormData();
    boxes.forEach(function (box) { data.append("deal", box.value); });
    var button = form.querySelector("[data-collab-fill]");
    if (button) button.disabled = true;
    act(root.getAttribute("data-fill-url"), data);
  }, true);

  // --- Панель размещения: «Заполнить из Collaborator» ---

  function fillForm(button) {
    var values;
    try { values = JSON.parse(button.getAttribute("data-values") || "{}"); } catch (error) { values = {}; }
    var form = button.closest("form") || document.querySelector(".seo-panel form");
    if (!form) return;
    var changed = 0;
    Object.keys(values).forEach(function (name) {
      var field = form.querySelector('[name="' + name + '"]');
      if (!field || field.disabled) return;
      var value = String(values[name]);
      if (field.value === value) return;
      field.value = value;
      field.classList.add("seo-collab-filled");
      field.dispatchEvent(new Event("input", { bubbles: true }));
      field.dispatchEvent(new Event("change", { bubbles: true }));
      changed += 1;
    });
    var n = nav();
    if (n) {
      n.toast(
        changed
          ? "Заполнено из Collaborator: " + changed + ". Проверьте и сохраните — «Размещено» → «Сохранить»."
          : "Поля уже как на Collaborator.",
        changed ? "success" : "info"
      );
    }
  }

  function init() {
    var root = screen();
    if (root) {
      setupTabs(root);
      updatePicked(root);
    }
    applyPending();
  }

  document.addEventListener("seo:load", init);
  // Вкладка в адресе сменилась («Назад», ссылка с #…) — показать её.
  window.addEventListener("hashchange", function () {
    var root = screen();
    if (root) setupTabs(root);
  });
  // Панель показала запись (seo/panel.js) — нажать статус, если просили.
  document.addEventListener("seo:panel", applyPending);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
