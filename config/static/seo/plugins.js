/* Экран «Плагины» (ADR-084): стоит ли расширение в этом браузере и какой версии.
 *
 * Расширение (browser-extension/relay.js) на страницах системы кладёт свою версию
 * в атрибут data-seo-extension у <html>: страница и скрипт расширения видят один
 * DOM. Скрипт расширения запускается, когда страница уже загружена, — поэтому
 * ждём атрибут до трёх секунд. Версии до 1.2.0 атрибута не ставят.
 */
(function () {
  "use strict";

  var WAIT_MS = 3000;

  function parts(version) {
    return String(version || "").split(".").map(function (part) { return parseInt(part, 10) || 0; });
  }

  // -1 — a старее b, 0 — одинаковые, 1 — a новее.
  function compare(a, b) {
    var left = parts(a), right = parts(b);
    for (var i = 0; i < Math.max(left.length, right.length); i += 1) {
      var diff = (left[i] || 0) - (right[i] || 0);
      if (diff) return diff < 0 ? -1 : 1;
    }
    return 0;
  }

  function show(card, installed) {
    var status = card.querySelector("[data-plugin-status]");
    var latest = card.dataset.version;
    card.classList.remove("is-current", "is-outdated", "is-missing");
    if (!installed) {
      card.classList.add("is-missing");
      status.textContent = "В этом браузере расширения не видно: не установлено, выключено, " +
        "вкладку не перезагрузили после установки или стоит версия до 1.2.0. Скачайте архив ниже.";
    } else if (compare(installed, latest) < 0) {
      card.classList.add("is-outdated");
      status.textContent = "У вас стоит версия " + installed + ", здесь — " + latest +
        ". Обновите: скачайте архив и распакуйте поверх старой папки.";
    } else {
      card.classList.add("is-current");
      status.textContent = "У вас стоит версия " + installed + " — актуальная.";
    }
  }

  function init() {
    var card = document.querySelector('[data-plugin="gray"]');
    if (!card || card.dataset.checked) return;
    card.dataset.checked = "1";
    var html = document.documentElement;
    var observer = null;
    var timer = null;
    function done() {
      if (observer) observer.disconnect();
      clearTimeout(timer);
      show(card, html.dataset.seoExtension);
    }
    if (html.dataset.seoExtension) {
      done();
      return;
    }
    observer = new MutationObserver(function () {
      if (html.dataset.seoExtension) done();
    });
    observer.observe(html, { attributes: true, attributeFilter: ["data-seo-extension"] });
    timer = setTimeout(done, WAIT_MS);
  }

  document.addEventListener("seo:load", init);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init, { once: true });
  else init();
})();
