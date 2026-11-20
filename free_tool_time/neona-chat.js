/* A single opt-in Neona chat widget for public Agency W sites.
 * No login, cookies, analytics, internal partner APIs or secret tokens.
 */
(function () {
  'use strict';
  if (window.__agencyWPublicNeonaLoaded) return;
  window.__agencyWPublicNeonaLoaded = true;
  var API = 'https://instagram-webhook-zeuv.onrender.com/public/neona/chat';
  var PRIVACY = 'https://instagram-webhook-zeuv.onrender.com/public/neona/privacy';
  var MAIL = 'mailto:polesski.immobilien@gmail.com?subject=' + encodeURIComponent('Вопрос после знакомства с Агентством W');
  var history = [];
  var busy = false;
  var ui = null;

  function node(tag, className, textValue) {
    var e = document.createElement(tag);
    if (className) e.className = className;
    if (textValue !== undefined) e.textContent = textValue;
    return e;
  }
  function addMessage(role, text) {
    var p = node('p', 'neona-chat-msg ' + (role === 'user' ? 'from-user' : 'from-neona'), text);
    ui.log.appendChild(p);
    ui.log.scrollTop = ui.log.scrollHeight;
  }
  function openChat() {
    ui.panel.hidden = false;
    ui.open.setAttribute('aria-expanded', 'true');
    ui.input.focus();
  }
  function closeChat() {
    ui.panel.hidden = true;
    ui.open.setAttribute('aria-expanded', 'false');
    ui.open.focus();
  }
  function build() {
    if (document.getElementById('agency-w-neona')) return;
    var css = node('style');
    css.textContent = [
      '#agency-w-neona{position:fixed;bottom:20px;right:20px;z-index:2147483000;font:15px/1.45 system-ui,-apple-system,Arial,sans-serif;color:#122841}',
      '#agency-w-neona *{box-sizing:border-box}',
      '#agency-w-neona .neona-open{width:auto;display:block;margin-left:auto;background:#edc773;color:#102640;border:1px solid #caa654;border-radius:30px;padding:12px 19px;font-size:15px;font-weight:750;cursor:pointer;box-shadow:0 8px 28px #0007}',
      '#agency-w-neona .neona-panel{width:min(380px,calc(100vw - 24px));height:min(570px,calc(100dvh - 85px));max-height:570px;background:#fff;border:1px solid #cda755;border-radius:15px;box-shadow:0 18px 80px #0009;display:flex;flex-direction:column;overflow:hidden;margin-bottom:10px}',
      '#agency-w-neona .neona-panel[hidden]{display:none}',
      '#agency-w-neona .neona-head{display:flex;justify-content:space-between;align-items:center;padding:13px 15px;background:#0b2845;color:#fff;gap:8px}',
      '#agency-w-neona .neona-head strong{font-size:16px;color:#f4cc80}',
      '#agency-w-neona .neona-close{width:auto;display:inline-block;background:transparent;color:white;border:1px solid #718da9;border-radius:7px;padding:3px 9px;font-size:21px;cursor:pointer}',
      '#agency-w-neona .neona-log{flex:1;overflow:auto;padding:15px;background:#f6f7fa;min-height:0}',
      '#agency-w-neona .neona-chat-msg{max-width:92%;white-space:pre-wrap;overflow-wrap:anywhere;margin:0 0 12px;padding:10px 12px;border-radius:11px;font-size:14px;line-height:1.5;color:#152a42;background:#e7edf2}',
      '#agency-w-neona .neona-chat-msg.from-user{margin-left:auto;background:#163858;color:#fff}',
      '#agency-w-neona .neona-foot{padding:10px 13px 12px;border-top:1px solid #d5dce6;background:#fff}',
      '#agency-w-neona textarea{display:block;width:100%;height:62px;max-height:100px;resize:vertical;border:1px solid #8b9aaa;border-radius:8px;padding:9px;color:#162d45;background:white;font:14px/1.4 system-ui,Arial,sans-serif;text-align:left}',
      '#agency-w-neona .neona-actions{display:flex;align-items:center;justify-content:space-between;margin-top:8px;gap:7px}',
      '#agency-w-neona .neona-send{width:auto;display:inline-block;padding:8px 13px;border:0;border-radius:7px;background:#0e3556;color:white;font-weight:650;font-size:14px;cursor:pointer}',
      '#agency-w-neona .neona-send:disabled{opacity:.5;cursor:wait}',
      '#agency-w-neona .neona-contact{color:#125483;font-size:13px;text-decoration:underline}',
      '#agency-w-neona .neona-consent{display:flex;gap:7px;align-items:flex-start;margin:8px 0;color:#394e63;font-size:12px;line-height:1.35}',
      '#agency-w-neona .neona-consent input{width:15px;height:15px;margin:1px 0 0;flex:0 0 15px}',
      '#agency-w-neona .neona-note{margin:7px 0 0;color:#5a6b7a;font-size:11px}',
      '#agency-w-neona .neona-note a{color:#155b8e;text-decoration:underline}',
      '#agency-w-neona .neona-error{color:#a12626;font-size:12px;min-height:16px;margin:4px 0 0}',
      '.neona-result-box{background:#102d47;border:1px solid #e9bd64;border-radius:12px;padding:17px;margin:16px 0;color:#fff}',
      '.neona-result-box p{margin:8px 0 12px;line-height:1.5;color:#e4e9f0}',
      '.neona-result-box button{display:block;width:100%;padding:12px;border:0;border-radius:9px;background:#f2d18d;color:#082239;font-size:16px;font-weight:750;cursor:pointer}',
      '@media(max-width:500px){#agency-w-neona{right:12px;bottom:12px}#agency-w-neona .neona-panel{height:min(530px,calc(100dvh - 78px))}}'
    ].join('\n');
    document.head.appendChild(css);
    var root = node('div');
    root.id = 'agency-w-neona';
    var panel = node('section', 'neona-panel');
    panel.id = 'neona-chat-panel';
    panel.hidden = true;
    panel.setAttribute('aria-label', 'Разговор с ИИ-секретарём Неоной');
    var head = node('div', 'neona-head');
    var title = node('strong', '', 'Неона · ИИ-секретарь');
    var close = node('button', 'neona-close', '×');
    close.type = 'button'; close.title = 'Закрыть чат'; close.setAttribute('aria-label', 'Закрыть чат');
    head.append(title, close);
    var log = node('div', 'neona-log');
    log.setAttribute('aria-live', 'polite');
    var foot = node('div', 'neona-foot');
    var input = node('textarea');
    input.maxLength = 1200;
    input.placeholder = 'Ваш вопрос Неоне...';
    input.setAttribute('aria-label', 'Ваш вопрос Неоне');
    var consent = node('label', 'neona-consent');
    var check = node('input');
    check.type = 'checkbox'; check.required = true;
    consent.append(check, node('span', '', 'Разрешаю передать мои сообщения ИИ-сервису для ответа. Без регистрации.'));
    var actions = node('div', 'neona-actions');
    var email = node('a', 'neona-contact', 'Связаться с Агентством');
    email.href = MAIL;
    var send = node('button', 'neona-send', 'Отправить');
    send.type = 'button';
    actions.append(email, send);
    var err = node('p', 'neona-error');
    err.setAttribute('role','status');
    var note = node('p', 'neona-note');
    note.append('Не отправляйте личные или платёжные данные. ', (function () {
      var a=node('a','','Конфиденциальность');a.href=PRIVACY;a.target='_blank';a.rel='noopener noreferrer';return a;
    })());
    foot.append(input, consent, actions, err, note);
    panel.append(head, log, foot);
    var open = node('button', 'neona-open', '💬 Спросить Неону');
    open.id = 'neona-w-open'; open.type = 'button';
    open.setAttribute('aria-controls','neona-chat-panel');
    open.setAttribute('aria-expanded','false');
    root.append(panel, open);
    document.body.appendChild(root);
    ui = { panel:panel, open:open, close:close, log:log, input:input, consent:check, send:send, err:err };
    addMessage('assistant', 'Здравствуйте! Я Неона, ИИ-секретарь Агентства W. Помогу разобраться, какие дела можно поручить цифровому помощнику. Что отнимает у вас больше всего времени?');
    open.addEventListener('click',function(){panel.hidden?openChat():closeChat();});
    close.addEventListener('click',closeChat);
    document.addEventListener('keydown',function(e){if(e.key==='Escape'&&!panel.hidden)closeChat();});
    send.addEventListener('click',sendMessage);
    input.addEventListener('keydown',function(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendMessage();}});
    if (document.getElementById('result')) {
      var box = node('div', 'neona-result-box');
      var heading = node('strong', '', 'Хотите вернуть часть этих часов?');
      var text = node('p', '', 'Спросите Неону, какие задачи вы можете делегировать ИИ. Бесплатно и без регистрации.');
      var button = node('button', '', '💬 Поговорить с Неоной');
      button.type='button';button.addEventListener('click',openChat);
      box.append(heading,text,button);
      var result = document.getElementById('result');
      var journal = result.querySelector('.journal-invite');
      result.insertBefore(box,journal||null);
    }
  }
  async function sendMessage() {
    if (!ui || busy) return;
    var msg = ui.input.value.trim();
    if (!msg) { ui.err.textContent='Напишите вопрос.'; return; }
    if (!ui.consent.checked) { ui.err.textContent='Для ответа нужно согласие на обработку сообщения.'; return; }
    ui.err.textContent='';
    busy=true;ui.send.disabled=true;ui.send.textContent='Отвечаю...';
    addMessage('user',msg);ui.input.value='';
    var context=history.slice(-8);
    try {
      var ctrl=new AbortController();
      var timer=setTimeout(function(){ctrl.abort();},25000);
      var response;
      try {
        response=await fetch(API,{method:'POST',mode:'cors',credentials:'omit',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg,history:context}),signal:ctrl.signal});
      } finally {clearTimeout(timer);}
      var data=await response.json();
      if(!response.ok||!data.reply)throw Error(data.error||'Не удалось получить ответ.');
      addMessage('assistant',data.reply);
      history.push({role:'user',content:msg},{role:'assistant',content:data.reply});
      history=history.slice(-8);
    } catch(e) {
      addMessage('assistant','Неона пока не может ответить. Можно попробовать ещё раз или связаться с Агентством по ссылке ниже.');
      ui.err.textContent=e.name==='AbortError'?'Ответ занял слишком много времени.':(e.message||'Ошибка связи.');
    } finally {
      busy=false;ui.send.disabled=false;ui.send.textContent='Отправить';ui.input.focus();
    }
  }
  if (document.readyState==='loading')document.addEventListener('DOMContentLoaded',build,{once:true});
  else build();
})();