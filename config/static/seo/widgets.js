/* Виджеты форм под удобство (E9-11, ADR-048) — config/forms.py.
 *
 * «Сегодня» у поля дня ставит в поле сегодняшний день. Кнопка статуса с
 * data-fills при выборе ставит сегодняшний день в пустое поле даты той же
 * формы (у размещения «Заявка отправлена» — дату заявки) и подсвечивает его;
 * выбрали другой статус, а подставленный день не трогали — он уходит.
 * «Сегодня» — по часам сервера, как у календаря Django: в списках день
 * показывается по времени проекта.
 *
 * Обработчики — на документе: формы приходят и страницей, и в панель записи.
 */
(function () {
  "use strict";

  if (window.seoWidgets) return;
  window.seoWidgets = true;

  function pad(number) {
    return (number < 10 ? "0" : "") + number;
  }

  function today() {
    var now = new Date();
    var offset = Number(document.body.dataset.adminUtcOffset);
    if (document.body.dataset.adminUtcOffset !== undefined && !isNaN(offset)) {
      // Часы сервера: UTC + его сдвиг, разложенные по полям даты браузера.
      now = new Date(now.getTime() + offset * 1000 + now.getTimezoneOffset() * 60000);
    }
    return now.getFullYear() + "-" + pad(now.getMonth() + 1) + "-" + pad(now.getDate());
  }

  // auto — подставлено выбором статуса: подсветить и помнить, что подставили.
  function setDay(input, value, auto) {
    input.value = value;
    if (auto) input.dataset.autoDay = value;
    else delete input.dataset.autoDay;
    var box = input.closest(".seo-day");
    if (box) {
      box.classList.toggle("seo-day-auto", auto);
      box.title = auto ? "Подставлено по статусу — поправьте, если день другой" : "";
    }
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest && event.target.closest("[data-today]");
    if (!button) return;
    var input = button.parentNode.querySelector("input");
    if (input && !input.disabled && !input.readOnly) setDay(input, today(), false);
  });

  document.addEventListener("change", function (event) {
    var radio = event.target;
    if (!(radio instanceof HTMLInputElement) || !radio.matches(".seo-choice input[type=radio]")) return;
    var form = radio.form;
    if (!form) return;
    var name = radio.dataset.fills || "";
    form.querySelectorAll("input[data-auto-day]").forEach(function (input) {
      if (input.name !== name && input.value === input.dataset.autoDay) setDay(input, "", false);
    });
    var target = name ? form.elements.namedItem(name) : null;
    if (target instanceof HTMLInputElement && !target.value) setDay(target, today(), true);
  });

  // Человек сам поправил подставленный день — подсветку снять.
  document.addEventListener("input", function (event) {
    var input = event.target;
    if (!event.isTrusted || !(input instanceof HTMLInputElement) || input.dataset.autoDay === undefined) return;
    delete input.dataset.autoDay;
    var box = input.closest(".seo-day");
    if (box) {
      box.classList.remove("seo-day-auto");
      box.title = "";
    }
  });
})();
