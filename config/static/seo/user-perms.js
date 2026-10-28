/* Карточка пользователя (ADR-078): роль и особые права.
 *
 * - Под кнопками роли — что даёт выбранная ([data-role-hint]).
 * - Роль «Администратор» — таблица прав скрыта: полный доступ.
 * - Другая роль — её права в таблице отмечены и серые (data-role-perms: роль →
 *   номера прав): отдельно их не дают, на запись они не уходят. Сменили роль —
 *   галочки человека, которые были до этого, возвращаются.
 * - Галочка в заголовке колонки раздела — вся колонка раздела.
 * - Поиск — строки по названию экрана; раздел с найденным раскрывается.
 * - У раздела — сколько прав отмечено своих и по роли.
 *
 * Скрипт приходит с формой (Media виджета): и в панели, и на полной странице,
 * поэтому обработчики — на document, один раз; таблицу настраивает init().
 */
(function () {
  "use strict";

  if (window.seoUserPerms) {
    window.seoUserPerms.init(document);
    return;
  }

  function table(form) {
    return form ? form.querySelector("[data-perm-table]") : null;
  }

  function rolePerms(box) {
    try {
      return JSON.parse(box.getAttribute("data-role-perms") || "{}");
    } catch (error) {
      return {};
    }
  }

  function chosenRole(form) {
    var input = form.querySelector("input[name=role]:checked");
    return input ? input.value : "";
  }

  function boxes(scope) {
    return Array.prototype.slice.call(scope.querySelectorAll("input[data-action]"));
  }

  // Права роли — отмечены и серые; свои галочки помним, чтобы вернуть.
  function applyRole(form) {
    var box = table(form);
    if (!box) return;
    var role = chosenRole(form);
    var admin = role === "admin";
    form.querySelectorAll("[data-role-hint]").forEach(function (hint) {
      hint.hidden = hint.getAttribute("data-role-hint") !== role;
    });
    box.querySelector("[data-perm-admin]").hidden = !admin;
    box.querySelector("[data-perm-body]").hidden = admin;
    var own = new Set((rolePerms(box)[role] || []).map(String));
    boxes(box).forEach(function (input) {
      if (!input.hasAttribute("data-own")) input.setAttribute("data-own", input.checked ? "1" : "");
      var byRole = own.has(input.value);
      input.disabled = byRole;
      input.closest("td").classList.toggle("seo-perm-role", byRole);
      input.checked = byRole || input.getAttribute("data-own") === "1";
      input.title = byRole ? "Есть по роли" : "";
    });
    count(box);
  }

  function count(box) {
    var total = 0;
    box.querySelectorAll("[data-perm-section]").forEach(function (section) {
      var mine = 0;
      var role = 0;
      boxes(section).forEach(function (input) {
        if (input.disabled && input.checked) role += 1;
        else if (input.checked) mine += 1;
      });
      total += mine;
      var parts = [];
      if (mine) parts.push("своих: " + mine);
      if (role) parts.push("по роли: " + role);
      section.querySelector("[data-perm-count]").textContent = parts.join(" · ");
    });
    box.querySelector("[data-perm-total]").textContent = total ? "Особых прав: " + total : "Особых прав нет";
  }

  function search(box, text) {
    var needle = text.trim().toLowerCase();
    box.querySelectorAll("[data-perm-section]").forEach(function (section) {
      var found = 0;
      section.querySelectorAll("[data-perm-row]").forEach(function (row) {
        var hit = !needle || row.getAttribute("data-search").indexOf(needle) !== -1;
        row.hidden = !hit;
        if (hit) found += 1;
      });
      section.hidden = Boolean(needle) && !found;
      if (needle && found) section.open = true;
    });
  }

  document.addEventListener("change", function (event) {
    var target = event.target instanceof Element ? event.target : null;
    if (!target) return;
    var form = target.closest("form");
    if (target.matches("input[name=role]")) {
      applyRole(form);
      return;
    }
    var box = target.closest("[data-perm-table]");
    if (!box) return;
    if (target.matches("[data-perm-column]")) {
      var section = target.closest("[data-perm-section]");
      var action = target.getAttribute("data-perm-column");
      boxes(section).forEach(function (input) {
        if (input.getAttribute("data-action") !== action || input.disabled) return;
        input.checked = target.checked;
        input.setAttribute("data-own", target.checked ? "1" : "");
      });
    } else if (target.matches("input[data-action]")) {
      target.setAttribute("data-own", target.checked ? "1" : "");
    }
    count(box);
  });

  document.addEventListener("input", function (event) {
    var target = event.target instanceof Element ? event.target : null;
    if (!target || !target.matches("[data-perm-search]")) return;
    search(target.closest("[data-perm-table]"), target.value);
  });

  // Enter в поиске не отправляет форму пользователя.
  document.addEventListener("keydown", function (event) {
    var target = event.target instanceof Element ? event.target : null;
    if (event.key === "Enter" && target && target.matches("[data-perm-search]")) event.preventDefault();
  });

  function init(root) {
    (root || document).querySelectorAll("[data-perm-table]").forEach(function (box) {
      applyRole(box.closest("form"));
    });
  }

  window.seoUserPerms = { init: init };
  init(document);
  document.addEventListener("seo:load", function () { init(document); });
})();
