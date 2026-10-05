/* Рабочий продукт в шапке, рядом с «SEO-система» (E9-12, ADR-057).
 *
 * Выбор в списке сам отправляет форму: событие submit перехватывает
 * seo/soft-nav.js, сервер запоминает продукт и возвращает тот же экран под
 * него — без перезагрузки. «next» — адрес экрана на момент выбора: шапка при
 * переходах без перезагрузки не меняется, и адрес из шаблона устарел бы.
 */
(function () {
  "use strict";

  document.addEventListener("change", function (event) {
    var select = event.target.closest && event.target.closest(".seo-product-switch select");
    if (!select || !select.form) return;
    var form = select.form;
    form.elements.next.value = location.pathname + location.search;
    form.requestSubmit();
  });
})();
