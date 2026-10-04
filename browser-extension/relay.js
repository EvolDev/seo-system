/* Вкладка админки SEO-системы: передаёт числа из Google в карточку площадки (E2-06).
 *
 * Отвечает фоновой части, ждёт ли открытая здесь карточка этот запрос:
 * у раздела «Серость в Google» запросы в data-total-query и data-gray-query.
 * Число для такого запроса отдаёт странице сообщением окна
 * `{ type: "seo-gray-reading", … }` — его принимает seo/gray-scan.js.
 * Записывает замер уже сама карточка, под входом человека.
 */
(function () {
  "use strict";

  function norm(text) {
    return String(text || "").replace(/\s+/g, " ").trim();
  }

  function kindFor(query) {
    var wanted = norm(query);
    var sections = document.querySelectorAll("[data-gray-scan]");
    for (var i = 0; i < sections.length; i += 1) {
      if (norm(sections[i].dataset.totalQuery) === wanted) return "total";
      if (norm(sections[i].dataset.grayQuery) === wanted) return "gray";
    }
    return null;
  }

  chrome.runtime.onMessage.addListener(function (message, sender, reply) {
    if (!message || typeof message.query !== "string") return;
    if (message.type === "seo-gray-expects") {
      reply({ kind: kindFor(message.query) });
      return;
    }
    if (message.type === "seo-gray-reading" && kindFor(message.query)) {
      window.postMessage(
        {
          type: "seo-gray-reading",
          query: message.query,
          count: message.count,
          urls: Array.isArray(message.urls) ? message.urls : [],
        },
        window.location.origin
      );
    }
  });
})();
