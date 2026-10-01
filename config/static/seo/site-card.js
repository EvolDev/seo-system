/* Карточка площадки окном справа, без перезагрузки страницы (E1-07, ADR-043).
 *
 * Ссылки с data-site-card (домен и «💬» в «Площадках», «Карточка» в каталоге)
 * открывают окно и подгружают в него карточку: GET по адресу ссылки с
 * заголовком X-Seo-Partial — сервер отдаёт только содержимое, без страницы.
 * Формы в карточке (data-card-form: «Сделать рабочей», «Оставить как есть»,
 * «Добавить заметку») окно отправляет само и показывает обновлённую карточку.
 *
 * Закрыть — ✕, Esc или щелчок мимо окна. Если в карточке что-то поменяли,
 * страница под окном перечитывается: колонки списка должны показать новое.
 * Ссылка с Ctrl/Cmd/Shift или средней кнопкой открывает карточку страницей.
 * Без скрипта ссылки и формы работают обычными переходами.
 */
(function () {
  "use strict";

  var PARTIAL = { "X-Seo-Partial": "1" };
  var back = null;
  var panel = null;
  var body = null;
  var opener = null;
  var changed = false;

  function build() {
    if (panel) return;
    back = document.createElement("div");
    back.className = "seo-card-back";
    back.hidden = true;
    panel = document.createElement("aside");
    panel.className = "seo-card";
    panel.hidden = true;
    panel.tabIndex = -1;
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "true");
    panel.setAttribute("aria-label", "Карточка площадки");
    body = document.createElement("div");
    body.className = "seo-card-body";
    panel.appendChild(body);
    document.body.appendChild(back);
    document.body.appendChild(panel);
    back.addEventListener("click", close);
    panel.addEventListener("submit", onSubmit);
  }

  function message(text) {
    body.textContent = "";
    var line = document.createElement("p");
    line.className = "seo-card-message";
    line.textContent = text;
    body.appendChild(line);
  }

  function show(request) {
    return request
      .then(function (response) {
        if (!response.ok) throw new Error("Сервер ответил " + response.status);
        return response.text();
      })
      .then(function (html) {
        body.innerHTML = html;
      })
      .catch(function (error) {
        message("Не удалось загрузить карточку: " + error.message + ". Обновите страницу.");
      });
  }

  function open(url, link) {
    build();
    opener = link;
    changed = false;
    back.hidden = false;
    panel.hidden = false;
    document.documentElement.classList.add("seo-card-open");
    message("Загружаю карточку…");
    panel.focus();
    show(fetch(url, { headers: PARTIAL, credentials: "same-origin" }));
  }

  function close() {
    if (!panel || panel.hidden) return;
    back.hidden = true;
    panel.hidden = true;
    document.documentElement.classList.remove("seo-card-open");
    if (changed) {
      // Цена или заметки поменялись — список под окном показывает старое.
      // Страница, которая умеет обновиться сама (экраны загрузки, E1-08),
      // ловит событие и подгружает себя без перезагрузки.
      var event = new CustomEvent("seo:card-changed", { cancelable: true });
      if (document.dispatchEvent(event)) window.location.reload();
      if (opener) opener.focus();
      return;
    }
    if (opener) opener.focus();
  }

  function onSubmit(event) {
    var form = event.target.closest("form[data-card-form]");
    if (!form) return;
    event.preventDefault();
    var button = form.querySelector("button[type=submit]");
    if (button) button.disabled = true;
    changed = true;
    show(
      fetch(form.action, {
        method: "POST",
        body: new FormData(form),
        headers: PARTIAL,
        credentials: "same-origin",
      })
    );
  }

  document.addEventListener("click", function (event) {
    var closer = event.target.closest("[data-card-close]");
    if (closer && panel && panel.contains(closer)) {
      close();
      return;
    }
    var link = event.target.closest("a[data-site-card]");
    if (!link || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey) return;
    event.preventDefault();
    open(link.href, link);
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") close();
  });
})();
