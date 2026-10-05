/* Меню «?» — справка по открытому экрану (E9-07, ADR-056).
 *
 * Кнопку рисует тег {% screen_help %} (config/user_docs.py) у названия экрана
 * и в шапке панели записи. Здесь — только меню, когда страниц про экран
 * несколько: щелчок по «?» открывает список, щелчок мимо, по пункту или Esc —
 * закрывает. Пункты — обычные ссылки в новую вкладку, их открывает браузер.
 * Обработчики — на document: экраны меняются без перезагрузки (soft-nav.js),
 * а навешивать заново на каждую новую кнопку не нужно.
 */
(function () {
  "use strict";

  if (window.seoHelp) return;
  window.seoHelp = true;

  function listOf(menu) { return menu.querySelector(".seo-help-list"); }
  function buttonOf(menu) { return menu.querySelector("button"); }

  function close(menu) {
    listOf(menu).hidden = true;
    buttonOf(menu).setAttribute("aria-expanded", "false");
  }

  function closeAll(except) {
    document.querySelectorAll("[data-seo-help]").forEach(function (menu) {
      if (menu !== except) close(menu);
    });
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest && event.target.closest("[data-seo-help] > button");
    if (!button) {
      closeAll(null);
      return;
    }
    var menu = button.parentNode;
    var opening = listOf(menu).hidden;
    closeAll(menu);
    listOf(menu).hidden = !opening;
    button.setAttribute("aria-expanded", String(opening));
  });

  // На window и при погружении — раньше панели записи: Esc сначала закрывает
  // меню, а панель под ним остаётся открытой (panel.js смотрит defaultPrevented).
  window.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    var open = document.querySelector("[data-seo-help] .seo-help-list:not([hidden])");
    if (!open) return;
    event.preventDefault();
    var menu = open.closest("[data-seo-help]");
    close(menu);
    buttonOf(menu).focus();
  }, true);

  document.addEventListener("seo:unload", function () { closeAll(null); });
})();
