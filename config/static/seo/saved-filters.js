/* «Мои фильтры» над колонкой фильтров списка (E9-10, ADR-050).
 *
 * Блок рисует сервер (apps/workspace/templatetags/saved_filters.py), скрипт:
 * - выбор набора — переход на список с его фильтрами подгрузкой (seoNav.visit);
 * - «Как в прошлый раз» — последние фильтры этого списка у этого пользователя.
 *   Хранятся в браузере: каждый раз, когда список открыт с фильтрами, они
 *   запоминаются; список открыли без фильтров (из меню) — прошлые остаются;
 * - «Добавить» — поле названия прямо в блоке; название занято — вопрос там же,
 *   кнопка «Заменить»;
 * - «Удалить» — набор помечается удалённым на сервере, в сообщении внизу —
 *   «Отменить».
 * Ответ сервера на «Добавить», «Удалить», «Отменить» — JSON с наборами списка
 * заново: селектор перерисовывается без перехода.
 *
 * Обработчики — на document, один раз: блок приходит с каждым экраном заново,
 * снимать по seo:unload нечего.
 */
(function () {
  "use strict";

  if (window.seoSavedFilters) return;
  window.seoSavedFilters = true;

  var LAST_KEY = "seo.lastFilters.";
  // Как на сервере (apps/workspace/saved_filters.py): номер страницы и
  // служебные метки в набор не идут, пустые поля — тоже.
  var DROPPED = ["p", "e", "_popup", "_to_field", "_changelist_filters"];

  function cleanQuery(search) {
    var out = new URLSearchParams();
    new URLSearchParams(search).forEach(function (value, key) {
      if (value && DROPPED.indexOf(key) === -1) out.append(key, value);
    });
    return out.toString();
  }

  function parts(box) {
    return {
      select: box.querySelector(".seo-saved-select"),
      buttons: box.querySelector("[data-saved-buttons]"),
      remove: box.querySelector("[data-saved-delete]"),
      form: box.querySelector(".seo-saved-form"),
    };
  }

  // ---------- «Как в прошлый раз» ----------

  // Ключ — пользователь и список: за одним браузером могут работать оба.
  function lastKey(box) {
    return LAST_KEY + box.dataset.user + "." + box.dataset.screen;
  }

  function readLast(box) {
    try { return window.localStorage.getItem(lastKey(box)); } catch (error) { return null; }
  }

  function rememberLast(box) {
    var query = cleanQuery(window.location.search);
    if (!query) return;
    try { window.localStorage.setItem(lastKey(box), query); } catch (error) { /* не запомнится */ }
  }

  // ---------- Сервер ----------

  function csrfToken() {
    var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    if (match) return decodeURIComponent(match[1]);
    var input = document.querySelector("input[name=csrfmiddlewaretoken]");
    return input ? input.value : "";
  }

  async function post(url, data) {
    var response = await fetch(url, {
      method: "POST",
      body: new URLSearchParams(data || {}),
      headers: { "Accept": "application/json", "X-CSRFToken": csrfToken() },
      credentials: "same-origin",
    });
    if (!response.ok) throw new Error("сервер ответил " + response.status);
    return response.json();
  }

  function urlFor(box, name, id) {
    return box.dataset[name + "Url"].replace("/0/", "/" + id + "/");
  }

  function toast(text, kind, action) {
    if (window.seoNav) window.seoNav.toast(text, kind, action);
  }

  function failed(error) {
    toast("Не получилось: " + error.message);
  }

  // ---------- Селектор ----------

  // Наборы с сервера — заново; выбран тот, чьи фильтры сейчас на экране.
  function renderSets(box, sets, selectedId) {
    var select = parts(box).select;
    var group = select.querySelector("optgroup");
    if (group) group.remove();
    if (sets.length) {
      group = document.createElement("optgroup");
      group.label = "Сохранённые";
      sets.forEach(function (item) {
        var option = document.createElement("option");
        option.value = String(item.id);
        option.dataset.query = item.query;
        option.textContent = item.name;
        group.appendChild(option);
      });
      select.appendChild(group);
    }
    select.value = selectedId ? String(selectedId) : "";
    refresh(box);
  }

  function currentSetId(sets) {
    var now = cleanQuery(window.location.search);
    var sorted = function (query) {
      return Array.from(new URLSearchParams(query)).map(String).sort().join("&");
    };
    var match = sets.filter(function (item) { return sorted(item.query) === sorted(now); })[0];
    return match ? match.id : null;
  }

  function refresh(box) {
    var bits = parts(box);
    bits.remove.disabled = !/^\d+$/.test(bits.select.value);
    var last = bits.select.querySelector("[data-last]");
    last.disabled = !readLast(box);
    last.title = last.disabled ? "Список ещё не открывали с фильтрами" : "";
  }

  function apply(box, query) {
    var url = box.dataset.listPath + (query ? "?" + query : "");
    if (window.seoNav) window.seoNav.visit(new URL(url, window.location.href).href, { scroll: "keep" });
    else window.location.assign(url);
  }

  document.addEventListener("change", function (event) {
    var select = event.target.closest && event.target.closest(".seo-saved-select");
    if (!select) return;
    var box = select.closest(".seo-saved");
    var option = select.selectedOptions[0];
    if (!option || !option.value) return;
    if (option.hasAttribute("data-last")) {
      var last = readLast(box);
      if (last) apply(box, last);
      return;
    }
    apply(box, option.dataset.query || "");
  });

  // ---------- Добавить ----------

  function openForm(box) {
    var bits = parts(box);
    bits.buttons.hidden = true;
    bits.form.hidden = false;
    resetReplace(bits.form);
    bits.form.elements.name.value = "";
    bits.form.elements.name.focus();
  }

  function closeForm(box) {
    var bits = parts(box);
    bits.form.hidden = true;
    bits.buttons.hidden = false;
    resetReplace(bits.form);
  }

  // Подсказка под полем: «назовите набор» или вопрос о замене — тогда кнопка
  // «Заменить». Правка названия возвращает «Сохранить».
  function showNote(form, message, replace) {
    var note = form.querySelector(".seo-saved-note");
    if (!note) {
      note = document.createElement("p");
      note.className = "seo-saved-note";
      form.elements.name.after(note);
    }
    note.textContent = message;
    if (replace) form.dataset.replace = "1";
    else delete form.dataset.replace;
    form.querySelector("[type=submit]").textContent = replace ? "Заменить" : "Сохранить";
  }

  function resetReplace(form) {
    var note = form.querySelector(".seo-saved-note");
    if (note) note.remove();
    delete form.dataset.replace;
    form.querySelector("[type=submit]").textContent = "Сохранить";
  }

  async function save(box) {
    var form = parts(box).form;
    var data = {
      screen: box.dataset.screen,
      name: form.elements.name.value,
      query: cleanQuery(window.location.search),
    };
    if (form.dataset.replace) data.replace = "1";
    var answer;
    try {
      answer = await post(box.dataset.saveUrl, data);
    } catch (error) {
      failed(error);
      return;
    }
    if (!answer.saved) {
      showNote(form, answer.message, Boolean(answer.exists));
      return;
    }
    closeForm(box);
    renderSets(box, answer.sets, answer.id);
    toast(answer.message, "success");
  }

  // ---------- Удалить и вернуть ----------

  async function remove(box) {
    var id = parts(box).select.value;
    if (!/^\d+$/.test(id)) return;
    var answer;
    try {
      answer = await post(urlFor(box, "delete", id));
    } catch (error) {
      failed(error);
      return;
    }
    renderSets(box, answer.sets, null);
    toast(answer.message, "success", { label: "Отменить", run: function () { restore(id); } });
  }

  // Блок к этому времени мог смениться (перешли на другой экран) — берём тот,
  // что на экране сейчас; адрес «вернуть» у всех блоков один.
  async function restore(id) {
    var box = document.querySelector(".seo-saved");
    if (!box) return;
    var answer;
    try {
      answer = await post(urlFor(box, "restore", id));
    } catch (error) {
      failed(error);
      return;
    }
    // Набор другого списка — селектор этого экрана не трогаем.
    if (answer.restored && box.dataset.screen === answer.screen) {
      renderSets(box, answer.sets, currentSetId(answer.sets));
    }
    toast(answer.message, answer.restored ? "success" : "error");
  }

  // ---------- Нажатия ----------

  document.addEventListener("click", function (event) {
    var button = event.target.closest && event.target.closest(".seo-saved button");
    if (!button) return;
    var box = button.closest(".seo-saved");
    if (button.hasAttribute("data-saved-add")) openForm(box);
    else if (button.hasAttribute("data-saved-cancel")) closeForm(box);
    else if (button.hasAttribute("data-saved-delete")) remove(box);
  });

  // На document — раньше перехвата форм в soft-nav.js (он на window).
  document.addEventListener("submit", function (event) {
    var form = event.target.closest && event.target.closest(".seo-saved-form");
    if (!form) return;
    event.preventDefault();
    save(form.closest(".seo-saved"));
  });

  document.addEventListener("input", function (event) {
    var form = event.target.closest && event.target.closest(".seo-saved-form");
    if (form && form.dataset.replace) resetReplace(form);
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    var form = event.target.closest && event.target.closest(".seo-saved-form");
    if (form) closeForm(form.closest(".seo-saved"));
  });

  function start() {
    var box = document.querySelector(".seo-saved");
    if (!box) return;
    rememberLast(box);
    refresh(box);
  }

  // Первый seo:load soft-nav.js шлёт, когда этот скрипт ещё не выполнен (оба
  // с defer, он — раньше): страница уже разобрана — настраиваем сами. Второй
  // раз на том же экране ничего не меняет.
  document.addEventListener("seo:load", start);
  if (document.readyState !== "loading") start();
})();
