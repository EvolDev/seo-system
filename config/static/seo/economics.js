/* «Экономика» (Работа → Экономика): подсказки графиков и отбор размещений.
 *
 * - Наведение или фокус на месяце графика (`[data-tip]`) — подсказка у курсора:
 *   строки текста, первая — месяц.
 * - Кнопки «строки ↓» у плиток и «показать» в «Что не посчитано»
 *   (`data-econ-show`), «облачка» над таблицей (`data-econ-filter`) — отбирают
 *   строки размещений периода по `data-kind` и `data-flags`, без запроса.
 *
 * Слушатели — на документе: работают и после подгрузки экрана (seo/soft-nav.js),
 * и в HTML-файле отчёта, куда этот скрипт вставлен целиком.
 */
(function () {
  "use strict";

  if (window.seoEconomics) return;
  window.seoEconomics = true;

  var tip = null;

  function tipBox() {
    if (tip && document.body.contains(tip)) return tip;
    tip = document.createElement("div");
    tip.className = "seo-econ-tip";
    tip.setAttribute("role", "tooltip");
    tip.hidden = true;
    var root = document.querySelector("[data-econ]") || document.body;
    root.appendChild(tip);
    return tip;
  }

  function show(target, x, y) {
    var box = tipBox();
    var lines = (target.getAttribute("data-tip") || "").split("\n");
    box.textContent = "";
    lines.forEach(function (line, index) {
      var row = document.createElement(index === 0 ? "b" : "div");
      row.textContent = line;
      box.appendChild(row);
    });
    box.hidden = false;
    var width = box.offsetWidth;
    var height = box.offsetHeight;
    var left = x + 14;
    if (left + width > window.innerWidth - 8) left = x - width - 14;
    var top = y - height - 12;
    if (top < 8) top = y + 18;
    box.style.left = Math.max(8, left) + "px";
    box.style.top = top + "px";
  }

  function hide() {
    if (tip) tip.hidden = true;
  }

  function tipTarget(event) {
    var target = event.target;
    return target instanceof Element ? target.closest("[data-econ] [data-tip]") : null;
  }

  document.addEventListener("pointermove", function (event) {
    var target = tipTarget(event);
    if (target) show(target, event.clientX, event.clientY);
    else hide();
  });
  document.addEventListener("focusin", function (event) {
    var target = tipTarget(event);
    if (!target) return;
    var rect = target.getBoundingClientRect();
    show(target, rect.left + rect.width / 2, rect.top + 40);
  });
  document.addEventListener("focusout", hide);
  // Прокрутка колесом или пальцем уводит месяц из-под курсора — подсказку прячем.
  // Не по «scroll»: тот приходит и от прокрутки к элементу, уже после показа.
  window.addEventListener("wheel", hide, { passive: true });
  window.addEventListener("touchmove", hide, { passive: true });
  document.addEventListener("seo:unload", function () {
    hide();
    tip = null;
    if (watched) watched.disconnect();
  });

  // Подписи графиков — 12 px при любой ширине: --econ-k — единиц рисунка на пиксель.
  var watched = typeof ResizeObserver === "function" ? new ResizeObserver(function (entries) {
    entries.forEach(function (entry) { fit(entry.target); });
  }) : null;

  function fit(svg) {
    var width = svg.getBoundingClientRect().width;
    var box = svg.viewBox && svg.viewBox.baseVal;
    if (!width || !box || !box.width) return;
    svg.style.setProperty("--econ-k", (box.width / width).toFixed(3));
  }

  function init() {
    document.querySelectorAll(".seo-econ-svg").forEach(function (svg) {
      fit(svg);
      if (watched) watched.observe(svg);
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
  document.addEventListener("seo:load", init);

  function matches(row, key) {
    if (key === "all") return true;
    if (key === "spent" || key === "planned") return row.getAttribute("data-kind") === key;
    return (" " + (row.getAttribute("data-flags") || "") + " ").indexOf(" " + key + " ") >= 0;
  }

  function filterRows(section, key) {
    var shown = 0;
    section.querySelectorAll("tbody tr[data-kind]").forEach(function (row) {
      var visible = matches(row, key);
      row.hidden = !visible;
      if (visible) shown += 1;
    });
    section.querySelectorAll("[data-econ-filter]").forEach(function (chip) {
      chip.setAttribute("aria-pressed", chip.getAttribute("data-econ-filter") === key ? "true" : "false");
    });
    var none = section.querySelector("[data-econ-none]");
    if (none) none.hidden = shown > 0;
  }

  document.addEventListener("click", function (event) {
    var target = event.target instanceof Element ? event.target : null;
    if (!target) return;
    var chip = target.closest("[data-econ-filter]");
    var more = target.closest("[data-econ-show]");
    if (!chip && !more) return;
    // Ссылка внутри плитки — своё действие, не отбор строк.
    var link = target.closest("a");
    if (more && link && more.contains(link)) return;
    var root = target.closest("[data-econ]");
    var section = root ? root.querySelector("[data-econ-rows]") : null;
    if (!section) return;
    event.preventDefault();
    if (chip) {
      filterRows(section, chip.getAttribute("data-econ-filter"));
      return;
    }
    filterRows(section, more.getAttribute("data-econ-show"));
    section.scrollIntoView({ behavior: "smooth", block: "start" });
  });

  // Плитка-кнопка — и с клавиатуры: Enter и пробел.
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Enter" && event.key !== " ") return;
    var target = event.target instanceof Element ? event.target : null;
    if (!target || !target.matches(".seo-econ-tile[data-econ-show]")) return;
    event.preventDefault();
    target.click();
  });
})();
