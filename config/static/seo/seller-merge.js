/* Граф слияния продавцов (E1-17): клик по узлу делает его главным.
 *
 * Сервер рисует узлы по кругу и стрелки от каждого к середине; скрипт после
 * щелчка переставляет главного в середину, разворачивает стрелки в него и
 * гасит тех, кого вывели из слияния галочкой «не сливать». Без скрипта экран
 * работает как обычная форма: у каждого узла radio-кнопка.
 */
(function () {
  "use strict";

  var form = document.querySelector("[data-merge]");
  if (!form) return;
  var graph = form.querySelector("[data-graph]");
  var nodes = [].slice.call(form.querySelectorAll("[data-node]"));
  var arrows = [].slice.call(form.querySelectorAll("[data-arrow]"));
  var resultName = form.querySelector("[data-result-name]");

  function home(node) {
    return { x: parseFloat(node.style.left), y: parseFloat(node.style.top) };
  }

  var places = {};
  nodes.forEach(function (node) { places[node.dataset.node] = home(node); });

  function targetId() {
    var picked = form.querySelector("[data-target]:checked");
    return picked ? picked.value : null;
  }

  function skipped(id) {
    var box = form.querySelector('[data-skip][value="' + id + '"]');
    return Boolean(box && box.checked);
  }

  function draw() {
    var target = targetId();
    nodes.forEach(function (node) {
      var id = node.dataset.node;
      var isTarget = id === target;
      var place = isTarget ? { x: 50, y: 50 } : places[id];
      node.style.left = place.x + "%";
      node.style.top = place.y + "%";
      node.classList.toggle("is-target", isTarget);
      node.classList.toggle("is-skipped", !isTarget && skipped(id));
      if (isTarget && resultName) {
        var name = node.querySelector(".seo-merge-name");
        resultName.textContent = name ? name.textContent : "";
      }
    });
    arrows.forEach(function (arrow) {
      var id = arrow.dataset.arrow;
      var place = places[id];
      var hidden = id === target || skipped(id);
      arrow.setAttribute("x1", place.x);
      arrow.setAttribute("y1", place.y);
      arrow.setAttribute("x2", 50);
      arrow.setAttribute("y2", 50);
      arrow.classList.toggle("is-off", hidden);
    });
  }

  form.addEventListener("change", function (event) {
    if (event.target.matches("[data-target], [data-skip]")) draw();
  });

  // Щелчок по карточке — выбрать главного; по галочке «не сливать» — не мешать ей.
  graph.addEventListener("click", function (event) {
    if (event.target.closest("[data-skip], .seo-merge-tip")) return;
    var node = event.target.closest("[data-node]");
    if (!node) return;
    var radio = node.querySelector("[data-target]");
    if (radio && !radio.checked) {
      radio.checked = true;
      draw();
    }
  });

  // С клавиатуры: Enter или пробел на карточке — то же самое.
  graph.addEventListener("keydown", function (event) {
    if (event.key !== "Enter" && event.key !== " ") return;
    var card = event.target.closest(".seo-merge-card");
    if (!card) return;
    event.preventDefault();
    var radio = card.closest("[data-node]").querySelector("[data-target]");
    radio.checked = true;
    draw();
  });

  draw();
})();
