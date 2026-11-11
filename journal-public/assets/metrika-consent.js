/* Agency W Journal · Yandex Metrica 114194345
 * The third-party analytics tag is NEVER requested before explicit opt-in.
 * Declining requires no cookies and leaves the journal fully functional.
 * This script is used on the main page and on every editorial article.
 */
(function () {
  "use strict";
  var COUNTER_ID = 114194345;
  var CONSENT_KEY = "agency_w_journal_metrika_consent_v1";
  var loaded = false;
  var initialChoice = null;
  function readConsent() {
    try { return window.localStorage.getItem(CONSENT_KEY); }
    catch (error) { return null; }
  }
  function writeConsent(value) {
    try { window.localStorage.setItem(CONSENT_KEY, value); }
    catch (error) { /* Storage blocked: consent lasts for this page only. */ }
  }
  function loadCounter() {
    if (loaded) return;
    loaded = true;
    // Only after a recorded opt-in may the browser contact mc.yandex.ru.
    window.ym = window.ym || function () {
      (window.ym.a = window.ym.a || []).push(arguments);
    };
    window.ym.l = Date.now();
    var tag = document.createElement("script");
    tag.async = true;
    tag.src = "https://mc.yandex.ru/metrika/tag.js?id=" + COUNTER_ID;
    document.head.appendChild(tag);
    window.ym(COUNTER_ID, "init", {
      ssr: true,
      clickmap: false,
      trackLinks: true,
      accurateTrackBounce: true,
      webvisor: false
    });
  }
  function initialize() {
    initialChoice = readConsent();
    if (initialChoice === "accepted") loadCounter();

    var style = document.createElement("style");
    style.textContent = [
      "#journal-analytics-consent{position:fixed;bottom:16px;left:16px;right:16px;z-index:99999;",
      "max-width:790px;margin:0 auto;padding:19px 22px;border:1px solid #d6ac62;",
      "border-radius:12px;background:#0b2236;color:#f6f2e9;box-shadow:0 13px 42px #000b;",
      "font:15px/1.5 system-ui,Arial,sans-serif}",
      "#journal-analytics-consent[hidden]{display:none!important}",
      "#journal-analytics-consent p{margin:0 0 12px;color:#edf0ef}",
      "#journal-analytics-consent a{color:#f5cf85;text-decoration:underline}",
      "#journal-analytics-consent .journal-consent-actions{display:flex;gap:10px;flex-wrap:wrap}",
      "#journal-analytics-consent button{cursor:pointer;padding:9px 17px;font:600 14px system-ui,Arial,sans-serif;",
      "border-radius:7px;border:1px solid #d6ac62}",
      "#journal-analytics-yes{background:#e3ba64;color:#061325}",
      "#journal-analytics-no{background:transparent;color:#f5e9d3}",
      "[data-analytics-settings]{background:none;border:0;color:#e3ba64;cursor:pointer;",
      "font:inherit;text-decoration:underline;padding:0}",
      "@media(max-width:500px){#journal-analytics-consent{bottom:8px;left:8px;right:8px;padding:14px}}"
    ].join("");
    document.head.appendChild(style);

    var banner = document.createElement("aside");
    banner.id = "journal-analytics-consent";
    banner.setAttribute("role", "region");
    banner.setAttribute("aria-label", "Согласие на статистику посещений");
    banner.hidden = initialChoice !== null;
    banner.innerHTML =
      '<p><strong>Можно собирать статистику посещений?</strong> ' +
      'Журнал «Время жить» использует Яндекс Метрику, чтобы понимать, какие статьи интересны читателям. ' +
      'При согласии данные о посещениях передаются Яндексу и могут обрабатываться за пределами ЕС. ' +
      'Без согласия счётчик не загружается. ' +
      '<a href="/privacy.html">Подробнее о данных</a>.</p>' +
      '<div class="journal-consent-actions">' +
      '<button id="journal-analytics-yes" type="button">Разрешить статистику</button>' +
      '<button id="journal-analytics-no" type="button">Не разрешать</button></div>';
    document.body.appendChild(banner);

    banner.querySelector("#journal-analytics-yes").addEventListener("click", function () {
      writeConsent("accepted");
      banner.hidden = true;
      loadCounter();
    });
    banner.querySelector("#journal-analytics-no").addEventListener("click", function () {
      var shouldReload = loaded;
      writeConsent("rejected");
      banner.hidden = true;
      // Removing a running analytics tag is unreliable; reload without it.
      if (shouldReload) window.location.reload();
    });
    document.querySelectorAll("[data-analytics-settings]").forEach(function (control) {
      control.addEventListener("click", function () {
        banner.hidden = false;
        banner.querySelector("#journal-analytics-no").focus();
      });
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initialize, { once: true });
  } else {
    initialize();
  }
}());
