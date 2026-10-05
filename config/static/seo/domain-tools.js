/* Кнопка «скопировать» у домена (E2-06): `<button data-copy="example.com">`.
 *
 * Значки рисует сервер (apps/sites/display.py): в «Площадках» рядом с доменом,
 * в заголовке карточки площадки. Скрипт один на все страницы — обработчик на
 * document, новое содержимое (подгрузка, панель) его не теряет.
 *
 * Пачкой — `<button data-copy-domains>` в строке действий любого списка
 * (admin/actions.html) и на разборе загрузки: домены отмеченных строк
 * столбиком, по одному на строке. Домен строки ищется по порядку: `data-domain`
 * (его ставит кнопка копирования), потом `data-copy`, потом первая ячейка,
 * похожая на домен, — так пачка работает и в списках, где домен приходит
 * простым текстом («Размещения», «Метрики», «Ссылающиеся домены»).
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
    if (button.hasAttribute("data-copy-domains")) {
      if (!button.dataset.label) button.dataset.label = button.textContent;
      button.textContent = "Скопировано";
      button.classList.add("seo-copied");
      clearTimeout(button.seoCopyTimer);
      button.seoCopyTimer = setTimeout(function () {
        button.textContent = button.dataset.label;
        button.classList.remove("seo-copied");
      }, DONE_MS);
      return;
    }
    if (!button.dataset.icon) button.dataset.icon = button.innerHTML;
    button.innerHTML = CHECK;
    button.classList.add("seo-copied");
    clearTimeout(button.seoCopyTimer);
    button.seoCopyTimer = setTimeout(function () {
      button.innerHTML = button.dataset.icon;
      button.classList.remove("seo-copied");
    }, DONE_MS);
  }

  var DOMAIN = /^[a-z0-9][a-z0-9-]*(\.[a-z0-9-]+)+$/i;

  function plural(count) {
    var tens = count % 100;
    var ones = count % 10;
    if (tens > 10 && tens < 20) return " доменов";
    if (ones === 1) return " домен";
    if (ones >= 2 && ones <= 4) return " домена";
    return " доменов";
  }

  function rowDomain(row) {
    var marked = row.querySelector("[data-domain]");
    if (marked) return marked.getAttribute("data-domain");
    var copy = row.querySelector("[data-copy]");
    if (copy) return copy.getAttribute("data-copy");
    var cells = row.querySelectorAll("td, th");
    for (var i = 0; i < cells.length; i++) {
      var text = (cells[i].textContent || "").trim();
      if (DOMAIN.test(text)) return text;
    }
    return "";
  }

  function selectedDomains(button) {
    var scope = button.closest("form") || document;
    var boxes = scope.querySelectorAll("input[type=checkbox]:checked");
    var seen = {};
    var domains = [];
    for (var i = 0; i < boxes.length; i++) {
      var row = boxes[i].closest("tr");
      if (!row) continue;
      var domain = rowDomain(row);
      if (!domain || seen[domain]) continue;
      seen[domain] = true;
      domains.push(domain);
    }
    return domains;
  }

  document.addEventListener("click", function (event) {
    var bulk = event.target.closest && event.target.closest("[data-copy-domains]");
    if (bulk) {
      event.preventDefault();
      var domains = selectedDomains(bulk);
      if (!domains.length) {
        if (window.seoNav) window.seoNav.toast("Отметьте строки — скопирую их домены", "warning");
        return;
      }
      copy(domains.join("\n")).then(
        function () {
          done(bulk);
          if (window.seoNav) {
            window.seoNav.toast("Скопировано: " + domains.length + plural(domains.length), "success");
          }
        },
        function () {
          if (window.seoNav) window.seoNav.toast("Не удалось скопировать", "error");
        }
      );
      return;
    }
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
