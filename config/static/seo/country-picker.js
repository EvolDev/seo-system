/* Выбор страны с флагами и поиском поверх обычного <select> (E1-10, ADR-045).
 *
 * Где: «Страна выгрузки» на экране загрузки Ahrefs и фильтр «Регион» в
 * «Площадках». Без скрипта работает сам <select>. Со скриптом — поле: набираете
 * «сша», «united» или «us», остаются подходящие страны; стрелки, Enter, Esc.
 * Флаг — код страны в data-flag у <option> (кусок общей картинки флагов):
 * флаги-эмодзи Windows не рисует.
 *
 * У <option> можно задать data-search — по чему ещё искать, и data-flag —
 * код страны для флага или «globe». С data-navigate у контейнера выбор открывает адрес из value
 * без перезагрузки страницы (фильтр «Регион», seo/soft-nav.js), иначе пишется в тот же
 * <select> и уходит с формой.
 *
 * Список не закрывается, когда тянут его полосу прокрутки: щелчок по полосе
 * переводит фокус на сам список (tabindex=-1), а закрываемся мы, только если
 * фокус ушёл из поля и списка совсем.
 */
(function () {
  "use strict";

  // Флаг — кусок общей картинки django-countries (flags/sprite-hq.css):
  // «us» → классы flag-u flag-_s; «globe» — значок «все страны».
  function flagImage(code) {
    var span = document.createElement("span");
    span.setAttribute("aria-hidden", "true");
    span.className = code === "globe"
      ? "seo-flag seo-flag-globe"
      : "seo-flag flag-sprite flag-" + code.charAt(0) + " flag-_" + code.charAt(1);
    return span;
  }

  function normalize(text) {
    return String(text || "").trim().toLowerCase().replace(/ё/g, "е");
  }

  function init(node) {
    if (!node || node.getAttribute("data-picker-ready")) return;
    node.setAttribute("data-picker-ready", "1");
    var select = node.querySelector("select");
    if (!select) return;
    var navigate = node.hasAttribute("data-navigate");
    var seen = {};
    var options = [];
    Array.prototype.forEach.call(select.options, function (option) {
      if (seen[option.value]) return;  // страна из «Уже загружали» — один раз, первой
      seen[option.value] = true;
      var group = option.parentNode.tagName === "OPTGROUP" ? option.parentNode.label : "";
      options.push({
        value: option.value,
        label: option.textContent.trim(),
        flag: option.getAttribute("data-flag") || "",
        search: normalize((option.getAttribute("data-search") || "") + " " + option.textContent),
        used: group === "Уже загружали",
      });
    });

    select.hidden = true;
    var field = document.createElement("div");
    field.className = "seo-country-field";
    var flag = document.createElement("span");
    flag.className = "seo-country-flag";
    var input = document.createElement("input");
    input.type = "text";
    input.className = "vTextField seo-country-input";
    input.setAttribute("autocomplete", "off");
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-expanded", "false");
    input.setAttribute("aria-label", node.getAttribute("data-label") || "Страна");
    input.placeholder = node.getAttribute("data-placeholder") || "Начните вводить: США, United States, us";
    var arrow = document.createElement("span");
    arrow.className = "seo-country-arrow";
    arrow.setAttribute("aria-hidden", "true");
    field.appendChild(flag);
    field.appendChild(input);
    field.appendChild(arrow);
    var dropdown = document.createElement("ul");
    dropdown.className = "seo-dropdown seo-country-list";
    dropdown.setAttribute("role", "listbox");
    dropdown.tabIndex = -1;
    dropdown.hidden = true;
    node.appendChild(field);
    node.appendChild(dropdown);
    var active = -1;
    var quiet = false;  // фокус возвращаем в поле сами — список не открывать

    function current() {
      return options.find(function (o) { return o.value === select.value; }) || options[0];
    }
    function showCurrent() {
      var chosen = current();
      input.value = chosen ? chosen.label : "";
      flag.textContent = "";
      if (chosen && chosen.flag) flag.appendChild(flagImage(chosen.flag));
      field.classList.toggle("seo-has-flag", Boolean(chosen && chosen.flag));
    }
    function hide() {
      dropdown.hidden = true;
      dropdown.textContent = "";
      active = -1;
      input.setAttribute("aria-expanded", "false");
    }
    function choose(option) {
      hide();
      if (navigate) {
        if (option.value !== select.value) {
          select.value = option.value;
          showCurrent();
          // Без перезагрузки страницы (seo/soft-nav.js, E9-09); прокрутка на месте.
          var url = new URL(option.value, window.location.href).href;
          if (window.seoNav) window.seoNav.visit(url, { scroll: "keep" });
          else window.location.href = url;
        } else {
          showCurrent();
        }
        return;
      }
      select.value = option.value;
      select.dispatchEvent(new Event("change", { bubbles: true }));
      showCurrent();
      quiet = true;
      input.focus();
      quiet = false;
    }
    function show() {
      var query = normalize(input.value);
      var chosen = current();
      if (chosen && query === normalize(chosen.label)) query = "";
      var list = options.filter(function (o) { return !query || o.search.indexOf(query) !== -1; });
      dropdown.textContent = "";
      active = -1;
      input.setAttribute("aria-expanded", "true");
      if (!list.length) {
        var none = document.createElement("li");
        none.className = "seo-flat seo-country-none";
        none.textContent = "Ничего не нашлось";
        dropdown.appendChild(none);
        dropdown.hidden = false;
        return;
      }
      list.forEach(function (option, index) {
        var item = document.createElement("li");
        item.setAttribute("role", "option");
        item.setAttribute("data-value", option.value);
        if (option.flag) item.appendChild(flagImage(option.flag));
        else item.classList.add("seo-no-flag");
        item.appendChild(document.createTextNode(option.label));
        if (option.used) {
          var mark = document.createElement("span");
          mark.className = "seo-sub";
          mark.textContent = " — уже загружали";
          item.appendChild(mark);
        }
        if (chosen && option.value === chosen.value) {
          item.classList.add("seo-chosen");
          item.setAttribute("aria-selected", "true");
        }
        // mousedown, а не click: фокус не уходит из поля, список не мигает.
        item.addEventListener("mousedown", function (event) { event.preventDefault(); choose(option); });
        dropdown.appendChild(item);
        if (query && index === 0) { active = 0; item.classList.add("seo-active"); }
      });
      dropdown.hidden = false;
      var on = dropdown.querySelector(".seo-chosen");
      if (on && !query) on.scrollIntoView({ block: "nearest" });
    }
    function highlight(step) {
      var items = dropdown.querySelectorAll("li[data-value]");
      if (!items.length) return;
      active = (active + step + items.length) % items.length;
      items.forEach(function (item, i) { item.classList.toggle("seo-active", i === active); });
      items[active].scrollIntoView({ block: "nearest" });
    }
    function pick() {
      var items = dropdown.querySelectorAll("li[data-value]");
      var item = items[active >= 0 ? active : 0];
      if (!item) return;
      var value = item.getAttribute("data-value");
      choose(options.find(function (o) { return o.value === value; }));
    }
    function leave(event) {
      // Фокус ушёл на полосу прокрутки списка или обратно в поле — список открыт.
      var to = event.relatedTarget;
      if (to === input || dropdown.contains(to)) return;
      hide();
      showCurrent();
    }

    input.addEventListener("focus", function () { if (!quiet) { input.select(); show(); } });
    input.addEventListener("click", function () { if (dropdown.hidden) show(); });
    input.addEventListener("input", show);
    input.addEventListener("blur", leave);
    dropdown.addEventListener("blur", leave);
    arrow.addEventListener("mousedown", function (event) {
      event.preventDefault();
      if (dropdown.hidden) { input.focus(); } else { hide(); showCurrent(); }
    });
    node.addEventListener("keydown", function (event) {
      if (event.key === "ArrowDown") { event.preventDefault(); if (dropdown.hidden) show(); highlight(1); }
      else if (event.key === "ArrowUp") { event.preventDefault(); highlight(-1); }
      else if (event.key === "Enter") {
        // Enter выбирает страну, а не отправляет форму.
        event.preventDefault();
        if (!dropdown.hidden) pick();
      } else if (event.key === "Escape") { hide(); showCurrent(); quiet = true; input.focus(); quiet = false; }
      else if (event.target === dropdown && event.key.length === 1) {
        // Начали печатать, пока фокус на списке, — печатаем в поле.
        input.focus();
      }
    });
    showCurrent();
  }

  window.seoCountryPicker = init;

  function initAll() {
    document.querySelectorAll("[data-country-picker][data-auto]").forEach(init);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initAll);
  else initAll();
  // Экран подгружен без перезагрузки (seo/soft-nav.js) — новые поля выбора.
  document.addEventListener("seo:load", initAll);
})();
