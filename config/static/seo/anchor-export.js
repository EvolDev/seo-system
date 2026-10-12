/* Анкоры: копируем отмеченные строки через сервер, включая выбор всех страниц. */
(function () {
  "use strict";
  if (window.seoAnchorExport) return;
  window.seoAnchorExport = true;

  function toast(text, kind) {
    if (window.seoNav) window.seoNav.toast(text, kind);
  }

  function fallback(text) {
    var field = document.createElement("textarea");
    field.value = text;
    field.style.position = "fixed";
    field.style.opacity = "0";
    document.body.appendChild(field);
    field.select();
    try {
      if (!document.execCommand("copy")) throw new Error("Буфер обмена недоступен");
    } finally {
      field.remove();
    }
  }

  async function clipboard(text) {
    if (navigator.clipboard && window.isSecureContext) {
      try { await navigator.clipboard.writeText(text); return; } catch (error) { /* fallback */ }
    }
    fallback(text);
  }

  document.addEventListener("click", async function (event) {
    var button = event.target.closest && event.target.closest("[data-copy-anchors]");
    if (!button || button.disabled) return;
    event.preventDefault();
    var form = button.closest("form");
    var across = form.querySelector('input[name="select_across"]');
    var selected = form.querySelectorAll('input[name="_selected_action"]:checked');
    var all = across && across.value === "1";
    if (!all && !selected.length) {
      toast("Отметьте анкоры для копирования", "warning");
      return;
    }
    var data = new FormData();
    data.append("select_across", all ? "1" : "0");
    selected.forEach(function (input) { data.append("ids", input.value); });
    var csrf = form.querySelector('input[name="csrfmiddlewaretoken"]');
    var label = "Скопировать анкоры";
    clearTimeout(button.seoAnchorTimer);
    button.disabled = true;
    button.textContent = "Копирование…";
    try {
      var response = await fetch(button.dataset.copyAnchors, {
        method: "POST", body: data, credentials: "same-origin",
        headers: {"X-CSRFToken": csrf ? csrf.value : "", "Accept": "application/json"}
      });
      if (!response.ok) throw new Error("Сервер ответил " + response.status);
      var answer = await response.json();
      if (!answer.count) throw new Error("В выбранных строках нет анкоров");
      await clipboard(answer.text);
      button.textContent = "Скопировано";
      toast("Скопировано анкоров: " + answer.count, "success");
      button.seoAnchorTimer = setTimeout(function () { button.textContent = label; }, 2000);
    } catch (error) {
      button.textContent = label;
      toast("Не удалось скопировать: " + error.message, "error");
    } finally {
      button.disabled = false;
    }
  });
})();
