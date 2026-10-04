/* Счета продавцов (E1-14, ADR-055).
 *
 * - «Поделить поровну» в форме счёта очищает доли строк: при записи сумма
 *   счёта поделится поровну до цента (apps/placements/invoices.py).
 * - «Счёт на отмеченные» в «Размещениях»: вместо перехода на форму счёта —
 *   панель справа поверх списка с отмеченными размещениями (seo/panel.js).
 *   «Выбрать все N» и страница без скрипта — обычное действие: сервер
 *   откроет форму нового счёта страницей.
 */
(function () {
  "use strict";

  if (window.seoInvoices) return;
  window.seoInvoices = true;

  var ACTION = "invoice_for_selected_action";

  document.addEventListener("click", function (event) {
    var button = event.target instanceof Element ? event.target.closest("[data-split-evenly]") : null;
    if (!button) return;
    var form = button.closest("form");
    if (!form) return;
    form.querySelectorAll("input[name^='items-'][name$='-amount_cents']").forEach(function (input) {
      if (!input.value) return;
      input.value = "";
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
  });

  // Раньше обработчика подгрузки (seo/soft-nav.js): он слушает отправку на window
  // без перехвата, этот — с перехватом.
  window.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement) || form.id !== "changelist-form") return;
    var select = form.querySelector("select[name=action]");
    if (!select || select.value !== ACTION) return;
    if (!window.seoPanel || !window.seoPanel.open) return;
    var across = form.querySelector("input[name=select_across]");
    if (across && across.value === "1") return;
    var ids = Array.prototype.map.call(
      form.querySelectorAll("input[name=_selected_action]:checked"),
      function (box) { return box.value; }
    );
    if (!ids.length) return;
    event.preventDefault();
    event.stopPropagation();
    var list = new URL(form.getAttribute("action") || window.location.href, window.location.href);
    var url = new URL("../invoice/add/", list);
    url.search = "placements=" + ids.join(",");
    window.seoPanel.open(url.href);
  }, true);
})();
