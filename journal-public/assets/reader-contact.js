/* Journal «Время жить»: reader-friendly, referral-aware introduction.
 * Referral is transported in the URL, never written to storage.
 * The VK service validates the member code itself before routing a conversation.
 */
(function () {
  "use strict";
  const current = new URL(window.location.href);
  const rawCode = current.searchParams.get("ref") || "";
  const code = /^W[1-9][0-9]{4,14}$/.test(rawCode) ? rawCode : "";

  // Preserve referral attribution when the reader follows another journal page.
  // Do not attach the code to external sites or tracking links.
  if (code) {
    document.addEventListener("click", function (event) {
      const link = event.target.closest && event.target.closest("a[href]");
      if (!link) return;
      const destination = new URL(link.href, window.location.href);
      if (destination.origin !== current.origin) return;
      if (!destination.searchParams.has("ref")) {
        destination.searchParams.set("ref", code);
        link.href = destination.href;
      }
    }, true);
  }

  const target = document.querySelector("main");
  if (!target || document.getElementById("reader-connection")) return;

  const style = document.createElement("style");
  style.textContent = [
    ".reader-connection{max-width:1050px;margin:24px auto 58px;padding:0 24px}",
    ".reader-connection-inner{padding:34px clamp(22px,5vw,46px);border:1px solid #bd9750;",
    "border-radius:14px;background:linear-gradient(125deg,#102e43,#09192b);color:#f5f1e9;box-shadow:0 14px 38px #0005}",
    ".reader-connection .reader-eyebrow{color:#e3ba64;letter-spacing:.16em;font-size:12px;font-weight:600}",
    ".reader-connection h2{font:normal clamp(28px,4vw,42px)/1.2 Georgia,serif;margin:12px 0 15px}",
    ".reader-connection p{color:#d1deea;font-size:17px;line-height:1.65;max-width:730px}",
    ".reader-actions{display:flex;gap:12px;flex-wrap:wrap;margin-top:24px}",
    ".reader-actions a{display:inline-block;text-decoration:none;font:600 15px/1.4 system-ui,Arial,sans-serif;",
    "padding:13px 21px;border:1px solid #e3ba64;border-radius:7px}",
    ".reader-actions a:first-child{color:#061325;background:#e3ba64}",
    ".reader-actions a:last-child{color:#e3ba64;background:transparent}",
    ".reader-connection .reader-note{font-size:13px;color:#b4c6d1;margin:17px 0 0}",
    "@media(max-width:550px){.reader-actions{flex-direction:column}.reader-actions a{text-align:center}}"
  ].join("");
  document.head.appendChild(style);

  const section = document.createElement("section");
  section.id = "reader-connection";
  section.className = "reader-connection";
  section.setAttribute("aria-label", "Связаться после прочтения");
  const panel = document.createElement("div");
  panel.className = "reader-connection-inner";
  const small = document.createElement("div");
  small.className = "reader-eyebrow";
  small.textContent = "ВАШ РАЗГОВОР МОЖЕТ НАЧАТЬСЯ ЗДЕСЬ";
  const title = document.createElement("h2");
  title.textContent = "Понравилось? Давайте знакомиться!";
  const text = document.createElement("p");
  text.textContent = code
    ? "Хотите поделиться мыслями или задать вопрос? Напишите человеку, который познакомил вас с журналом. Цифровой помощник поможет начать беседу."
    : "Хотите поделиться мыслями о статье, задать вопрос или узнать больше? Напишите в редакцию журнала «Время жить».";
  const actions = document.createElement("div");
  actions.className = "reader-actions";
  const contact = document.createElement("a");
  const vk = new URL("https://vk.me/club241237638");
  if (code) vk.searchParams.set("ref", code);
  vk.searchParams.set("ref_source", "agency_w");
  contact.href = vk.toString();
  contact.target = "_blank";
  contact.rel = "noopener noreferrer";
  contact.textContent = code ? "Написать и познакомиться" : "Написать в редакцию";
  const calc = document.createElement("a");
  calc.href = "https://agency-w-time.onrender.com/";
  calc.target = "_blank";
  calc.rel = "noopener noreferrer";
  calc.textContent = "Проверить, куда уходит время";
  actions.append(contact, calc);
  const note = document.createElement("p");
  note.className = "reader-note";
  note.textContent = "Для переписки откроется VK. Читать журнал можно бесплатно и без регистрации. Первое сообщение встретит цифровой помощник Агентства W.";
  panel.append(small, title, text, actions, note);
  section.appendChild(panel);
  target.appendChild(section);
}());
