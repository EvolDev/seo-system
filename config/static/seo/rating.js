/* Оценка площадки звёздочкой (E1-21): `<button data-rate="<адрес>" data-mine="4">`.
 *
 * Звезду с числом рисует сервер (apps/sites/display.py) перед доменом в
 * «Площадках» и «Размещениях». Нажатие открывает окошко рядом с ней: пять
 * звёзд, наведение подсвечивает, щелчок ставит оценку. Крестик снимает —
 * промах мышью иначе не отменить.
 *
 * Обработчик на document, как у seo/domain-tools.js: подгруженное содержимое
 * (панель, мягкий переход) его не теряет. Без скрипта звезда неактивна —
 * оценка не из тех дел, ради которых нужна отдельная страница.
 *
 * Ответ сервера — JSON со средним, числом оценок и подсказкой; обновляем
 * только саму кнопку, строку не трогаем.
 */
(function () {
  "use strict";

  if (window.seoRating) return;
  window.seoRating = true;

  var STARS = 5;
  var open = null;

  function csrfToken() {
    var input = document.querySelector("input[name=csrfmiddlewaretoken]");
    if (input) return input.value;
    var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  function close() {
    if (!open) return;
    open.box.remove();
    open = null;
  }

  function paint(box, upto) {
    var stars = box.querySelectorAll("[data-value]");
    for (var i = 0; i < stars.length; i++) {
      stars[i].classList.toggle("is-on", i < upto);
    }
  }

  function build(button) {
    var mine = parseInt(button.dataset.mine, 10) || 0;
    var box = document.createElement("div");
    box.className = "seo-rating-pick";
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-label", "Оценить площадку");

    for (var value = 1; value <= STARS; value++) {
      var star = document.createElement("button");
      star.type = "button";
      star.dataset.value = String(value);
      star.className = "seo-rating-star";
      star.title = value + " из " + STARS;
      star.setAttribute("aria-label", value + " из " + STARS);
      star.textContent = "★";
      box.appendChild(star);
    }
    if (mine) {
      var clear = document.createElement("button");
      clear.type = "button";
      clear.className = "seo-rating-clear";
      clear.dataset.value = "";
      clear.title = "Снять оценку";
      clear.setAttribute("aria-label", "Снять оценку");
      clear.textContent = "×";
      box.appendChild(clear);
    }
    paint(box, mine);
    return box;
  }

  function place(box, button) {
    var at = button.getBoundingClientRect();
    box.style.top = (at.bottom + window.scrollY + 4) + "px";
    box.style.left = (at.left + window.scrollX) + "px";
  }

  function send(button, value) {
    var body = new URLSearchParams();
    body.set("value", value === null ? "" : String(value));
    return fetch(button.dataset.rate, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "X-CSRFToken": csrfToken(),
      },
      body: body.toString(),
    }).then(function (response) {
      if (!response.ok) throw new Error("Сервер ответил " + response.status + ".");
      return response.json();
    });
  }

  function show(button, data) {
    var rated = data.count > 0 && data.average !== null;
    button.dataset.mine = data.mine ? String(data.mine) : "";
    button.title = data.title;
    button.setAttribute("aria-label", data.title);
    button.classList.toggle("seo-rating-empty", !rated);
    var number = button.querySelector(".seo-rating-value");
    if (rated) {
      if (!number) {
        number = document.createElement("span");
        number.className = "seo-rating-value";
        button.appendChild(number);
      }
      number.textContent = data.average;
    } else if (number) {
      number.remove();
    }
  }

  document.addEventListener("click", function (event) {
    var star = event.target.closest(".seo-rating-pick [data-value]");
    if (star && open) {
      event.preventDefault();
      var button = open.button;
      var raw = star.dataset.value;
      var value = raw === "" ? null : parseInt(raw, 10);
      close();
      send(button, value).then(function (data) {
        show(button, data);
      }).catch(function (error) {
        window.alert(error.message);
      });
      return;
    }
    if (event.target.closest(".seo-rating-pick")) return;

    var trigger = event.target.closest("[data-rate]");
    if (!trigger) {
      close();
      return;
    }
    event.preventDefault();
    if (open && open.button === trigger) {
      close();
      return;
    }
    close();
    var box = build(trigger);
    document.body.appendChild(box);
    place(box, trigger);
    open = { button: trigger, box: box };
  });

  document.addEventListener("mouseover", function (event) {
    if (!open) return;
    var star = event.target.closest(".seo-rating-pick .seo-rating-star");
    if (star) paint(open.box, parseInt(star.dataset.value, 10));
  });

  document.addEventListener("mouseleave", function (event) {
    if (!open || !event.target.closest) return;
    if (event.target === open.box) {
      paint(open.box, parseInt(open.button.dataset.mine, 10) || 0);
    }
  }, true);

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") close();
  });

  window.addEventListener("resize", close);
  window.addEventListener("scroll", close, true);
})();
