/* Блокировка правки (ADR-077): площадку и размещения правит один человек.
 *
 * Метка — admin/seo_lock.html в карточке площадки, форме размещения и решении:
 * data-seo-lock — что блокирует запись, data-seo-lock-text — кто её держит
 * (тогда запись пришла только для просмотра), data-seo-lock-mine — держит сам.
 *
 * - Чужая блокировка — окошко в правом нижнем углу: «Площадку правит Анна с
 *   14:05 — пока только просмотр». Висит, пока запись открыта.
 * - Блокировку берёт сервер, когда запись открыли. Своя простояла дольше
 *   EDIT_LOCK — её возьмёт первая правка в форме (ввод, выбор); держит другой —
 *   окошко, запись перечитывается только для просмотра. Заметка и наборы
 *   условий серости ([data-lock-free]) — не правка записи.
 * - Пока запись открыта — раз в POLL_MS спросить, чья она. Освободилась —
 *   запись перечитывается: свежие данные, можно править. Её взял другой —
 *   окошко и просмотр. Своя — продлить, пока человек в ней работает.
 * - Закрыли панель, ушли со страницы — снять свою блокировку: сервер снимает
 *   только свою, поэтому снимаем всегда — блокировку мог взять и сам сервер
 *   при записи формы.
 *
 * Панель (seo/panel.js) зовёт attach(своё содержимое) после показа записи и
 * detach() при закрытии; запись страницей — по seo:load (seo/soft-nav.js).
 */
(function () {
  "use strict";

  if (window.seoLock) return;

  // Как часто открытая запись спрашивает, чья она, и как часто продлевать свою
  // блокировку, пока человек в ней работает. Срок простоя — настройка EDIT_LOCK.
  var POLL_MS = 20000;
  var TOUCH_MS = 60000;

  // Открытая запись: key — что блокирует, url — куда спрашивать, root — где
  // она на экране, text — чужая блокировка, mine — держим сами.
  var state = null;
  var alertItem = null;

  function csrfToken() {
    var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  async function call(action) {
    if (!state) return null;
    var key = state.key;
    var url = state.url;
    var init = { credentials: "same-origin", headers: { "Accept": "application/json" } };
    if (action) {
      var body = new FormData();
      body.append("key", key);
      body.append("action", action);
      init.method = "POST";
      init.body = body;
      init.headers["X-CSRFToken"] = csrfToken();
    } else {
      url += (url.indexOf("?") === -1 ? "?" : "&") + "key=" + encodeURIComponent(key);
    }
    try {
      var response = await fetch(url, init);
      if (!response.ok) return null;
      var data = await response.json();
      // Пока шёл ответ, открыли другую запись — он уже не о ней.
      return state && state.key === key ? data : null;
    } catch (error) {
      return null;
    }
  }

  function release(key, url) {
    var body = new FormData();
    body.append("key", key);
    body.append("action", "release");
    body.append("csrfmiddlewaretoken", csrfToken());
    // sendBeacon доходит и когда вкладку закрывают.
    if (navigator.sendBeacon && navigator.sendBeacon(url, body)) return;
    fetch(url, { method: "POST", body: body, credentials: "same-origin", keepalive: true })
      .catch(function () {});
  }

  // ---------- Окошко ----------

  function showAlert(text) {
    if (!alertItem || !alertItem.isConnected) {
      var box = document.querySelector(".seo-toast-box");
      if (!box) {
        box = document.createElement("div");
        box.className = "seo-toast-box";
        box.setAttribute("role", "status");
        document.body.appendChild(box);
      }
      alertItem = document.createElement("div");
      alertItem.className = "seo-toast seo-toast-warning seo-lock-alert";
      alertItem.setAttribute("data-seo-lock-alert", "");
      box.appendChild(alertItem);
    }
    alertItem.textContent = text;
  }

  function hideAlert() {
    if (alertItem && alertItem.isConnected) alertItem.remove();
    alertItem = null;
  }

  function freeText() {
    return (state && state.key.indexOf("site:") === 0 ? "Площадка свободна" : "Размещения свободны")
      + " — данные обновлены, можно править.";
  }

  // Запись заново: в панели — панель, иначе страница. Правки, сделанные поверх
  // чужой блокировки, не сохранятся всё равно — их не спрашиваем.
  function reloadRecord() {
    if (window.seoPanel && window.seoPanel.isOpen() && state && state.inPanel) {
      window.seoPanel.reload();
    } else if (window.seoNav && window.seoNav.enabled) {
      window.seoNav.reload();
    } else {
      window.location.reload();
    }
  }

  // Ответ сервера: чья запись теперь.
  function apply(data) {
    if (!state || !data) return;
    var was = state.text;
    state.mine = Boolean(data.mine);
    state.text = data.text || "";
    if (state.mine) state.touched = Date.now();
    if (state.text) {
      showAlert(state.text);
      // Взял другой — запись только для просмотра.
      if (!was) reloadRecord();
    } else {
      hideAlert();
      if (was) {
        if (window.seoNav) window.seoNav.toast(freeText(), "success");
        reloadRecord();
      }
    }
  }

  function poll() {
    if (!state || document.hidden || state.taking) return;
    call("").then(apply);
  }

  // ---------- Правка и работа в записи ----------

  // Правит человек, а не скрипт: выбор с флагами и поля с поиском сами шлют
  // change при открытии записи — это не правка.
  function inRecord(event) {
    var target = event.target instanceof Element ? event.target : null;
    return Boolean(event.isTrusted && state && target && state.root && state.root.contains(target)
      && target.closest("form") && !target.closest("[data-lock-free]"));
  }

  // Первая правка — взять блокировку; держит другой — окошко и просмотр.
  function onEdit(event) {
    if (!inRecord(event) || state.text || state.mine || state.taking) return;
    state.taking = true;
    var key = state.key;
    call("take").then(function (data) {
      if (state && state.key === key) state.taking = false;
      apply(data);
    });
  }

  // Человек работает в записи — продлить свою блокировку, не чаще TOUCH_MS.
  function onActivity(event) {
    if (!event.isTrusted || !state || !state.mine || !state.root) return;
    var target = event.target instanceof Element ? event.target : null;
    if (!target || !state.root.contains(target)) return;
    if (Date.now() - state.touched < TOUCH_MS) return;
    state.touched = Date.now();
    call("touch").then(apply);
  }

  document.addEventListener("input", onEdit, true);
  document.addEventListener("change", onEdit, true);
  document.addEventListener("keydown", onActivity, true);
  document.addEventListener("pointerdown", onActivity, true);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) poll();
  });
  window.addEventListener("pagehide", function () {
    if (state) release(state.key, state.url);
  });

  // ---------- Открыть и закрыть запись ----------

  function attach(root, inPanel) {
    var el = root ? root.querySelector("[data-seo-lock]") : null;
    if (!el) {
      detach();
      return;
    }
    var key = el.getAttribute("data-seo-lock");
    // Другая запись — свою блокировку отпустить; та же (вкладка, ответ формы
    // карточки) — держать дальше.
    if (state && state.key !== key) detach();
    if (!state) state = { key: key, timer: setInterval(poll, POLL_MS), touched: 0 };
    state.root = root;
    state.inPanel = Boolean(inPanel);
    state.url = el.getAttribute("data-seo-lock-url");
    state.mine = el.hasAttribute("data-seo-lock-mine");
    state.text = el.getAttribute("data-seo-lock-text") || "";
    state.taking = false;
    if (state.mine) state.touched = Date.now();
    if (state.text) showAlert(state.text);
    else hideAlert();
  }

  function detach() {
    if (!state) return;
    clearInterval(state.timer);
    release(state.key, state.url);
    state = null;
    hideAlert();
  }

  // Запись страницей (карточка или форма без панели) — её метка в #content.
  function attachPage() {
    if (window.seoPanel && window.seoPanel.isOpen()) return;
    attach(document.getElementById("content"), false);
  }

  document.addEventListener("seo:load", attachPage);
  document.addEventListener("seo:unload", detach);
  if (document.readyState !== "loading") attachPage();
  else document.addEventListener("DOMContentLoaded", attachPage);

  window.seoLock = {
    attach: function (root) { attach(root, true); },
    detach: detach,
    attachPage: attachPage,
    poll: poll,
  };
})();
