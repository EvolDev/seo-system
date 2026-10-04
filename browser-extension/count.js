/* Число из строки оценки Google (E2-06).
 *
 * «About 1,230 results (0.32 seconds)» → 1230, «1 result» → 1,
 * «Page 2 of about 1,230 results» → 1230, «Результатов: примерно 1 230
 * (0,32 сек.)» → 1230. Время в скобках отбрасывается, берётся последнее
 * число: разделители тысяч — запятая, точка или пробел. Числа нет — null.
 *
 * Отдельный файл — чтобы его проверял браузерный тест (tests/e2e/test_gray_scan.py):
 * в расширении он стоит перед google.js в одном списке скриптов и делит с ним
 * глобальную область.
 */
// eslint-disable-next-line no-unused-vars
function seoGrayCount(text) {
  "use strict";
  var clean = String(text || "").replace(/\([^)]*\)/g, " ");
  var groups = clean.match(/\d[\d.,\s  ]*/g);
  if (!groups) return null;
  var digits = groups[groups.length - 1].replace(/\D/g, "");
  return digits ? parseInt(digits, 10) : null;
}
