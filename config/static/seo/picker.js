/* Выпадающий список с поиском: `<select data-search>` → поле «начните печатать».
 *
 * Продавцов в справочнике полсотни, и в штатном списке нужного искали глазами
 * (просьба пользователя 05.10.2026). Здесь поле фильтрует варианты по мере
 * ввода, стрелки и Enter работают как в списке, Esc закрывает. Сам <select>
 * остаётся в форме скрытым — значение уходит на сервер как прежде, и без
 * скрипта экран работает как раньше.
 *
 * Оформление общее с полем «Анкор» (seo/picker.css): тот список устроен так же,
 * но знает про анкоры и продукты (seo/anchors.js).
 */
(function () {
  "use strict";

  if (window.seoPicker) {
    window.seoPicker.init(document);
    return;
  }

  var MAX_SHOWN = 80;

  function options(select) {
    return Array.prototype.slice.call(select.options);
  }

  function labelOf(select) {
    var option = select.selectedOptions[0];
    return option && option.value ? option.textContent.trim() : "";
  }

  function enhance(select) {
    if (select.seoPicker || /__prefix__/.test(select.name || "")) return;

    var box = document.createElement("div");
    box.className = "seo-picker";
    var input = document.createElement("input");
    input.type = "text";
    input.className = "seo-picker-input";
    input.autocomplete = "off";
    input.placeholder = select.dataset.search || "Начните печатать…";
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
    toggle.setAttribute("aria-label", "Показать список");
    var list = document.createElement("ul");
    list.className = "seo-picker-list";
    list.hidden = true;

    select.parentNode.insertBefore(box, select);
    box.appendChild(input);
    box.appendChild(toggle);
    box.appendChild(list);
    box.appendChild(select);
    select.hidden = true;
    select.tabIndex = -1;
    input.value = labelOf(select);

    var picker = { select: select, input: input, list: list, index: -1 };
    select.seoPicker = picker;

    function close() {
      list.hidden = true;
      picker.index = -1;
      input.setAttribute("aria-expanded", "false");
    }

    function pick(option) {
      select.value = option.value;
      input.value = option.value ? option.textContent.trim() : "";
      close();
      select.dispatchEvent(new Event("change", { bubbles: true }));
    }

    function draw(query) {
      var text = (query || "").trim().toLowerCase();
      var found = options(select).filter(function (option) {
        if (!option.value) return !text; // «— выберите —» — только в полном списке
        return !text || option.textContent.toLowerCase().indexOf(text) !== -1;
      });
      list.textContent = "";
      found.slice(0, MAX_SHOWN).forEach(function (option) {
        var item = document.createElement("li");
        item.textContent = option.textContent.trim();
        if (option.value === select.value) item.classList.add("seo-picker-current");
        item.addEventListener("mousedown", function (event) {
          event.preventDefault();
          pick(option);
        });
        list.appendChild(item);
      });
      if (!found.length) {
        var empty = document.createElement("li");
        empty.className = "seo-picker-empty";
        empty.textContent = "Ничего не нашлось";
        list.appendChild(empty);
      } else if (found.length > MAX_SHOWN) {
        var more = document.createElement("li");
        more.className = "seo-picker-empty";
        more.textContent = "Ещё " + (found.length - MAX_SHOWN) + " — уточните поиск";
        list.appendChild(more);
      }
      picker.found = found;
      picker.index = -1;
      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
    }

    function move(step) {
      var items = list.querySelectorAll("li:not(.seo-picker-empty)");
      if (!items.length) return;
      picker.index = (picker.index + step + items.length) % items.length;
      items.forEach(function (item, at) {
        item.classList.toggle("seo-picker-active", at === picker.index);
      });
      items[picker.index].scrollIntoView({ block: "nearest" });
    }

    input.addEventListener("input", function () { draw(input.value); });
    input.addEventListener("focus", function () { draw(""); input.select(); });
    toggle.addEventListener("click", function () {
      if (list.hidden) { draw(""); input.focus(); } else { close(); }
    });
    input.addEventListener("blur", function () { setTimeout(close, 120); });
    input.addEventListener("keydown", function (event) {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        if (list.hidden) draw(input.value);
        else move(event.key === "ArrowDown" ? 1 : -1);
        return;
      }
      if (event.key === "Enter") {
        var items = list.querySelectorAll("li:not(.seo-picker-empty)");
        if (!list.hidden && picker.index >= 0 && picker.found) {
          event.preventDefault();
          pick(picker.found[picker.index]);
        } else if (!list.hidden && items.length === 1 && picker.found) {
          event.preventDefault();
          pick(picker.found[0]);
        }
        return;
      }
      if (event.key === "Escape" && !list.hidden) {
        event.preventDefault();
        close();
      }
    });
    // Значение поменяли со стороны (сброс формы, другой скрипт) — показать его.
    select.addEventListener("change", function () {
      if (document.activeElement !== input) input.value = labelOf(select);
    });
  }

  function init(root) {
    (root || document).querySelectorAll("select[data-search]").forEach(enhance);
  }

  window.seoPicker = { init: init };
  document.addEventListener("seo:load", function () { init(document); });
  init(document);
})();
