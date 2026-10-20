/* Поиск столбиком доменов (admin/search_form.html): у списков, где поиск
 * понимает несколько доменов сразу («Размещения», «Площадки», «Каталог
 * площадок»), поле поиска — <textarea data-domain-search>.
 *
 * Высота — по числу строк, до `data-max-rows` (дальше прокрутка): вставили
 * столбик — поле раскрылось. Enter — «Найти», как в однострочном поле;
 * Shift+Enter — новая строка. Отправку перехватывает seo/soft-nav.js —
 * список меняется без перезагрузки, а высоту нового поля ставит сервер
 * (`rows`). Обработчики — на document: подгрузка их не теряет.
 */
(function () {
  "use strict";

  if (window.seoDomainSearch) return;
  window.seoDomainSearch = true;

  var BOX = "textarea[data-domain-search]";

  function isBox(target) {
    return Boolean(target && target.matches && target.matches(BOX));
  }

  document.addEventListener("input", function (event) {
    var box = event.target;
    if (!isBox(box)) return;
    var lines = box.value.split("\n").length;
    box.rows = Math.min(lines, Number(box.dataset.maxRows) || lines);
  });

  document.addEventListener("keydown", function (event) {
    var box = event.target;
    if (!isBox(box) || event.key !== "Enter" || event.shiftKey || event.isComposing) return;
    event.preventDefault();
    if (box.form) box.form.requestSubmit();
  });
})();
