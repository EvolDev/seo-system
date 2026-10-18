/* Копирайтер в размещении (ADR-072): выбор подставляет его ставку.
 *
 * У вариантов списка «Копирайтер» — `data-rate` и `data-currency`. Ставка
 * встаёт в «Цену копирайтера», если там пусто или стоит подставленная раньше
 * ставка другого копирайтера; вписанную руками цену не трогаем. Слушатель — на
 * документе: форма приходит и панелью, и страницей (seo/panel.js, soft-nav.js).
 */
(function () {
  "use strict";

  if (window.seoCopywriterPick) return;
  window.seoCopywriterPick = true;

  document.addEventListener("change", function (event) {
    var select = event.target;
    if (!(select instanceof HTMLSelectElement) || !select.hasAttribute("data-copywriter")) return;
    var form = select.form;
    if (!form) return;
    var amount = form.querySelector("[name=copywriter_cents]");
    var currency = form.querySelector("[name=copywriter_currency]");
    if (!amount) return;
    var option = select.selectedOptions[0];
    var rate = option ? option.getAttribute("data-rate") : null;
    var filled = amount.getAttribute("data-filled");
    if (amount.value && amount.value !== filled) return;
    amount.value = rate || "";
    if (rate) amount.setAttribute("data-filled", rate);
    else amount.removeAttribute("data-filled");
    if (currency && rate) currency.value = option.getAttribute("data-currency") || "EUR";
    amount.dispatchEvent(new Event("input", { bubbles: true }));
    amount.dispatchEvent(new Event("change", { bubbles: true }));
  });
})();
