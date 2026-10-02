/* Окно «Решение по площадке» в «Площадках» без перехода (E9-09, ADR-046).
 *
 * Статус в строке списка (ссылка с data-decision) открывает окно поверх
 * списка: GET адреса из data-decision с заголовком X-Seo-Partial — сервер
 * отдаёт только форму (статус и причина отказа). «Сохранить» отправляет её
 * тем же заголовком: ошибка — форма с подсказками остаётся в окне; записано
 * — окно закрывается, список перечитывается на месте (seoNav.reload, с
 * прокруткой там же), внизу — сообщение.
 *
 * Закрыть — ✕, «Отмена», Esc или щелчок мимо окна. Ctrl/Cmd/Shift или
 * средняя кнопка — полная форма решения страницей; без скрипта — тоже она.
 */
(function () {
  "use strict";

  var PARTIAL = { "X-Seo-Partial": "1" };
  var dialog = null;
  var body = null;
  var opener = null;

  function build() {
    if (dialog && dialog.isConnected) return;
    dialog = document.createElement("dialog");
    dialog.className = "seo-dialog";
    dialog.setAttribute("aria-label", "Решение по площадке");
    body = document.createElement("div");
    body.className = "seo-dialog-body";
    dialog.appendChild(body);
    document.body.appendChild(dialog);
    dialog.addEventListener("click", function (event) {
      // Щелчок по затемнению вокруг окна попадает в сам <dialog>, по окну — в его содержимое.
      if (event.target === dialog || event.target.closest("[data-dialog-close]")) dialog.close();
    });
    dialog.addEventListener("close", function () {
      if (opener && opener.isConnected) opener.focus();
      opener = null;
    });
    dialog.addEventListener("submit", onSubmit);
  }

  function message(text) {
    body.textContent = "";
    var line = document.createElement("p");
    line.className = "seo-dialog-message";
    line.textContent = text;
    body.appendChild(line);
  }

  function show(html) {
    body.innerHTML = html;
    var select = body.querySelector("select");
    if (select) select.focus();
  }

  async function open(url, link) {
    build();
    opener = link;
    message("Загружаю…");
    if (!dialog.open) dialog.showModal();
    try {
      var response = await fetch(url, { headers: PARTIAL, credentials: "same-origin" });
      if (!response.ok) throw new Error("сервер ответил " + response.status);
      show(await response.text());
    } catch (error) {
      message("Не удалось открыть решение: " + error.message + ".");
    }
  }

  async function onSubmit(event) {
    var form = event.target.closest("form[data-decision-form]");
    if (!form) return;
    event.preventDefault();
    var button = form.querySelector("button[type=submit]");
    if (button) {
      button.disabled = true;
      button.classList.add("seo-btn-busy");
    }
    try {
      var response = await fetch(form.getAttribute("action"), {
        method: "POST",
        body: new FormData(form),
        headers: PARTIAL,
        credentials: "same-origin",
      });
      var type = response.headers.get("Content-Type") || "";
      if (response.ok && type.indexOf("json") !== -1) {
        var data = await response.json();
        dialog.close();
        if (window.seoNav) {
          window.seoNav.reload();
          window.seoNav.toast(data.message, "success");
        } else {
          window.location.reload();
        }
        return;
      }
      // Ошибка в форме — та же форма с подсказками.
      if (response.status === 400) {
        show(await response.text());
        return;
      }
      throw new Error("сервер ответил " + response.status);
    } catch (error) {
      if (button) {
        button.disabled = false;
        button.classList.remove("seo-btn-busy");
      }
      if (window.seoNav) window.seoNav.toast("Не удалось сохранить: " + error.message);
    }
  }

  document.addEventListener("click", function (event) {
    var link = event.target.closest && event.target.closest("a[data-decision]");
    if (!link || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    open(link.getAttribute("data-decision"), link);
  });

  // Ушли на другой экран («Открыть полностью», «Назад») — окно закрыть.
  document.addEventListener("seo:unload", function () {
    if (dialog && dialog.open) dialog.close();
  });
})();
