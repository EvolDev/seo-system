/* Страница выдачи Google: оценка «About N results» и примеры адресов (E2-06).
 *
 * Работает только на вкладке, которую ждёт карточка площадки: сначала
 * спрашивает фоновую часть, ждёт ли какая-нибудь открытая карточка этот запрос
 * (`seo-gray-expects`). Обычные поиски человека не трогает.
 *
 * Оценка — в `#result-stats`. Google прячет её за кнопкой «Tools»: числа нет —
 * скрипт нажимает «Tools» сам и ждёт строку до 6 секунд. «did not match any
 * documents» — ноль. Не нашлось — в карточку уходит count: null, и она просит
 * вписать число руками. С выдачи серых тем — до 10 адресов найденных страниц.
 */
(function () {
  "use strict";

  var WAIT_MS = 6000;
  var SAMPLES = 10;
  var TOOLS = ["Tools", "Инструменты", "Інструменти"];

  var query = new URLSearchParams(window.location.search).get("q");
  if (!query) return;

  chrome.runtime.sendMessage({ type: "seo-gray-expects", query: query }, function (reply) {
    if (chrome.runtime.lastError || !reply || !reply.kind) return;
    readCount(function (count) {
      chrome.runtime.sendMessage({
        type: "seo-gray-reading",
        query: query,
        count: count,
        urls: reply.kind === "gray" ? sampleUrls() : [],
      });
      badge(count);
    });
  });

  function statsText() {
    var stats = document.getElementById("result-stats");
    return stats && /\d/.test(stats.textContent) ? stats.textContent : null;
  }

  function noResults() {
    return document.body.textContent.indexOf("did not match any documents") !== -1;
  }

  function toolsButton() {
    var byId = document.getElementById("hdtb-tls");
    if (byId) return byId;
    var nodes = document.querySelectorAll('[role="button"], button, a');
    for (var i = 0; i < nodes.length; i += 1) {
      if (TOOLS.indexOf(nodes[i].textContent.trim()) !== -1) return nodes[i];
    }
    return null;
  }

  function readCount(done) {
    var text = statsText();
    if (text) {
      done(seoGrayCount(text));
      return;
    }
    if (noResults()) {
      done(0);
      return;
    }
    var finished = false;
    var observer = new MutationObserver(check);
    var timer = setTimeout(function () { finish(null); }, WAIT_MS);
    function finish(value) {
      if (finished) return;
      finished = true;
      observer.disconnect();
      clearTimeout(timer);
      done(value);
    }
    function check() {
      var found = statsText();
      if (found) finish(seoGrayCount(found));
    }
    observer.observe(document.body, { childList: true, subtree: true, characterData: true });
    var button = toolsButton();
    if (button) button.click();
    check();
  }

  // Адрес результата: прямой или через переадресацию Google /url?q=…
  function resultUrl(link) {
    var href = link.href;
    try {
      var url = new URL(href);
      if (/(^|\.)google\./.test(url.hostname)) href = url.pathname === "/url" ? url.searchParams.get("q") || "" : "";
    } catch (error) {
      return "";
    }
    return /^https?:\/\//.test(href) ? href : "";
  }

  function sampleUrls() {
    var urls = [];
    document.querySelectorAll("#search a[href] h3, #rso a[href] h3").forEach(function (title) {
      var href = resultUrl(title.closest("a"));
      if (href && urls.indexOf(href) === -1 && urls.length < SAMPLES) urls.push(href);
    });
    return urls;
  }

  function badge(count) {
    var box = document.createElement("div");
    box.textContent = typeof count === "number"
      ? "SEO-система: " + count.toLocaleString("ru-RU") + " — в карточку площадки"
      : "SEO-система: оценки «About N results» нет — впишите число в карточке руками";
    box.setAttribute("style", [
      "position:fixed", "right:16px", "bottom:16px", "z-index:2147483647",
      "padding:10px 14px", "border-radius:8px", "font:14px/1.4 system-ui,sans-serif",
      "color:#fff", "background:" + (typeof count === "number" ? "#17803a" : "#8a5a00"),
      "box-shadow:0 4px 16px rgba(0,0,0,.25)",
    ].join(";"));
    document.body.appendChild(box);
    setTimeout(function () { box.remove(); }, 6000);
  }
})();
