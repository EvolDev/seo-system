/* Админка без перезагрузки страниц (E9-09, ADR-046).
 *
 * Переход по ссылке, фильтр, поиск, сортировка, страницы списка, сохранение
 * формы, действие над отмеченными строками — без полной перезагрузки: скрипт
 * запрашивает страницу сам и меняет только содержимое. Шапка и меню стоят на
 * месте, сверху бежит полоска загрузки, смена плавная (View Transitions, как
 * у переходов между страницами, ADR-038). Адрес в строке всегда настоящий:
 * «Назад» и «Вперёд», новая вкладка, F5 работают как раньше.
 *
 * Порядок перехода:
 * 1. запрос страницы; файл в ответ (Content-Disposition) — скачивается;
 *    вход, ошибка, чужой ответ — обычный переход, как без скрипта;
 * 2. стили нового экрана, которых ещё нет, — до показа: иначе мелькнёт
 *    неоформленная страница;
 * 3. замена: заголовок, классы <body>, крошки, содержимое <main> (боковое
 *    меню остаётся, меняется только подсветка пункта; нет меню на одной из
 *    страниц — меняется весь #main);
 * 4. скрипты нового экрана, которых ещё нет, — по порядку, после замены:
 *    штатные скрипты Django запускаются при выполнении файла и должны
 *    увидеть уже новое содержимое; затем скрипты внутри содержимого;
 * 5. перезапуск штатного, что было загружено раньше (см. restartAdmin):
 *    новое запускается само, и повторный запуск задвоил бы обработчики —
 *    галочка срабатывала бы дважды;
 * 6. событие `seo:load` — наши скрипты навешивают обработчики на новое
 *    содержимое; перед заменой — `seo:unload`, снять старые.
 *
 * Прокрутка: другой экран — наверх; тот же экран с другими фильтрами или
 * сортировкой — на месте; страницы списка — наверх; «Назад» — где была.
 * «Назад» перечитывает экран с сервера (решение пользователя 02.10.2026):
 * цифры всегда свежие.
 *
 * Наши скрипты зовут `window.seoNav`: visit(url) — перейти, reload() —
 * перечитать экран на месте, render(html, url) — показать страницу, которую
 * скрипт получил сам (отправка файла с ходом загрузки), mount(container, doc,
 * source) — показать кусок полученной страницы в контейнере со стилями,
 * скриптами и виджетами (панель записи, E9-11), progress — полоска,
 * toast(text, kind, action) — сообщение внизу справа; action — кнопка в нём,
 * `{ label, run }` («Отменить» после удаления набора фильтров, E9-10).
 * Без скрипта всё работает обычными переходами.
 */
(function () {
  "use strict";

  if (window.seoNav) return;

  // Стили нового экрана не загрузились за это время — показываем без них.
  var STYLE_TIMEOUT_MS = 4000;
  var TOAST_MS = 8000;

  // Штатные скрипты Django, которые при выполнении файла настраивают то, что
  // уже есть на странице, и больше ничего не трогают: их можно выполнить
  // ещё раз для нового содержимого. Скрипты с обработчиками на document
  // (автодополнение, связанные объекты, всплывающее окно темы) и filters.js
  // (вешается на все <details>, и на «Оформление» в шапке) так нельзя —
  // для них свой перезапуск в restartAdmin.
  var RERUN = ["/admin/js/actions.js", "/admin/js/inlines.js"];
  // Скрипты темы, которые без jQuery ничего не делают. Первая страница без
  // jQuery (главная) — после перехода на страницу с ним их надо выполнить.
  var NEED_JQUERY = [
    "/admin_interface/magnific-popup/jquery.magnific-popup.js",
    "/admin_interface/related-modal/related-modal.js",
  ];
  var NAV_SIDEBAR = "/admin/js/nav_sidebar.js";
  // Подсветка текущего пункта меню — классы, которые ставит сервер. Остальные
  // классы меню (свёрнутая группа) ставят скрипты — их не трогаем.
  var MARKS = ["current-model", "current-app"];

  var root = document.documentElement;
  // Не подгружаем: во всплывающем окне связанного объекта (Django закрывает
  // его скриптом ответа) и на странице входа (там нет шапки и меню).
  var off = window.self !== window.top || /[?&]_popup=1/.test(window.location.search)
    || document.body.classList.contains("login");
  var navigation = 0;
  // Адрес показанного содержимого: popstate по якорю — не повод перечитывать.
  var shown = window.location.href;
  var controller = null;
  var posting = false;

  // ---------- Полоска загрузки ----------

  var progress = (function () {
    var bar = null, active = 0, timer = null;
    function node() {
      if (!bar || !bar.isConnected) {
        bar = document.createElement("div");
        bar.className = "seo-topbar";
        bar.setAttribute("aria-hidden", "true");
        document.body.appendChild(bar);
      }
      return bar;
    }
    return {
      start: function () {
        active += 1;
        var el = node();
        clearTimeout(timer);
        el.classList.remove("seo-topbar-done");
        el.style.width = "0";
        void el.offsetWidth; // перезапуск перехода
        el.classList.add("seo-topbar-on");
        el.style.width = "85%";
      },
      done: function () {
        active = Math.max(0, active - 1);
        if (active) return;
        var el = node();
        el.style.width = "100%";
        el.classList.add("seo-topbar-done");
        timer = setTimeout(function () { el.classList.remove("seo-topbar-on"); el.style.width = "0"; }, 400);
      },
    };
  })();

  // ---------- Сообщения ----------

  // kind: error (по умолчанию, висит дольше) или success.
  function toast(text, kind, action) {
    kind = kind || "error";
    var box = document.querySelector(".seo-toast-box");
    if (!box) {
      box = document.createElement("div");
      box.className = "seo-toast-box";
      box.setAttribute("role", "status");
      document.body.appendChild(box);
    }
    var item = document.createElement("div");
    item.className = "seo-toast seo-toast-" + kind;
    item.textContent = text;
    if (action) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "seo-toast-action";
      button.textContent = action.label;
      button.addEventListener("click", function () {
        item.remove();
        action.run();
      });
      item.appendChild(button);
    }
    box.appendChild(item);
    setTimeout(function () { item.remove(); }, kind === "error" ? TOAST_MS * 2 : TOAST_MS);
  }

  // ---------- Что подгружаем, а что оставляем браузеру ----------

  // Корень админки — из ссылки на главную в шапке: не зашиваем «/admin/».
  function adminRoot() {
    var home = document.querySelector("#site-name a");
    return home ? new URL(home.href).pathname : "/admin/";
  }

  function softUrl(url) {
    var target = new URL(url, window.location.href);
    if (target.origin !== window.location.origin) return false;
    var base = adminRoot();
    if (target.pathname.indexOf(base) !== 0) return false;
    // Выход — только POST формой; jsi18n — скрипт, а не страница.
    if (/\/(logout|jsi18n)\/$/.test(target.pathname)) return false;
    // Всплывающее окно связанного объекта открывает тема.
    if (target.searchParams.has("_popup")) return false;
    return true;
  }

  function softLink(link) {
    if (link.hasAttribute("download") || link.hasAttribute("data-no-soft")) return false;
    if (link.target && link.target !== "_self") return false;
    if (link.closest("#djDebug")) return false;
    var href = link.getAttribute("href");
    if (!href || href.charAt(0) === "#" || /^(javascript|mailto|tel):/i.test(href)) return false;
    var target = new URL(link.href);
    // Та же страница со своим якорем — прокрутку к якорю делает браузер.
    if (target.hash && target.pathname === window.location.pathname && target.search === window.location.search) {
      return false;
    }
    return softUrl(target.href);
  }

  function softForm(form) {
    if (form.hasAttribute("data-no-soft") || form.id === "logout-form") return false;
    if (form.target && form.target !== "_self") return false;
    var method = (form.getAttribute("method") || "get").toLowerCase();
    if (method !== "get" && method !== "post") return false;
    // Файл в форме GET браузер отправляет по-своему — не подделываем.
    if (method === "get" && form.querySelector("input[type=file]")) return false;
    return softUrl(actionOf(form));
  }

  // Адрес отправки — атрибутом: у формы с полем name="action" (действия над
  // отмеченными строками) свойство form.action — это поле, а не адрес.
  function actionOf(form) {
    return new URL(form.getAttribute("action") || window.location.href, window.location.href).href;
  }

  // Прокрутка после перехода по ссылке: тот же экран — на месте, кроме
  // страниц списка; другой экран — наверх.
  function scrollFor(element, url) {
    var target = new URL(url, window.location.href);
    if (target.pathname !== window.location.pathname) return "top";
    if (element && element.closest(".paginator, #nav-sidebar, #header, .breadcrumbs")) return "top";
    return "keep";
  }

  // ---------- Запрос ----------

  async function visit(url, options) {
    options = options || {};
    if (off) {
      window.location.assign(url);
      return;
    }
    var method = options.method || "GET";
    var seq = ++navigation;
    if (controller) controller.abort();
    controller = new AbortController();
    var signal = controller.signal;
    var response;
    progress.start();
    try {
      response = await fetch(url, {
        method: method,
        body: options.body,
        credentials: "same-origin",
        signal: signal,
        headers: method === "POST" ? { "X-CSRFToken": csrfToken() } : {},
      });
    } catch (error) {
      progress.done();
      if (error && error.name === "AbortError") return;
      // Сервер не ответил. Страница — обычным переходом (браузер покажет, что
      // не так); форму повторять нельзя — сообщение, введённое остаётся.
      if (method === "GET") {
        window.location.assign(url);
        return;
      }
      toast("Не удалось отправить: нет связи с сервером");
      throw error;
    }
    try {
      if (seq === navigation) await handle(response, url, method, options, seq);
    } catch (error) {
      if (error && error.name === "AbortError") return;
      // Ответ пришёл, но показать его не вышло (ошибка в скрипте) — обычный
      // переход на адрес ответа: после записи формы это страница результата.
      window.console.error("seoNav:", error);
      window.location.assign(response.url || url);
    } finally {
      progress.done();
    }
  }

  async function handle(response, url, method, options, seq) {
    var type = response.headers.get("Content-Type") || "";
    var disposition = response.headers.get("Content-Disposition") || "";
    if (/attachment/i.test(disposition)) {
      await saveFile(response, disposition);
      return;
    }
    if (type.indexOf("text/html") === -1) {
      // Не страница (JSON, картинка) — пусть браузер покажет сам.
      if (method === "GET") window.location.assign(response.url || url);
      else toast("Сервер ответил не страницей (" + (type || response.status) + ")");
      return;
    }
    var html = await response.text();
    if (seq !== navigation) return;
    if (!response.ok) {
      // Ошибка сервера: GET — обычным переходом (сервер покажет страницу
      // ошибки сам), POST повторять нельзя — показываем, что пришло.
      if (method === "GET") window.location.assign(response.url || url);
      else showDocument(html);
      return;
    }
    var finalUrl = response.url || url;
    var push = options.push;
    if (push === undefined) {
      // POST без переадресации (ошибки в форме, промежуточная страница
      // действия) — адрес тот же, новая запись в истории не нужна.
      push = !(method === "POST" && !response.redirected && sameUrl(finalUrl, window.location.href));
    }
    var scroll = options.scroll;
    if (scroll === undefined) scroll = method === "POST" ? "top" : scrollFor(null, finalUrl);
    await render(html, finalUrl, { push: push, scroll: scroll, seq: seq, popstate: options.popstate });
  }

  function sameUrl(a, b) {
    var x = new URL(a, window.location.href), y = new URL(b, window.location.href);
    return x.pathname === y.pathname && x.search === y.search;
  }

  function csrfToken() {
    var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    if (match) return decodeURIComponent(match[1]);
    var input = document.querySelector("input[name=csrfmiddlewaretoken]");
    return input ? input.value : "";
  }

  async function saveFile(response, disposition) {
    var blob = await response.blob();
    var name = fileName(disposition) || "download";
    var link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = name;
    link.hidden = true;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(function () { URL.revokeObjectURL(link.href); }, 60 * 1000);
  }

  // `filename*=utf-8''…` (русские буквы) главнее простого `filename="…"`.
  function fileName(disposition) {
    var star = disposition.match(/filename\*\s*=\s*([^']*)''([^;]+)/i);
    if (star) {
      try { return decodeURIComponent(star[2].trim()); } catch (error) { /* ниже — простое имя */ }
    }
    var plain = disposition.match(/filename\s*=\s*"?([^";]+)"?/i);
    return plain ? plain[1].trim() : "";
  }

  // Страница ошибки после POST: целиком вместо текущей, как показал бы браузер.
  function showDocument(html) {
    document.open();
    document.write(html);
    document.close();
  }

  // ---------- Показ страницы ----------

  // Нужная разметка админки: без <main> (вход, страница ошибки, чужая
  // страница) показываем обычным переходом.
  function adminDocument(doc) {
    return doc.querySelector("main#content-start") && !doc.body.classList.contains("login");
  }

  async function render(html, url, options) {
    options = options || {};
    var seq = options.seq || ++navigation;
    var doc = new DOMParser().parseFromString(html, "text/html");
    if (!adminDocument(doc)) {
      window.location.assign(url);
      return;
    }
    await loadStyles(doc);
    if (seq !== navigation) return;

    var loadedBefore = loadedScripts();
    var hadJQuery = hasJQuery();
    var keepFilter = options.scroll === "keep" ? scrollTopOf("#changelist-filter") : null;
    var y = scrollTarget(options.scroll);
    var replaced = null;

    // Одинаковый адрес (щелчок по текущему пункту меню) — без новой записи,
    // как у браузера. «Назад» сюда уже перенёс историю — запись не трогаем.
    if (options.push && !sameUrl(url, window.location.href)) {
      rememberScroll();
      window.history.pushState(state(y), "", url);
    } else if (!options.popstate) {
      window.history.replaceState(state(y), "", url);
    }
    shown = url;

    document.dispatchEvent(new CustomEvent("seo:unload"));
    var update = function () {
      replaced = swap(doc);
      window.scrollTo(0, y);
      if (keepFilter !== null) setScrollTop("#changelist-filter", keepFilter);
    };
    var reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (document.startViewTransition && !reduced) {
      var transition = document.startViewTransition(update);
      // Содержимое меняется не сразу, а когда браузер снимет картинку старого:
      // скрипты запускаем только после этого, иначе они настроят старое.
      await transition.updateCallbackDone.catch(function () {});
    } else {
      update();
    }
    if (!replaced) {
      window.location.assign(url);
      return;
    }

    dropStyles(doc);
    await loadScripts(doc, loadedBefore);
    await runInlineScripts(replaced.content);
    await restartAdmin(loadedBefore, hadJQuery, replaced.sidebar);
    focusMain();
    // Высота могла вырасти после скриптов (инлайны, виджеты) — «Назад» на место.
    if (typeof options.scroll === "number") window.scrollTo(0, y);
    document.dispatchEvent(new CustomEvent("seo:load", { detail: { url: url } }));
  }

  function scrollTarget(scroll) {
    if (typeof scroll === "number") return scroll;
    if (scroll === "keep") return window.scrollY;
    return 0;
  }

  function scrollTopOf(selector) {
    var node = document.querySelector(selector);
    return node ? node.scrollTop : null;
  }

  function setScrollTop(selector, value) {
    var node = document.querySelector(selector);
    if (node) node.scrollTop = value;
  }

  // Запись истории: наша (popstate её узнаёт) и где была прокрутка.
  function state(y) {
    return { seoNav: true, scrollY: y };
  }

  function rememberScroll() {
    if (off) return;
    var current = window.history.state || {};
    if (current.seoNav && current.scrollY === window.scrollY) return;
    window.history.replaceState(Object.assign({}, current, state(window.scrollY)), "");
  }

  function swap(doc) {
    document.title = doc.title;
    copyAttributes(doc.body, document.body);
    swapCrumbs(doc);
    var main = document.getElementById("main");
    var fresh = doc.getElementById("main");
    if (!main || !fresh) return null;
    var sidebar = document.getElementById("nav-sidebar");
    var freshSidebar = doc.getElementById("nav-sidebar");
    if (sidebar && freshSidebar && markSidebar(sidebar, freshSidebar)) {
      var content = document.getElementById("content-start");
      var freshContent = doc.getElementById("content-start");
      copyAttributes(freshContent, content);
      content.innerHTML = freshContent.innerHTML;
      return { content: content, sidebar: false };
    }
    // Меню появилось или пропало (на главной его нет) — меняем весь #main.
    copyAttributes(fresh, main);
    main.innerHTML = fresh.innerHTML;
    return { content: main, sidebar: Boolean(freshSidebar) };
  }

  function copyAttributes(from, to) {
    Array.prototype.slice.call(to.attributes).forEach(function (attr) {
      if (!from.hasAttribute(attr.name)) to.removeAttribute(attr.name);
    });
    Array.prototype.forEach.call(from.attributes, function (attr) {
      if (to.getAttribute(attr.name) !== attr.value) to.setAttribute(attr.name, attr.value);
    });
  }

  // Крошки — <nav> над #main; на главной их нет.
  function swapCrumbs(doc) {
    var current = crumbsOf(document);
    var fresh = crumbsOf(doc);
    if (current && fresh) current.innerHTML = fresh.innerHTML;
    else if (current) current.remove();
    else if (fresh) {
      var main = document.getElementById("main");
      if (main) main.parentNode.insertBefore(document.importNode(fresh, true), main);
    }
  }

  function crumbsOf(doc) {
    var crumbs = doc.querySelector("#container > nav .breadcrumbs");
    return crumbs ? crumbs.parentNode : null;
  }

  // Боковое меню на всех экранах одно и то же, кроме подсветки текущего
  // пункта: переносим только её — поле фильтра меню и прокрутка остаются.
  // Разметка разошлась (другой набор пунктов) — false, меню заменится целиком.
  function markSidebar(current, fresh) {
    var mine = current.querySelectorAll("*");
    var theirs = fresh.querySelectorAll("*");
    if (mine.length !== theirs.length) return false;
    for (var i = 0; i < mine.length; i += 1) {
      if (mine[i].tagName !== theirs[i].tagName) return false;
    }
    for (var j = 0; j < mine.length; j += 1) {
      MARKS.forEach(function (name) {
        mine[j].classList.toggle(name, theirs[j].classList.contains(name));
      });
      var current = theirs[j].getAttribute("aria-current");
      if (current === null) mine[j].removeAttribute("aria-current");
      else mine[j].setAttribute("aria-current", current);
    }
    return true;
  }

  function focusMain() {
    var active = document.activeElement;
    if (active && active !== document.body && active.isConnected) return;
    var main = document.getElementById("content-start");
    if (main) main.focus({ preventScroll: true });
  }

  // ---------- Стили и скрипты экрана ----------

  function stylesOf(doc) {
    return Array.prototype.map.call(doc.head.querySelectorAll("link[rel=stylesheet][href]"), function (link) {
      return new URL(link.getAttribute("href"), window.location.href).href;
    });
  }

  // Возвращает добавленные стили: панель записи снимает свои, когда закрывается.
  function loadStyles(doc) {
    var have = new Set(stylesOf(document));
    var added = [];
    var waits = stylesOf(doc).filter(function (href) { return !have.has(href); }).map(function (href) {
      return new Promise(function (resolve) {
        var link = document.createElement("link");
        link.rel = "stylesheet";
        link.href = href;
        link.addEventListener("load", resolve);
        link.addEventListener("error", resolve);
        setTimeout(resolve, STYLE_TIMEOUT_MS);
        document.head.appendChild(link);
        added.push(link);
      });
    });
    return Promise.all(waits).then(function () { return added; });
  }

  // Стили прошлого экрана, которых у нового нет (списки — у формы), убираем:
  // экран должен выглядеть так же, как после обычного перехода.
  function dropStyles(doc) {
    var keep = new Set(stylesOf(doc));
    Array.prototype.forEach.call(document.head.querySelectorAll("link[rel=stylesheet][href]"), function (link) {
      if (!keep.has(link.href)) link.remove();
    });
  }

  // Скрипт узнаём по пути без ?v=…: один файл — один раз за жизнь страницы.
  function scriptKey(src) {
    return new URL(src, window.location.href).pathname;
  }

  function loadedScripts() {
    var keys = new Set();
    Array.prototype.forEach.call(document.querySelectorAll("script[src]"), function (script) {
      keys.add(scriptKey(script.src));
    });
    return keys;
  }

  // Новые скрипты <head> — все сразу, выполняются по порядку (async = false):
  // jQuery раньше плагинов, плагины раньше своих настроек.
  async function loadScripts(doc, loadedBefore) {
    var waits = [];
    var seen = new Set(loadedBefore);
    // Плагины jQuery (автодополнение select2, его перевод) цепляются к
    // window.jQuery. При обычной загрузке он ещё виден: jquery.init.js прячет
    // его в django.jQuery позже, после плагинов. jQuery уже загружен прошлым
    // экраном — на время загрузки показываем его снова, потом прячем.
    var shim = hasJQuery() && window.jQuery === undefined;
    if (shim) window.jQuery = window.django.jQuery;
    Array.prototype.forEach.call(doc.head.querySelectorAll("script[src]"), function (old) {
      var key = scriptKey(old.getAttribute("src"));
      if (seen.has(key)) return;
      seen.add(key);
      var script = cloneScript(old);
      waits.push(waitScript(script));
      document.head.appendChild(script);
    });
    await Promise.all(waits);
    if (shim && window.jQuery === window.django.jQuery) window.jQuery = undefined;
  }

  // Скрипты внутри нового содержимого вставлены как текст и сами не
  // выполняются — заменяем их живыми копиями по порядку.
  async function runInlineScripts(container) {
    var scripts = Array.prototype.slice.call(container.querySelectorAll("script"));
    for (var i = 0; i < scripts.length; i += 1) {
      var old = scripts[i];
      var type = (old.getAttribute("type") || "").toLowerCase();
      // Данные (application/json) — не код, их читают сами скрипты.
      if (type && type !== "text/javascript" && type !== "module") continue;
      var script = cloneScript(old);
      var wait = old.hasAttribute("src") ? waitScript(script) : null;
      old.replaceWith(script);
      if (wait) await wait;
    }
  }

  function cloneScript(old) {
    var script = document.createElement("script");
    Array.prototype.forEach.call(old.attributes, function (attr) {
      script.setAttribute(attr.name, attr.value);
    });
    script.removeAttribute("defer");
    script.async = false;
    script.textContent = old.textContent;
    return script;
  }

  function waitScript(script) {
    return new Promise(function (resolve) {
      script.addEventListener("load", resolve);
      script.addEventListener("error", resolve);
    });
  }

  // Выполнить файл ещё раз: живая копия на месте старого тега.
  function rerun(path) {
    var old = Array.prototype.find.call(document.querySelectorAll("script[src]"), function (script) {
      return scriptKey(script.src).slice(-path.length) === path;
    });
    if (!old) return Promise.resolve();
    var script = cloneScript(old);
    var wait = waitScript(script);
    old.replaceWith(script);
    return wait;
  }

  // Был ли файл на странице до перехода. Пути в наборе — с /static/ впереди,
  // сравниваем окончание.
  function wasLoaded(loadedBefore, path) {
    return Array.from(loadedBefore).some(function (key) { return key.slice(-path.length) === path; });
  }

  // Штатные скрипты Django и темы, которые запускаются один раз при загрузке
  // страницы. Новые (загруженные этим переходом) уже запустились сами.
  async function restartAdmin(loadedBefore, hadJQuery, sidebarReplaced) {
    var was = function (path) { return wasLoaded(loadedBefore, path); };
    var again = RERUN.slice();
    if (!hadJQuery && hasJQuery()) again = again.concat(NEED_JQUERY);
    if (sidebarReplaced) again.push(NAV_SIDEBAR);
    // Файлы выполняются по порядку вставки: плагин раньше того, кто его зовёт.
    await Promise.all(again.filter(was).map(rerun));
    if (was("/admin/js/filters.js")) restoreFilters();
    restartWidgets(document, loadedBefore);
    // Сворачивание групп меню (тема) — тоже по load: window.onload.
    if (typeof window.onload === "function") window.onload(new Event("load"));
  }

  // Виджеты полей формы внутри scope (страница или панель записи).
  function restartWidgets(scope, loadedBefore) {
    var $ = hasJQuery() ? window.django.jQuery : null;
    var was = function (path) { return wasLoaded(loadedBefore, path); };
    // Автодополнение: его скрипт слушает document — выполнять заново нельзя.
    if ($ && $.fn.djangoAdminSelect2 && was("/admin/js/autocomplete.js")) {
      $(scope).find(".admin-autocomplete").not("[name*=__prefix__]").djangoAdminSelect2();
    }
    // Ссылки «изменить / посмотреть» у выбранного связанного объекта.
    if ($ && was("/admin/js/admin/RelatedObjectLookups.js")) {
      $(scope).find(".related-widget-wrapper select").trigger("change");
    }
    // Календарь, часы и выбор из двух списков ждут события load у окна, а оно
    // было один раз. Старые окошки календаря остаются скрытыми в <body>: их
    // ищут обработчики прошлого экрана, удалять нельзя. Календарь ищет поля
    // по всей странице и второй раз добавил бы значки к уже настроенным —
    // зовём, только когда в scope есть поля даты.
    if (window.DateTimeShortcuts && scope.querySelector("input.vDateField, input.vTimeField")) {
      window.DateTimeShortcuts.init();
    }
    if (window.SelectFilter) {
      scope.querySelectorAll("select.selectfilter, select.selectfilterstacked").forEach(function (el) {
        window.SelectFilter.init(el.id, el.dataset.fieldName, parseInt(el.dataset.isStacked, 10));
      });
    }
  }

  // ---------- Кусок страницы в контейнере (панель записи, E9-11) ----------

  // Показать в container содержимое source из ответа doc (разобранного
  // DOMParser) — так панель записи показывает форму с сервера. Порядок тот же,
  // что у перехода: стили — до показа, скрипты — после вставки (штатные скрипты
  // Django настраивают то, что уже на странице), затем скрипты внутри куска.
  // Перезапуск штатного — только внутри container: список под панелью уже
  // настроен, повторный actions.js задвоил бы его обработчики. inlines.js
  // ищет наборы строк по всей странице, но под панелью их нет.
  // Возвращает стили, добавленные ради куска.
  async function mount(container, doc, source) {
    var added = await loadStyles(doc);
    var loadedBefore = loadedScripts();
    var hadJQuery = hasJQuery();
    container.innerHTML = source.innerHTML;
    await loadScripts(doc, loadedBefore);
    await runInlineScripts(container);
    var again = ["/admin/js/inlines.js"];
    if (!hadJQuery && hasJQuery()) again = again.concat(NEED_JQUERY);
    await Promise.all(again.filter(function (path) { return wasLoaded(loadedBefore, path); }).map(rerun));
    restartWidgets(container, loadedBefore);
    return added;
  }

  // Что делает filters.js: свёрнутые и раскрытые фильтры списка помнятся
  // в sessionStorage. Только для фильтров — без <details> в шапке.
  function restoreFilters() {
    var key = "django.admin.filtersState";
    var read = function () {
      try { return JSON.parse(window.sessionStorage.getItem(key)) || {}; } catch (error) { return {}; }
    };
    var saved = read();
    document.querySelectorAll("#changelist-filter details[data-filter-title]").forEach(function (detail) {
      var title = detail.dataset.filterTitle;
      if (title in saved) detail.open = Boolean(saved[title]);
      detail.addEventListener("toggle", function () {
        var now = read();
        now[title] = detail.open;
        try { window.sessionStorage.setItem(key, JSON.stringify(now)); } catch (error) { /* не запомнится */ }
      });
    });
  }

  function hasJQuery() {
    return Boolean(window.django && window.django.jQuery);
  }

  // ---------- Перехват ссылок и форм ----------

  // На window, а не на document: свои обработчики (карточка площадки,
  // проверка индексации, экраны загрузки) срабатывают раньше и, если взяли
  // нажатие себе, отменяют его — тогда здесь ничего не делаем.
  window.addEventListener("click", function (event) {
    if (off || event.defaultPrevented || event.button !== 0) return;
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    var link = event.target.closest && event.target.closest("a[href]");
    if (!link || !softLink(link)) return;
    event.preventDefault();
    visit(link.href, { scroll: scrollFor(link, link.href) });
  });

  window.addEventListener("submit", function (event) {
    if (off || event.defaultPrevented) return;
    var form = event.target;
    if (!softForm(form)) return;
    var submitter = event.submitter || null;
    if (submitter && (submitter.hasAttribute("formaction") || submitter.hasAttribute("formmethod")
        || submitter.hasAttribute("formtarget"))) return;
    event.preventDefault();
    var method = (form.getAttribute("method") || "get").toLowerCase();
    if (method === "get") {
      var url = new URL(actionOf(form));
      url.search = new URLSearchParams(formData(form, submitter)).toString();
      visit(url.href, { scroll: scrollFor(form, url.href) });
      return;
    }
    // Второе нажатие, пока идёт отправка, — не отправляем дважды.
    if (posting) return;
    posting = true;
    var body = formData(form, submitter);
    var buttons = form.querySelectorAll("button[type=submit], input[type=submit], button:not([type])");
    buttons.forEach(function (button) { button.disabled = true; });
    if (submitter && submitter.tagName === "BUTTON") submitter.classList.add("seo-btn-busy");
    visit(actionOf(form), { method: "POST", body: body })
      .catch(function () {})
      .finally(function () {
        posting = false;
        // Страница не сменилась (ошибка связи) — кнопки снова доступны.
        if (form.isConnected) {
          buttons.forEach(function (button) { button.disabled = false; });
          if (submitter) submitter.classList.remove("seo-btn-busy");
        }
      });
  });

  // ---------- Фильтры списка ----------
  //
  // Выпадающие фильтры темы и «от — до» пакета rangefilter открывали адрес
  // через window.location — перезагрузка целиком. Их скрипты убраны из наших
  // шаблонов (admin_interface/dropdown_filter.html, rangefilter/numeric_filter.html),
  // выбор открываем здесь. Прокрутка остаётся на месте.

  function filterVisit(url) {
    visit(new URL(url, window.location.href).href, { scroll: "keep" });
  }

  document.addEventListener("change", function (event) {
    var select = event.target.closest && event.target.closest(".list-filter-dropdown select");
    if (select && select.value) filterVisit(select.value);
  });

  // «Найти» и «Сбросить» у «от — до»; Enter в поле браузер превращает в
  // нажатие «Найти». К уже выбранным фильтрам (скрытое поле *-query-string)
  // добавляются значения полей — как делал скрипт пакета.
  document.addEventListener("click", function (event) {
    var button = event.target.closest
      && event.target.closest(".numericrangefilter input[type=submit], .numericrangefilter input[type=reset]");
    if (!button) return;
    event.preventDefault();
    var box = button.closest(".numericrangefilter");
    var query = box.querySelector("input[id$=-query-string]");
    var base = window.location.pathname + (query ? query.value : "?");
    if (button.type === "reset") {
      filterVisit(base);
      return;
    }
    var values = new URLSearchParams(new FormData(box.querySelector("form"))).toString();
    filterVisit(base + (base.slice(-1) === "?" ? "" : "&") + values);
  });

  // Кнопка, которой отправили форму, тоже уходит на сервер («Сохранить и
  // продолжить», «Выполнить» у действий).
  function formData(form, submitter) {
    try {
      return new FormData(form, submitter);
    } catch (error) {
      var data = new FormData(form);
      if (submitter && submitter.name) data.append(submitter.name, submitter.value);
      return data;
    }
  }

  window.addEventListener("popstate", function (event) {
    if (off) return;
    // Сменился только якорь (#…) — прокручивает браузер, содержимое то же.
    if (sameUrl(window.location.href, shown)) return;
    var y = event.state && event.state.seoNav ? event.state.scrollY || 0 : 0;
    visit(window.location.href, { push: false, popstate: true, scroll: y });
  });

  // Где прокрутка — в запись истории: по «Назад» браузер уже перейдёт на
  // прошлую запись, и обновить уходящую будет поздно. Сохраняем, когда
  // прокрутка остановилась: браузер ограничивает частые replaceState.
  var scrollTimer = null;
  window.addEventListener("scroll", function () {
    clearTimeout(scrollTimer);
    scrollTimer = setTimeout(rememberScroll, 150);
  }, { passive: true });
  // Уход обычным путём (F5, другой сайт) — тоже: браузер сам прокрутку
  // больше не восстанавливает (scrollRestoration = manual).
  window.addEventListener("pagehide", rememberScroll);

  function start() {
    if (!off) {
      var current = window.history.state;
      var restoreY = current && current.seoNav ? current.scrollY : null;
      window.history.scrollRestoration = "manual";
      window.history.replaceState(Object.assign({}, current || {}, state(window.scrollY)), "");
      // F5 и возврат после обычного перехода: прокрутку возвращаем сами.
      var entry = window.performance && performance.getEntriesByType
        ? performance.getEntriesByType("navigation")[0] : null;
      if (restoreY && entry && (entry.type === "reload" || entry.type === "back_forward")) {
        window.scrollTo(0, restoreY);
      }
    }
    document.dispatchEvent(new CustomEvent("seo:load", { detail: { url: window.location.href } }));
  }

  window.seoNav = {
    visit: visit,
    render: function (html, url) { return render(html, url, { push: !sameUrl(url, window.location.href), scroll: "top" }); },
    reload: function () { return visit(window.location.href, { push: false, scroll: "keep" }); },
    mount: mount,
    progress: progress,
    toast: toast,
    enabled: !off,
  };

  // Палитра и прочее в <head> уже на месте; seo:load — когда разметка готова.
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();

  root.dataset.softNav = off ? "off" : "on";
})();
