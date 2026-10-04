/* Окно выгрузки над списком (E1-06, ADR-053).
 *
 * Разметку рисует сервер (config/export_tags.py, admin/seo_export_tools.html),
 * скрипт:
 * - «Выгрузить ▾» открывает и закрывает окно; щелчок мимо и Esc — закрывают;
 * - «Все колонки» ставит и снимает все галочки и сама показывает «частично»;
 * - строки: отмечены галочками в списке — уходят только они; «Выбрать все N»
 *   или ничего не отмечено — все отобранные;
 * - Excel и CSV — отправка формы на адрес формата с номерами отмеченных
 *   строк. Ответ — файл: его сохраняет soft-nav.js, страница остаётся на месте
 *   (без soft-nav браузер скачает файл сам).
 * Сервер запоминает снятые галочки в сессии — в следующий раз окно откроется
 * с ними.
 *
 * Обработчики — на document, один раз: окно приходит с каждым экраном заново.
 */
(function () {
  "use strict";

  if (window.seoExport) return;
  window.seoExport = true;

  // Номера отмеченных строк списка; null — «выбраны все N» (все отобранные).
  function picked() {
    var across = document.querySelector("#changelist-form input[name=select_across]");
    if (across && across.value === "1") return null;
    var boxes = document.querySelectorAll("#result_list input.action-select:checked");
    return Array.prototype.map.call(boxes, function (box) { return box.value; });
  }

  function rowsText(form) {
    var ids = picked();
    if (ids && ids.length) return "Строки: отмеченные — " + ids.length;
    return "Строки: все отобранные — " + form.dataset.total;
  }

  function columns(form) {
    return Array.prototype.slice.call(form.querySelectorAll("input[name=columns]"));
  }

  function sync(form) {
    var boxes = columns(form);
    var on = boxes.filter(function (box) { return box.checked; }).length;
    var all = form.querySelector("[data-export-all]");
    all.checked = on === boxes.length;
    all.indeterminate = on > 0 && on < boxes.length;
    form.querySelector("[data-export-error]").hidden = on > 0;
  }

  function open(box) {
    var form = box.querySelector("[data-export-form]");
    form.querySelector("[data-export-rows]").textContent = rowsText(form);
    sync(form);
    form.hidden = false;
    box.querySelector("[data-export-toggle]").setAttribute("aria-expanded", "true");
  }

  function close(box) {
    box.querySelector("[data-export-form]").hidden = true;
    box.querySelector("[data-export-toggle]").setAttribute("aria-expanded", "false");
  }

  function openBoxes() {
    var forms = document.querySelectorAll("[data-export-form]:not([hidden])");
    return Array.prototype.map.call(forms, function (form) { return form.closest("[data-export]"); });
  }

  document.addEventListener("click", function (event) {
    var toggle = event.target.closest && event.target.closest("[data-export-toggle]");
    if (toggle) {
      // Ctrl и средняя кнопка — ссылка как есть: Excel со всеми строками.
      if (event.ctrlKey || event.metaKey || event.shiftKey || event.button !== 0) return;
      event.preventDefault();
      var box = toggle.closest("[data-export]");
      if (box.querySelector("[data-export-form]").hidden) open(box);
      else close(box);
      return;
    }
    openBoxes().forEach(function (box) {
      if (!box.contains(event.target)) close(box);
    });
  });

  document.addEventListener("change", function (event) {
    var target = event.target;
    var form = target.closest && target.closest("[data-export-form]");
    if (form) {
      if (target.hasAttribute("data-export-all")) {
        columns(form).forEach(function (box) { box.checked = target.checked; });
      }
      sync(form);
      return;
    }
    if (target.matches && target.matches("#result_list input.action-select, #action-toggle")) {
      // «Отметить все» на странице меняет строки своим обработчиком — после него.
      setTimeout(function () {
        openBoxes().forEach(function (box) {
          var opened = box.querySelector("[data-export-form]");
          opened.querySelector("[data-export-rows]").textContent = rowsText(opened);
        });
      }, 0);
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") openBoxes().forEach(close);
  });

  // На document — раньше перехвата форм в soft-nav.js (он на window): адрес и
  // строки подставляются до того, как он прочтёт форму.
  document.addEventListener("submit", function (event) {
    var form = event.target.closest && event.target.closest("[data-export-form]");
    if (!form) return;
    if (!columns(form).some(function (box) { return box.checked; })) {
      event.preventDefault();
      form.querySelector("[data-export-error]").hidden = false;
      return;
    }
    var format = (event.submitter && event.submitter.dataset.format) || "xlsx";
    form.setAttribute("action", format === "csv" ? form.dataset.csv : form.dataset.xlsx);
    form.querySelectorAll("input[data-export-picked]").forEach(function (input) { input.remove(); });
    var ids = picked();
    var values = ids === null ? [["select_across", "1"]] : ids.map(function (id) { return ["ids", id]; });
    values.forEach(function (pair) {
      var input = document.createElement("input");
      input.type = "hidden";
      input.name = pair[0];
      input.value = pair[1];
      input.setAttribute("data-export-picked", "");
      form.appendChild(input);
    });
    // Закрыть — после того, как soft-nav.js заберёт форму.
    var box = form.closest("[data-export]");
    setTimeout(function () { close(box); }, 0);
  });
})();
