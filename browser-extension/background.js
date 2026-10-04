/* Фоновая часть расширения: пересылка между вкладкой Google и вкладками админки (E2-06).
 *
 * Разрешений не нужно: сообщения расширения доходят только до его же скриптов
 * на страницах, адреса чужих вкладок расширение не видит. Вкладка, из которой
 * открыли Google, спрашивается первой, потом остальные: у скрипта админки
 * (relay.js) ответ есть, у прочих вкладок получателя нет — это не ошибка.
 */
"use strict";

chrome.runtime.onMessage.addListener(function (message, sender, reply) {
  if (!message || typeof message.type !== "string") return undefined;
  if (message.type === "seo-gray-expects") {
    expects(message, sender).then(reply);
    // Ответ придёт позже: канал держим открытым.
    return true;
  }
  if (message.type === "seo-gray-reading") {
    tabsFor(sender).then(function (tabs) {
      tabs.forEach(function (tab) {
        chrome.tabs.sendMessage(tab.id, message).catch(function () { /* не наша вкладка */ });
      });
    });
  }
  return undefined;
});

async function tabsFor(sender) {
  var own = sender.tab ? sender.tab.id : null;
  var opener = sender.tab ? sender.tab.openerTabId : undefined;
  var tabs = (await chrome.tabs.query({})).filter(function (tab) { return tab.id !== own; });
  tabs.sort(function (a, b) { return (b.id === opener) - (a.id === opener); });
  return tabs;
}

async function expects(message, sender) {
  var tabs = await tabsFor(sender);
  for (var i = 0; i < tabs.length; i += 1) {
    try {
      var answer = await chrome.tabs.sendMessage(tabs[i].id, message);
      if (answer && answer.kind) return answer;
    } catch (error) {
      // В вкладке нет скрипта расширения.
    }
  }
  return { kind: null };
}
