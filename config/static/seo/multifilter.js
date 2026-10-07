/* Поиск по длинному фильтру с галочками (E1-24): `<input class="seo-multi-search">`.
 *
 * Продавцов полсотни, и нужного искали глазами (просьба пользователя
 * 07.10.2026). Поле сужает пункты на месте, ничего не загружая: отметка
 * по-прежнему ссылка, и фильтр работает без скрипта — тогда поле просто
 * ничего не делает.
 *
 * Обработчик на document, как у seo/domain-tools.js: подгруженное содержимое
 * (мягкий переход, панель) его не теряет.
 */
(function () {
  "use strict";

  if (window.seoMultiFilter) return;
  window.seoMultiFilter = true;

  function fold(text) {
    return text.toLowerCase().replace(/ё/g, "е").trim();
  }

  function filter(input) {
    var box = input.closest(".seo-multi");
    if (!box) return;
    var needle = fold(input.value);
    var items = box.querySelectorAll(".seo-multi-item");
    var shown = 0;
    for (var i = 0; i < items.length; i++) {
      var item = items[i];
      // «Все» — переключатель, а не пункт списка: его не прячем.
      var label = item.querySelector("span:last-child");
      var text = fold(label ? label.textContent : item.textContent);
      var hit = !needle || text.indexOf(needle) !== -1 || i === 0;
      item.hidden = !hit;
      if (hit) shown++;
    }
    var empty = box.querySelector(".seo-multi-empty");
    if (!empty) {
      empty = document.createElement("p");
      empty.className = "seo-multi-empty";
      empty.textContent = "Ничего не нашлось";
      box.querySelector(".seo-multi-list").appendChild(empty);
    }
    empty.hidden = shown > 1 || !needle;
  }

  document.addEventListener("input", function (event) {
    if (event.target.classList.contains("seo-multi-search")) filter(event.target);
  });

  // Esc очищает поле, а не закрывает меню: закрытие стёрло бы и отметки из виду.
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    if (!event.target.classList.contains("seo-multi-search")) return;
    if (!event.target.value) return;
    event.stopPropagation();
    event.target.value = "";
    filter(event.target);
  });
})();
