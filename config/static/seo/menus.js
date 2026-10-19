/* Выпадающие меню на <details data-seo-menu> (ADR-073): «Период ▾» над
 * списками, «Ещё N ▾» во вкладках площадки.
 *
 * Обычный <details> закрывается только своей же кнопкой; меню — ещё щелчком
 * мимо, выбором пункта и Esc, а открытое закрывает остальные. После перехода
 * без перезагрузки (seo:load, seo/soft-nav.js) меню закрыты: открытость
 * поставил не сервер, и подгрузка её бы оставила. Прочие <details> (фильтры,
 * «Оформление») не трогаем. Esc ловится на window до панели (seo/panel.js),
 * чтобы закрыть меню, а не саму панель.
 */
(function () {
  "use strict";

  var MENU = "details[data-seo-menu]";

  function closeAll(except) {
    document.querySelectorAll(MENU + "[open]").forEach(function (menu) {
      if (menu !== except) menu.removeAttribute("open");
    });
  }

  document.addEventListener("click", function (event) {
    var menu = event.target.closest ? event.target.closest(MENU) : null;
    // Пункт меню — ссылка: выбрали, меню больше не нужно.
    if (menu && event.target.closest("a[href]")) menu = null;
    closeAll(menu);
  });

  document.addEventListener("seo:load", function () {
    closeAll(null);
  });

  window.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    var open = document.querySelector(MENU + "[open]");
    if (!open) return;
    event.stopPropagation();
    open.removeAttribute("open");
    var summary = open.querySelector("summary");
    if (summary) summary.focus();
  }, true);
})();
