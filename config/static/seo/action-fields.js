/* Поля рядом с действием списка — только при своём действии.
 *
 * Поле с `data-for-action="<действие>"` (например, сумма у «Цены написания…»)
 * видно, только когда это действие выбрано: иначе строка действий длинная и
 * непонятно, к чему поле. Слушатель — на документе: работает и после
 * подгрузки списка без перезагрузки (seo/soft-nav.js).
 */
(function () {
  "use strict";

  if (window.seoActionFields) return;
  window.seoActionFields = true;

  function sync(form) {
    var select = form.querySelector("select[name=action]");
    if (!select) return;
    form.querySelectorAll("[data-for-action]").forEach(function (field) {
      var holder = field.closest("label") || field;
      var shown = field.getAttribute("data-for-action") === select.value;
      holder.hidden = !shown;
      if (shown && document.activeElement === select) field.focus();
    });
  }

  function syncAll() {
    document.querySelectorAll("#changelist-form").forEach(sync);
  }

  document.addEventListener("change", function (event) {
    var target = event.target;
    if (!(target instanceof HTMLSelectElement) || target.name !== "action") return;
    var form = target.closest("form");
    if (form) sync(form);
  });

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", syncAll);
  else syncAll();
  document.addEventListener("seo:load", syncAll);
})();
