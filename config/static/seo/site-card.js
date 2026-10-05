/* Карточка площадки (E9-13, ADR-058) — в панели справа и отдельной страницей.
 *
 * 1. Щелчок по заголовку раздела сворачивает его до строки со сводкой;
 *    «Свернуть все» / «Развернуть все» — все разделы. Выбор пишется у
 *    пользователя в базе (POST data-sections-url) и действует на все карточки,
 *    в любом браузере; сервер сразу отдаёт свёрнутые свёрнутыми.
 * 2. Вкладки под шапкой — переходы к разделам; свёрнутый раздел при переходе
 *    разворачивается (и это тоже запоминается). Подсвечена вкладка раздела, до
 *    которого дошла прокрутка.
 * 3. Кнопки Google в «Серости» разворачивают раздел только на экране: туда
 *    расширение браузера подставит числа, человек должен их видеть.
 * 4. Ctrl+Enter (⌘+Enter) в поле заметки — «Добавить заметку».
 *
 * Скрипт стоит в разметке карточки и выполняется при каждом её показе:
 * обработчики на document навешиваются один раз, настройка карточки — каждый.
 */
(function () {
  "use strict";

  var SECTION = ".seo-card-sec";

  function cardOf(node) {
    return node && node.closest ? node.closest("[data-site-card]") : null;
  }

  function sections(card) {
    return Array.prototype.slice.call(card.querySelectorAll(SECTION));
  }

  function isClosed(section) {
    return section.classList.contains("seo-closed");
  }

  function setOpen(section, open) {
    section.classList.toggle("seo-closed", !open);
    var toggle = section.querySelector(".seo-card-toggle");
    if (toggle) toggle.setAttribute("aria-expanded", String(open));
  }

  function syncAll(card) {
    var button = card.querySelector("[data-card-all]");
    if (!button) return;
    var all = sections(card);
    var closed = all.filter(isClosed).length;
    button.textContent = all.length && closed === all.length ? "Развернуть все" : "Свернуть все";
  }

  function csrfToken() {
    var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    if (match) return decodeURIComponent(match[1]);
    var input = document.querySelector("input[name=csrfmiddlewaretoken]");
    return input ? input.value : "";
  }

  // Запомнить у пользователя. Не вышло — раздел на экране уже свёрнут,
  // а в следующий раз откроется как был: сказать об этом.
  function remember(card, section, closed) {
    var url = card.dataset.sectionsUrl;
    if (!url) return;
    var data = new FormData();
    data.append("section", section);
    data.append("closed", closed ? "1" : "0");
    fetch(url, {
      method: "POST",
      body: data,
      credentials: "same-origin",
      headers: { "Accept": "application/json", "X-CSRFToken": csrfToken() },
    }).then(function (response) {
      if (!response.ok) throw new Error("сервер ответил " + response.status);
    }).catch(function (error) {
      if (window.seoNav) window.seoNav.toast("Не запомнилось, какие разделы свёрнуты: " + error.message, "error");
    });
  }

  function toggle(section) {
    var card = cardOf(section);
    var open = isClosed(section);
    setOpen(section, open);
    syncAll(card);
    remember(card, section.dataset.section, !open);
  }

  function toggleAll(card) {
    var all = sections(card);
    var open = all.length > 0 && all.every(isClosed);
    all.forEach(function (section) { setOpen(section, open); });
    syncAll(card);
    remember(card, "all", !open);
  }

  function smooth() {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
  }

  function jump(link) {
    var card = cardOf(link);
    var section = card.querySelector(SECTION + '[data-section="' + link.dataset.jump + '"]');
    if (!section) return;
    if (isClosed(section)) {
      setOpen(section, true);
      syncAll(card);
      remember(card, section.dataset.section, false);
    }
    card.seoJumped = section.dataset.section;
    mark(card, section.dataset.section);
    // Отступ под закреплённую шапку — scroll-margin-top раздела (seo/site-card.css).
    section.scrollIntoView({ behavior: smooth(), block: "start" });
  }

  function mark(card, key) {
    card.querySelectorAll("[data-jump]").forEach(function (link) {
      var on = link.dataset.jump === key;
      link.classList.toggle("seo-on", on);
      if (on) link.setAttribute("aria-current", "true");
      else link.removeAttribute("aria-current");
    });
  }

  // ---------- Обработчики на document — один раз ----------

  if (!window.seoSiteCard) {
    window.seoSiteCard = true;

    document.addEventListener("click", function (event) {
      var target = event.target;
      if (!target.closest || !cardOf(target)) return;
      var toggleButton = target.closest(".seo-card-toggle");
      if (toggleButton) {
        toggle(toggleButton.closest(SECTION));
        return;
      }
      var all = target.closest("[data-card-all]");
      if (all) {
        toggleAll(cardOf(all));
        return;
      }
      var link = target.closest("[data-jump]");
      if (link) {
        event.preventDefault();
        jump(link);
        return;
      }
      var google = target.closest("[data-gray-open]");
      if (google) {
        var section = google.closest(SECTION);
        if (section && isClosed(section)) {
          setOpen(section, true);
          syncAll(cardOf(section));
        }
      }
    });

    document.addEventListener("keydown", function (event) {
      if (event.key !== "Enter" || !(event.ctrlKey || event.metaKey)) return;
      var area = event.target;
      var form = area && area.closest ? area.closest(".seo-note-form") : null;
      if (!form || area.tagName !== "TEXTAREA" || !cardOf(form)) return;
      event.preventDefault();
      if (!area.value.trim()) return;
      // requestSubmit — с событием submit: в панели форму отправляет seo/panel.js.
      if (form.requestSubmit) form.requestSubmit();
      else form.submit();
    });
  }

  // ---------- Настройка показанной карточки ----------

  function scrollerOf(card) {
    return card.closest(".seo-panel-body") || null;
  }

  // Вкладка — раздела, до которого дошла прокрутка: верх раздела под шапкой.
  // Долистали до конца — последний, к которому переходили, если он виден.
  function spy(card) {
    var head = card.querySelector(".seo-card-head");
    if (!head) return;
    var line = head.getBoundingClientRect().bottom + 24;
    var current = null;
    sections(card).forEach(function (section) {
      if (section.getBoundingClientRect().top <= line) current = section.dataset.section;
    });
    var scroller = scrollerOf(card) || document.scrollingElement;
    var bottom = scroller && scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 2;
    if (bottom && card.seoJumped) current = card.seoJumped;
    mark(card, current || (sections(card)[0] || {}).dataset.section);
  }

  function setup(card) {
    if (card.seoReady) return;
    card.seoReady = true;
    syncAll(card);
    var head = card.querySelector(".seo-card-head");
    if (head && window.ResizeObserver) {
      new ResizeObserver(function () {
        card.style.setProperty("--seo-card-head", head.offsetHeight + "px");
      }).observe(head);
    } else if (head) {
      card.style.setProperty("--seo-card-head", head.offsetHeight + "px");
    }
    // Панель после записи показывает новую карточку в том же месте: обработчик
    // ушедшей карточки снимает себя сам.
    var target = scrollerOf(card) || window;
    var frame = 0;
    var onScroll = function (event) {
      if (!card.isConnected) {
        target.removeEventListener("scroll", onScroll);
        target.removeEventListener("wheel", onScroll);
        return;
      }
      // Человек прокручивает сам — «последний переход» больше не главный.
      if (event.type === "wheel") card.seoJumped = null;
      if (frame) return;
      frame = requestAnimationFrame(function () {
        frame = 0;
        spy(card);
      });
    };
    target.addEventListener("scroll", onScroll, { passive: true });
    target.addEventListener("wheel", onScroll, { passive: true });
    spy(card);
  }

  document.querySelectorAll("[data-site-card]").forEach(setup);
})();
