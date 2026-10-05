/* Анкоры продукта (E3-05, ADR-059): ссылки размещения и страница «Анкоры».
 *
 * 1. Поле «Анкор» у ссылки — <select data-anchor-select> с анкорами всех
 *    продуктов; здесь оно становится полем с поиском: в списке — анкоры
 *    продукта размещения, последний пункт — «＋ Новый анкор…». Выбрали анкор —
 *    «Куда ведёт» получает его адрес, если поле пустое или в нём адрес,
 *    подставленный раньше; свой адрес человека не трогаем.
 * 2. Окно «Новый анкор» (<dialog data-anchor-dialog>): анкор, куда ведёт, тип
 *    страницы. Записанный анкор появляется во всех полях анкора и выбирается
 *    в том, откуда окно открыли; на странице «Анкоры» список перечитывается.
 * 3. «Рекомендуем» — щелчок ставит анкор в первую свободную ссылку, нет
 *    свободной — добавляет ссылку («Добавить ещё»).
 * 4. Сменили продукт в новой форме — блок «Анкоры продукта» приходит заново,
 *    в полях анкора — анкоры нового продукта.
 * 5. Доли на странице «Анкоры» правятся на месте: значение уходит при смене
 *    поля, после записи список перечитывается без перезагрузки.
 *
 * Обработчики на document — один раз; поля настраивает init(): скрипт
 * зовут в конце формы размещения (в панели записи — когда ссылки на месте),
 * по seo:load и при добавлении строки ссылок (formset:added).
 */
(function () {
  "use strict";

  if (window.seoAnchors) {
    window.seoAnchors.init(document);
    return;
  }

  var MAX_SHOWN = 60;

  function csrfToken() {
    var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    if (match) return decodeURIComponent(match[1]);
    var input = document.querySelector("input[name=csrfmiddlewaretoken]");
    return input ? input.value : "";
  }

  function toast(text, kind) {
    if (window.seoNav && text) window.seoNav.toast(text, kind || "success");
  }

  async function post(url, fields) {
    var data = new FormData();
    Object.keys(fields).forEach(function (key) { data.append(key, fields[key]); });
    var response = await fetch(url, {
      method: "POST",
      body: data,
      credentials: "same-origin",
      headers: { "Accept": "application/json", "X-CSRFToken": csrfToken() },
    });
    if (!response.ok) throw new Error("сервер ответил " + response.status);
    return response.json();
  }

  function norm(text) {
    return String(text || "").toLowerCase().replace(/\s+/g, " ").trim();
  }

  // ---------- Продукт формы ----------

  function productOf(node) {
    var form = node && node.closest ? node.closest("form") : null;
    var field = form ? form.querySelector("select[name=product], input[name=product]:not([type=hidden])") : null;
    if (field && field.value) return field.value;
    var block = document.querySelector("[data-anchor-block]");
    if (block && block.dataset.product) return block.dataset.product;
    var dialog = document.querySelector("[data-anchor-dialog]");
    return dialog ? dialog.dataset.product || "" : "";
  }

  // ---------- Поле «Анкор» с поиском ----------

  function isTemplate(node) {
    return Boolean(node.closest(".empty-form")) || /__prefix__/.test(node.name || "");
  }

  function selectedLabel(select) {
    var option = select.selectedOptions[0];
    if (!option || (!option.value && !option.dataset.legacy)) return "";
    return option.textContent.trim();
  }

  function urlInputOf(select) {
    var row = select.closest(".inline-related") || select.closest("fieldset") || select.form;
    return row ? row.querySelector("[data-anchor-url]") : null;
  }

  function fillUrl(select, url) {
    var input = urlInputOf(select);
    if (!input || !url) return;
    if (input.value && input.value !== input.dataset.autoUrl) return;
    input.value = url;
    input.dataset.autoUrl = url;
    input.classList.remove("seo-anchor-url-flash");
    void input.offsetWidth;
    input.classList.add("seo-anchor-url-flash");
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function choose(select, value) {
    var option = Array.prototype.find.call(select.options, function (o) { return o.value === String(value); });
    if (!option) return false;
    select.value = option.value;
    var picker = select.seoPicker;
    if (picker) picker.input.value = option.textContent.trim();
    fillUrl(select, option.dataset.url);
    select.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  }

  function enhance(select) {
    if (select.seoPicker || isTemplate(select)) return;
    var box = document.createElement("div");
    box.className = "seo-picker";
    var input = document.createElement("input");
    input.type = "text";
    input.className = "seo-picker-input";
    input.autocomplete = "off";
    input.placeholder = "Найти анкор…";
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-expanded", "false");
    input.setAttribute("aria-autocomplete", "list");
    if (select.id) {
      var label = document.querySelector('label[for="' + select.id + '"]');
      if (label) label.htmlFor = select.id + "_search";
      input.id = select.id + "_search";
    }
    var toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "seo-picker-toggle";
    toggle.tabIndex = -1;
    toggle.setAttribute("aria-label", "Все анкоры");
    var list = document.createElement("ul");
    list.className = "seo-picker-list";
    list.setAttribute("role", "listbox");
    list.hidden = true;
    var listId = (select.id || "anchor") + "_list";
    list.id = listId;
    input.setAttribute("aria-controls", listId);
    box.appendChild(input);
    box.appendChild(toggle);
    box.appendChild(list);
    select.hidden = true;
    select.parentNode.insertBefore(box, select.nextSibling);
    var picker = { box: box, input: input, list: list, active: -1, items: [] };
    select.seoPicker = picker;
    input.value = selectedLabel(select);
    var legacy = select.selectedOptions[0] && select.selectedOptions[0].dataset.legacy;
    if (legacy) box.classList.add("seo-picker-legacy");

    function open(query) {
      render(select, query);
      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
      box.classList.add("seo-picker-open");
    }
    function close() {
      list.hidden = true;
      input.setAttribute("aria-expanded", "false");
      box.classList.remove("seo-picker-open");
      picker.active = -1;
      input.removeAttribute("aria-activedescendant");
    }
    picker.close = close;

    input.addEventListener("focus", function () { input.select(); open(""); });
    input.addEventListener("click", function () { if (list.hidden) open(""); });
    input.addEventListener("input", function () { open(input.value); });
    toggle.addEventListener("mousedown", function (event) { event.preventDefault(); });
    toggle.addEventListener("click", function () {
      if (list.hidden) { input.focus(); open(""); } else close();
    });
    input.addEventListener("blur", function () {
      setTimeout(function () {
        if (box.contains(document.activeElement)) return;
        close();
        input.value = selectedLabel(select);
      }, 120);
    });
    input.addEventListener("keydown", function (event) {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        if (list.hidden) open(input.value);
        move(picker, event.key === "ArrowDown" ? 1 : -1);
      } else if (event.key === "Enter") {
        // Enter в поле не отправляет форму размещения: выбирает вариант.
        event.preventDefault();
        if (!list.hidden && picker.items[picker.active]) pickItem(select, picker.items[picker.active]);
        else if (!list.hidden && picker.items.length === 1) pickItem(select, picker.items[0]);
      } else if (event.key === "Escape") {
        if (!list.hidden) {
          event.preventDefault();
          event.stopPropagation();
          close();
          input.value = selectedLabel(select);
        }
      }
    });
    list.addEventListener("mousedown", function (event) { event.preventDefault(); });
    list.addEventListener("click", function (event) {
      var item = event.target.closest("li[data-index]");
      if (item) pickItem(select, picker.items[Number(item.dataset.index)]);
    });
  }

  function move(picker, step) {
    if (!picker.items.length) return;
    picker.active = (picker.active + step + picker.items.length) % picker.items.length;
    Array.prototype.forEach.call(picker.list.querySelectorAll("li[data-index]"), function (li) {
      var on = Number(li.dataset.index) === picker.active;
      li.classList.toggle("seo-picker-active", on);
      li.setAttribute("aria-selected", String(on));
      if (on) {
        picker.input.setAttribute("aria-activedescendant", li.id);
        li.scrollIntoView({ block: "nearest" });
      }
    });
  }

  function render(select, query) {
    var picker = select.seoPicker;
    var product = productOf(select);
    var q = norm(query);
    var starts = [];
    var contains = [];
    Array.prototype.forEach.call(select.options, function (option) {
      if (!option.value || (product && option.dataset.product !== product)) return;
      var text = norm(option.textContent);
      if (!q || text.indexOf(q) === 0) starts.push(option);
      else if (text.indexOf(q) !== -1) contains.push(option);
    });
    var found = starts.concat(contains);
    picker.list.textContent = "";
    picker.items = [];
    found.slice(0, MAX_SHOWN).forEach(function (option) {
      picker.items.push({ option: option });
    });
    var canAdd = Boolean(document.querySelector("[data-anchor-dialog]"));
    if (canAdd) picker.items.push({ add: true, text: query.trim() });
    picker.items.forEach(function (item, index) {
      var li = document.createElement("li");
      li.dataset.index = String(index);
      li.id = picker.list.id + "_" + index;
      li.setAttribute("role", "option");
      if (item.add) {
        li.className = "seo-picker-add";
        li.textContent = item.text ? "＋ Новый анкор «" + item.text + "»…" : "＋ Новый анкор…";
      } else {
        var name = document.createElement("span");
        name.className = "seo-picker-name";
        name.textContent = item.option.textContent.trim();
        li.appendChild(name);
        var meta = document.createElement("span");
        meta.className = "seo-picker-meta";
        meta.textContent = item.option.dataset.naked ? "безанкор" : item.option.dataset.type || "";
        li.appendChild(meta);
        if (item.option.selected) li.classList.add("seo-picker-current");
      }
      picker.list.appendChild(li);
    });
    if (!found.length) {
      var empty = document.createElement("li");
      empty.className = "seo-picker-empty";
      empty.textContent = q ? "Такого анкора нет" : "У продукта нет анкоров";
      picker.list.insertBefore(empty, picker.list.firstChild);
    } else if (found.length > MAX_SHOWN) {
      var more = document.createElement("li");
      more.className = "seo-picker-empty";
      more.textContent = "Ещё " + (found.length - MAX_SHOWN) + " — уточните поиск";
      picker.list.insertBefore(more, picker.list.lastChild);
    }
    picker.active = -1;
    if (q && found.length) move(picker, 1);
  }

  function pickItem(select, item) {
    if (!item) return;
    var picker = select.seoPicker;
    if (item.add) {
      picker.close();
      openDialog(productOf(select), item.text, select);
      return;
    }
    choose(select, item.option.value);
    picker.box.classList.remove("seo-picker-legacy");
    picker.close();
  }

  // ---------- Окно «Новый анкор» ----------

  var requester = null;

  function dialogEl() {
    return document.querySelector("[data-anchor-dialog]");
  }

  function field(dialog, name) {
    return dialog.querySelector('[data-dialog-field="' + name + '"]');
  }

  function openDialog(product, text, from) {
    var dialog = dialogEl();
    if (!dialog) return;
    requester = from || null;
    if (product) dialog.dataset.product = product;
    var looksUrl = /^https?:\/\//i.test(text || "");
    field(dialog, "keyword").value = looksUrl ? "" : text || "";
    field(dialog, "target_url").value = looksUrl ? text : "";
    field(dialog, "page_type").value = "";
    showError(dialog, "");
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
    (field(dialog, "keyword").value ? field(dialog, "target_url") : field(dialog, "keyword")).focus();
  }

  function closeDialog(dialog) {
    if (typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
    if (requester && requester.seoPicker) requester.seoPicker.input.focus();
  }

  function showError(dialog, text) {
    var line = dialog.querySelector("[data-anchor-dialog-error]");
    line.textContent = text;
    line.hidden = !text;
  }

  async function saveDialog(dialog) {
    var button = dialog.querySelector("[data-anchor-dialog-save]");
    if (button.disabled) return;
    button.disabled = true;
    try {
      var answer = await post(dialog.dataset.addUrl, {
        product: dialog.dataset.product || "",
        keyword: field(dialog, "keyword").value,
        target_url: field(dialog, "target_url").value,
        page_type: field(dialog, "page_type").value,
      });
      if (!answer.ok) {
        showError(dialog, answer.message);
        return;
      }
      addOption(answer);
      var from = requester;
      closeDialog(dialog);
      if (from) {
        choose(from, answer.id);
        if (from.seoPicker) from.seoPicker.box.classList.remove("seo-picker-legacy");
      }
      toast(answer.message);
      document.dispatchEvent(new CustomEvent("seo:anchor-added", { detail: answer }));
      if (document.querySelector("[data-anchors-page]") && window.seoNav) window.seoNav.reload();
    } catch (error) {
      showError(dialog, "Не записалось: " + error.message);
    } finally {
      button.disabled = false;
    }
  }

  function addOption(anchor) {
    document.querySelectorAll("select[data-anchor-select]").forEach(function (select) {
      var exists = Array.prototype.some.call(select.options, function (o) { return o.value === String(anchor.id); });
      if (exists) return;
      var option = document.createElement("option");
      option.value = String(anchor.id);
      option.textContent = anchor.keyword;
      option.dataset.product = String(anchor.product);
      option.dataset.url = anchor.url;
      option.dataset.type = anchor.page_type || "";
      if (anchor.naked) option.dataset.naked = "1";
      select.appendChild(option);
    });
  }

  // ---------- «Рекомендуем» ----------

  function linkSelects() {
    return Array.prototype.filter.call(document.querySelectorAll("select[data-anchor-select]"), function (select) {
      return !isTemplate(select);
    });
  }

  function isFree(select) {
    var option = select.selectedOptions[0];
    return !select.value && !(option && option.dataset.legacy);
  }

  function pickRecommended(button) {
    var id = button.dataset.anchorPick;
    var free = linkSelects().filter(isFree)[0];
    if (free) {
      place(free, id, button);
      return;
    }
    var first = linkSelects()[0];
    var group = first ? first.closest(".inline-group") : document.querySelector(".inline-group");
    var add = group ? group.querySelector(".add-row a") : null;
    if (!add) {
      toast("Свободной ссылки нет — сначала уберите анкор из одной из ссылок.", "error");
      return;
    }
    pendingPick = { id: id, button: button };
    add.click();
  }

  var pendingPick = null;

  function place(select, id, button) {
    enhance(select);
    if (!choose(select, id)) return;
    if (select.seoPicker) select.seoPicker.box.classList.remove("seo-picker-legacy");
    button.classList.add("seo-anchor-picked");
    var row = select.closest(".inline-related") || select;
    row.scrollIntoView({ block: "nearest", behavior: "smooth" });
    row.classList.remove("seo-anchor-row-flash");
    void row.offsetWidth;
    row.classList.add("seo-anchor-row-flash");
  }

  // ---------- Смена продукта в новой форме ----------

  async function reloadBlock(product) {
    var block = document.querySelector("[data-anchor-block]");
    var dialog = dialogEl();
    if (dialog) dialog.dataset.product = product;
    linkSelects().forEach(function (select) {
      if (select.value && select.selectedOptions[0].dataset.product !== product) {
        select.value = "";
        if (select.seoPicker) select.seoPicker.input.value = "";
      }
    });
    if (!block || !product) return;
    var url = block.dataset.summaryUrl.replace(/\/\d+\/$/, "/" + product + "/");
    try {
      var response = await fetch(url, { credentials: "same-origin" });
      if (!response.ok) return;
      var holder = block.parentNode;
      holder.innerHTML = await response.text();
    } catch (error) {
      toast("Не обновились анкоры продукта: " + error.message, "error");
    }
  }

  // ---------- Доли на странице «Анкоры» ----------

  async function saveShare(input) {
    var page = input.closest("[data-anchors-page]");
    if (!page) return;
    if (input.value === input.defaultValue) return;
    input.classList.add("seo-share-busy");
    try {
      var answer = await post(page.dataset.shareUrl, {
        kind: input.dataset.kind,
        id: input.dataset.id,
        field: input.dataset.field,
        value: input.value,
        product: page.dataset.product,
      });
      if (!answer.ok) {
        toast(answer.message, "error");
        input.value = input.defaultValue;
        return;
      }
      input.value = answer.value;
      input.defaultValue = answer.value;
      input.classList.add("seo-share-saved");
      if (window.seoNav) window.seoNav.reload();
    } catch (error) {
      toast("Не записалось: " + error.message, "error");
      input.value = input.defaultValue;
    } finally {
      input.classList.remove("seo-share-busy");
    }
  }

  // ---------- Обработчики на document ----------

  document.addEventListener("click", function (event) {
    var target = event.target;
    if (!target.closest) return;
    var opener = target.closest("[data-anchor-new]");
    if (opener) {
      event.preventDefault();
      openDialog(opener.dataset.product || productOf(opener), "", null);
      return;
    }
    var pick = target.closest("[data-anchor-pick]");
    if (pick) {
      event.preventDefault();
      pickRecommended(pick);
      return;
    }
    var dialog = target.closest("[data-anchor-dialog]");
    if (!dialog) return;
    if (target.closest("[data-anchor-dialog-save]")) {
      event.preventDefault();
      saveDialog(dialog);
    } else if (target.closest("[data-anchor-dialog-cancel]")) {
      event.preventDefault();
      closeDialog(dialog);
    } else if (target === dialog) {
      // Щелчок по затемнению вокруг окна.
      closeDialog(dialog);
    }
  });

  document.addEventListener("keydown", function (event) {
    var target = event.target;
    if (event.key !== "Enter" || !target.closest) return;
    var dialog = target.closest("[data-anchor-dialog]");
    if (dialog && target.tagName !== "BUTTON") {
      // Окно внутри формы размещения: Enter записывает анкор, а не форму.
      event.preventDefault();
      saveDialog(dialog);
      return;
    }
    if (target.matches("input[data-share]")) {
      event.preventDefault();
      target.blur();
    }
  });

  document.addEventListener("change", function (event) {
    var target = event.target;
    if (target.matches && target.matches("input[data-share]")) {
      saveShare(target);
    } else if (target.matches && target.matches("select[name=product]") && target.closest("form") &&
               target.closest("form").querySelector("select[data-anchor-select]")) {
      reloadBlock(target.value);
    }
  });

  // Новая строка ссылок («Добавить ещё»): поле анкора и, если ждёт, рекомендация.
  document.addEventListener("formset:added", function (event) {
    var row = event.target;
    if (!row || !row.querySelectorAll) return;
    row.querySelectorAll("select[data-anchor-select]").forEach(function (select) {
      enhance(select);
      if (pendingPick) {
        var wanted = pendingPick;
        pendingPick = null;
        place(select, wanted.id, wanted.button);
      }
    });
  });

  function init(root) {
    (root || document).querySelectorAll("select[data-anchor-select]").forEach(enhance);
  }

  document.addEventListener("seo:load", function () { init(document); });
  window.seoAnchors = { init: init, open: openDialog };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { init(document); });
  } else {
    init(document);
  }
})();
