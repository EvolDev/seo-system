/* Серость в Google — раздел карточки площадки (E2-06, ADR-054).
 *
 * 1. Доля и зона пересчитываются, пока человек вписывает числа. Правило зон —
 *    как на сервере (apps/sites/gray_scan.py): меньше green — зелёная, до
 *    yellow включительно — жёлтая, больше — красная; доля — два знака.
 * 2. Расширение браузера (browser-extension/) присылает «About N results» со
 *    страницы Google сообщением окна
 *    `{ type: "seo-gray-reading", query, count, urls }`. Число встаёт в поле
 *    того запроса, который открыла кнопка раздела (data-total-query или
 *    data-gray-query); примеры адресов — с выдачи серых. Пришли оба числа от
 *    расширения — форма отправляется сама, панель покажет новую карточку.
 *    count не число — Google не показал оценку: подсказка просит вписать руками.
 *
 * Скрипт стоит в разметке раздела и выполняется при каждом показе карточки:
 * обработчики на document и window навешиваются один раз, пересчёт — каждый.
 */
(function () {
  "use strict";

  var ZONE_TITLES = { green: "зелёная зона", yellow: "жёлтая зона", red: "красная зона" };

  function norm(text) {
    return String(text || "").replace(/\s+/g, " ").trim();
  }

  function sections() {
    return document.querySelectorAll("[data-gray-scan]");
  }

  function number(input) {
    var value = input ? input.value.trim() : "";
    return /^\d+$/.test(value) ? parseInt(value, 10) : null;
  }

  function zoneOf(ratio, section) {
    var green = parseFloat(section.dataset.green);
    var yellow = parseFloat(section.dataset.yellow);
    if (isNaN(green) || isNaN(yellow)) return null;
    if (ratio < green) return "green";
    return ratio <= yellow ? "yellow" : "red";
  }

  function update(section) {
    var form = section.querySelector(".seo-gray-form");
    if (!form) return;
    var out = form.querySelector("[data-gray-result]");
    var total = number(form.elements.total);
    var gray = number(form.elements.gray);
    out.textContent = "";
    out.className = "seo-gray-result";
    if (total === null || gray === null) return;
    if (total === 0) {
      out.textContent = "в индексе 0 страниц — доли нет";
      return;
    }
    var ratio = Math.round((Math.min(gray, total) * 10000) / total) / 100;
    var text = ratio.toFixed(2).replace(/\.?0+$/, "").replace(".", ",") + " %";
    var zone = zoneOf(ratio, section);
    out.textContent = "→ " + text + (zone ? " · " + ZONE_TITLES[zone] : "");
    if (zone) out.classList.add("seo-gray-zone", "seo-gray-" + zone);
  }

  function hint(section, text, warn) {
    var line = section.querySelector("[data-gray-hint]");
    if (!line) return;
    line.textContent = text;
    line.classList.toggle("seo-gray-hint-warn", Boolean(warn));
  }

  function receive(data) {
    var query = norm(data.query);
    sections().forEach(function (section) {
      var kind = norm(section.dataset.totalQuery) === query ? "total"
        : norm(section.dataset.grayQuery) === query ? "gray" : null;
      var form = section.querySelector(".seo-gray-form");
      if (!kind || !form) return;
      var what = kind === "total" ? "всего страниц" : "серых";
      if (typeof data.count !== "number" || data.count < 0) {
        hint(section, "Google не показал оценку «About N results» (" + what + "): впишите число руками.", true);
        return;
      }
      var input = form.elements[kind];
      input.value = String(data.count);
      input.dataset.fromExtension = "1";
      input.classList.add("seo-gray-filled");
      if (kind === "gray") form.elements.sample_urls.value = (data.urls || []).join("\n");
      update(section);
      if (form.elements.total.dataset.fromExtension && form.elements.gray.dataset.fromExtension) {
        form.elements.source.value = "extension";
        form.requestSubmit();
        return;
      }
      hint(section, kind === "total"
        ? "Всего — из Google. Теперь «Google: серые темы»."
        : "Серых — из Google. Теперь «Google: всего страниц».");
    });
  }

  if (!window.seoGrayReady) {
    window.seoGrayReady = true;
    document.addEventListener("input", function (event) {
      var input = event.target;
      if (!input.matches || !input.matches("[data-gray-input]")) return;
      // Поправили руками — число уже не от расширения: само не отправится.
      delete input.dataset.fromExtension;
      input.classList.remove("seo-gray-filled");
      var section = input.closest("[data-gray-scan]");
      if (section) update(section);
    });
    window.addEventListener("message", function (event) {
      var data = event.data;
      if (event.source !== window || !data || data.type !== "seo-gray-reading") return;
      receive(data);
    });
  }
  sections().forEach(update);
})();
