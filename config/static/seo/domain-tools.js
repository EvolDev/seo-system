/* Кнопка «скопировать» у домена (E2-06): `<button data-copy="example.com">`.
 *
 * Значки рисует сервер (apps/sites/display.py): в «Площадках» рядом с доменом,
 * в заголовке карточки площадки. Скрипт один на все страницы — обработчик на
 * document, новое содержимое (подгрузка, панель) его не теряет.
 *
 * Копирует через буфер обмена браузера; где он недоступен (страница не по
 * https и не localhost) — через скрытое поле. Скопировано — значок на две
 * секунды становится галочкой.
 */
(function () {
  "use strict";

  if (window.seoDomainTools) return;
  window.seoDomainTools = true;

  var DONE_MS = 2000;
  var CHECK = '<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" fill="none"'
    + ' stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">'
    + '<path d="M5 12l5 5L20 7"/></svg>';

  function fallback(text) {
    var field = document.createElement("textarea");
    field.value = text;
    field.setAttribute("readonly", "");
    field.style.position = "fixed";
    field.style.opacity = "0";
    document.body.appendChild(field);
    field.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (error) { ok = false; }
    field.remove();
    return ok ? Promise.resolve() : Promise.reject(new Error("copy"));
  }

  function copy(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).catch(function () { return fallback(text); });
    }
    return fallback(text);
  }

  function done(button) {
    if (!button.dataset.icon) button.dataset.icon = button.innerHTML;
    button.innerHTML = CHECK;
    button.classList.add("seo-copied");
    clearTimeout(button.seoCopyTimer);
    button.seoCopyTimer = setTimeout(function () {
      button.innerHTML = button.dataset.icon;
      button.classList.remove("seo-copied");
    }, DONE_MS);
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest && event.target.closest("[data-copy]");
    if (!button) return;
    event.preventDefault();
    event.stopPropagation();
    copy(button.dataset.copy).then(
      function () { done(button); },
      function () {
        if (window.seoNav) window.seoNav.toast("Не удалось скопировать: " + button.dataset.copy, "error");
      }
    );
  });
})();
