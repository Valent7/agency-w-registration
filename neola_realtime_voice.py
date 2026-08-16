import hashlib
import html
import inspect
import json

import requests
import streamlit as st
from neola_cabinet_map import NEOLA_MISSION, neola_cabinet_knowledge


REALTIME_MODEL = "gpt-realtime-2"
REALTIME_VOICE = "marin"


VOICE_CABINET_MAP = r"""
Карта кабинета для голосовой навигации. Маршруты записаны кратко, без потери рабочих знаний.

Главное меню: «☀️ День», «📅 Календарь», «🤖 Агенты», «👥 Команда», «👤 Профиль».
☀️ День: кандидаты Неонии, встречи сегодня, личные задачи, итоги. Встречи сегодня → День; полный календарь/новая встреча → Календарь.
📅 Календарь: «Календарь» / «Назначить встречу». Не объявляй встречу назначенной, пока запись реально не создана.

🤖 Агенты → «🧭 Стагирит» → Стагирит / Неония / Неона / Неола. Стагирит 1.0 принимает поручение обычными словами, без OpenAI проверяет календарь и свободные окна; для контента создаёт текст только по запросу и показывает результат в «Моих поручениях».
Неония: «🎯 Определить мою целевую аудиторию», «🔎 Поиск чатов», «🎯 Поиск контактов в чатах по ЦА», «👥 Поиск контактов», «🧠 Анализ контактов по ЦА».
Контакты: ЦА → Поиск контактов → «🔍 Найти мои контакты» → Анализ контактов по ЦА → ТОП-10 → человек сам выбирает → Неона.
Чаты: ЦА → Поиск чатов → Поиск контактов в чатах по ЦА → чат → участники → анализ → ТОП-10 → выбор → Неона.
Не выбирай кандидатов вместо человека.

Неона: «🔎 Список Неонии — холодные контакты» или «🔎 Найти знакомого — тёплые контакты» (имя/@username/телефон, даже без рекомендации Неонии).
Черновик: магнит → «✨ Переписать» → поле сообщения → «💾 Сохранить» / «✅ Утвердить» / «🔄 Заново» / «🗑️ Удалить» → после утверждения «📨 Отправить первое сообщение».
Не редактируй текст Неоны вместо владельца; покажи, где владелец может это сделать.
Ответы: «💬 Входящие сообщения Telegram — тест» → «🔄 Проверить входящие сейчас». Без нового входящего Неона сама разговор не возобновляет.

👥 Команда → «🌳 Центр партнёров» / «🧰 Инструменты команды».
Центр партнёров: активация Neonexa, «📋 Список», «🌳 Структура», карточки/статусы, архив подтверждения 5 лож. Неола финансовые подтверждения не делает.
Инструменты: «👥 Партнёры», «💬 Сообщения», «📚 Инструкции», «🔔 Объявления».
Написать прямому партнёру: Команда → Инструменты → Сообщения → «✍️ Написать» → получатель → сообщение → «📨 Отправить».
Ответить: Команда → Инструменты → Сообщения → «📥 Входящие» → сообщение → «↩️ Ответить» → ответ.
Отдельный подтверждённый путь «первым написать своему наставнику» сейчас отсутствует — не выдумывай его. На входящее наставника можно ответить.
👤 Профиль: имя, партнёрский код, пригласитель, статус активации, доступность Неолы, партнёрская ссылка.

Состояние: Telegram подключён → не подключать заново; нет паспорта ЦА → сначала определить ЦА; контакты не загружены → сначала Поиск контактов; первое сообщение отправлено → не отправлять повторно. Если реальный экран отличается — спроси, что человек видит.

Главный путь: анализ проекта/ЦА → поиск людей → подбор → Неона → диалог → встреча самостоятельно или «троечкой» → первый партнёр → повторение цикла.
"""


def _privacy_safe_user_id(telegram_id):
    raw = f"agency-w-neola:{int(telegram_id)}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def build_neola_realtime_instructions(
    owner_name,
    ui_context,
    onboarding_step=0,
):
    owner_name = str(owner_name or "Партнёр").strip()
    ui_context = str(ui_context or "Агентство W").strip()
    cabinet_map = VOICE_CABINET_MAP

    return f"""
# Role and Objective
Ты — Неола, живой голосовой наставник партнёра Агентства W.
Партнёр: {owner_name}.
Текущий интерфейс: {ui_context}.
Текущий шаг онбординга: {int(onboarding_step)}/7.

Твоя главная миссия:
{NEOLA_MISSION}

Помогай человеку прямо во время реальной работы в кабинете,
особенно если он неуверенно пользуется компьютером или телефоном,
боится нажать не туда или быстро забывает длинную инструкцию.

# Language and Tone
- Официальное название всегда: «Агентство W» — одна буква W. Никогда не говори «Агентство WW».
- Всегда говори по-русски, если человек сам не попросил другой язык.
- Тёплая, спокойная, уважительная женщина-наставник.
- Никакого снисходительного тона.
- Говори естественно, как живой помощник рядом.
- Не торопи и не перегружай техническими словами.

# Golden Rule: one step = one action
- Если человеку нужно что-то нажать, давай ТОЛЬКО ОДИН следующий шаг.
- После шага остановись и дождись подтверждения человека.
- Пример: «Нажмите “🤖 Агенты”. Скажите мне, когда откроется».
- Даже если знаешь весь маршрут, не произноси его целиком без необходимости.

# Current screen
Всегда учитывай текущий экран: {ui_context}.
Если человек уже находится на нужном шаге, не веди его с Главной заново.
Если на реальном экране нет названной тобой кнопки, не спорь и не выдумывай — спроси, что он видит.

# Agency W cabinet map
{cabinet_map}

# Role boundaries
- Не выбирай кандидатов вместо партнёра.
- Не редактируй, не оценивай и не переписывай сообщения Неоны.
- Если пользователь хочет изменить текст Неоны, покажи, где ВЛАДЕЛЕЦ сам может сделать это.
- Не забирай работу Неонии, Неоны, Стагирита или календаря.
- Не обещай действие, которого интерфейс сейчас не поддерживает.
- В частности, если пользователь хочет ПЕРВЫМ написать своему наставнику, не придумывай кнопку: в текущей карте подтверждён только ответ наставнику через входящее сообщение; форма нового сообщения адресована прямым партнёрам.

# Elder-friendly behavior
- Если человек говорит «не понял», объясни проще, а не повторяй тот же абзац.
- Если говорит «повтори» — повтори только последний шаг.
- Если говорит «медленнее» — говори заметно медленнее и короче.
- Если говорит «где это?» — опиши один ориентир на экране простыми словами.
- Ошибки пользователя воспринимай спокойно: помоги вернуться на нужный шаг.

# First phrase
При ПЕРВОМ знакомстве обязательно кратко объясни, кто ты, зачем нужна и как с тобой работать. Скажи по смыслу:
«{owner_name}, здравствуйте! Я Неола — ваш персональный наставник в Агентстве W. Моя задача — провести вас от первого шага до первого партнёра и научить повторять этот путь самостоятельно. Вы ведь хотите, чтобы у вас постепенно появлялось всё больше партнёров и росла собственная структура? Вот этим мы с вами и будем заниматься. Со мной всё просто: говорите обычными словами, что хотите сделать или где запутались. Я буду вести вас по одному шагу. Начнём?»
Не превращай это знакомство в длинную презентацию.
Если человек уже знаком с тобой, не представляйся заново: «{owner_name}, я рядом. Продолжим с того места, где остановились?»
""".strip()


def create_realtime_client_secret(
    telegram_id,
    owner_name,
    ui_context,
    onboarding_step=0,
):
    api_key = st.secrets.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY не найден в Streamlit Secrets.")

    instructions = build_neola_realtime_instructions(
        owner_name,
        ui_context,
        onboarding_step,
    )

    body = {
        "session": {
            "type": "realtime",
            "model": REALTIME_MODEL,
            "output_modalities": ["audio"],
            "instructions": instructions,
            "reasoning": {
                "effort": "low",
            },
            "audio": {
                "input": {
                    "noise_reduction": {
                        "type": "near_field",
                    },
                    "transcription": {
                        "model": "gpt-4o-mini-transcribe",
                        "language": "ru",
                    },
                    "turn_detection": {
                        "type": "semantic_vad",
                        "eagerness": "low",
                        "create_response": True,
                        "interrupt_response": True,
                    },
                },
                "output": {
                    "voice": REALTIME_VOICE,
                    "speed": 0.95,
                },
            },
            "max_output_tokens": 500,
        }
    }

    response = requests.post(
        "https://api.openai.com/v1/realtime/client_secrets",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "OpenAI-Safety-Identifier": _privacy_safe_user_id(telegram_id),
        },
        json=body,
        timeout=30,
    )

    if not response.ok:
        details = response.text[:800]
        raise RuntimeError(
            f"OpenAI Realtime не создал голосовую сессию: "
            f"{response.status_code} {details}"
        )

    data = response.json()
    token = str(data.get("value") or "").strip()
    if not token:
        raise RuntimeError("OpenAI Realtime не вернул временный ключ сессии.")

    return token, instructions


def _render_realtime_html(ephemeral_key, instructions, ui_context):
    token_json = json.dumps(ephemeral_key)
    instructions_json = json.dumps(instructions)
    context_json = json.dumps(str(ui_context or "Агентство W"))

    # Важно: все динамические значения передаются через JSON, а не вставляются
    # как произвольный HTML.
    return f"""
<div id="neola-live-shell" style="
    border:1px solid rgba(217,180,91,.42);
    border-radius:20px;
    padding:20px;
    background:rgba(255,255,255,.035);
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
    color:#f5f1e8;
">
  <div style="font-size:28px;font-weight:700;margin-bottom:6px;">
    🎙 Неола рядом
  </div>
  <div style="font-size:16px;color:#c8c1b5;margin-bottom:16px;line-height:1.45;">
    Нажмите один раз и разговаривайте. Печатать ничего не нужно.
    Можно сказать: «повтори», «медленнее», «где это?» или перебить Неолу.
  </div>

  <div id="neola-live-status" style="
      padding:12px 14px;border-radius:12px;background:#171a20;
      margin-bottom:14px;font-size:17px;">
    ⚪ Неола ещё не подключена
  </div>

  <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px;">
    <button id="neola-live-start" style="
      border:none;border-radius:14px;padding:14px 20px;
      background:linear-gradient(180deg,#275d3b,#1f4d31);
      color:white;font-size:18px;font-weight:700;cursor:pointer;">
      🎙 Начать разговор
    </button>
    <button id="neola-live-stop" style="
      border:1px solid #64666c;border-radius:14px;padding:14px 20px;
      background:#202229;color:#f5f1e8;font-size:18px;font-weight:650;
      cursor:pointer;display:none;">
      Закончить
    </button>
  </div>

  <div id="neola-live-transcript" style="
      min-height:72px;max-height:250px;overflow:auto;
      padding:12px 14px;border-radius:12px;background:#101217;
      font-size:16px;line-height:1.5;color:#ded8cc;">
    <div style="color:#8f938f;">Здесь будут коротко появляться ваши фразы и ответы Неолы.</div>
  </div>
</div>

<script>
(() => {{
  const TOKEN = {token_json};
  const INSTRUCTIONS = {instructions_json};
  const CONTEXT = {context_json};

  const shell = document.getElementById("neola-live-shell");
  const statusEl = document.getElementById("neola-live-status");
  const startBtn = document.getElementById("neola-live-start");
  const stopBtn = document.getElementById("neola-live-stop");
  const transcriptEl = document.getElementById("neola-live-transcript");

  function setStatus(text) {{
    if (statusEl) statusEl.textContent = text;
  }}

  function appendLine(who, text) {{
    if (!transcriptEl || !text) return;
    if (transcriptEl.dataset.empty !== "false") {{
      transcriptEl.innerHTML = "";
      transcriptEl.dataset.empty = "false";
    }}
    const row = document.createElement("div");
    row.style.margin = "8px 0";
    const label = document.createElement("strong");
    label.textContent = who + ": ";
    row.appendChild(label);
    row.appendChild(document.createTextNode(text));
    transcriptEl.appendChild(row);
    transcriptEl.scrollTop = transcriptEl.scrollHeight;
  }}

  function uiConnected() {{
    startBtn.style.display = "none";
    stopBtn.style.display = "inline-block";
    setStatus("🟢 Неола рядом. Говорите — она слушает.");
  }}

  function uiDisconnected() {{
    startBtn.style.display = "inline-block";
    stopBtn.style.display = "none";
    setStatus("⚪ Разговор закончен");
  }}

  // Глобальный объект позволяет соединению пережить обычные rerun Streamlit,
  // пока браузерная страница не перезагружена полностью.
  if (!window.__agencyWNeolaLive) {{
    window.__agencyWNeolaLive = {{
      pc: null,
      dc: null,
      stream: null,
      audioEl: null,
      connected: false,
      connecting: false,
      assistantTranscript: "",
      instructions: INSTRUCTIONS,
      context: CONTEXT,
    }};
  }}

  const live = window.__agencyWNeolaLive;
  live.instructions = INSTRUCTIONS;
  live.context = CONTEXT;

  function sendContextUpdate() {{
    if (!live.dc || live.dc.readyState !== "open") return;
    live.dc.send(JSON.stringify({{
      type: "session.update",
      session: {{
        type: "realtime",
        instructions: live.instructions
      }}
    }}));
  }}

  if (live.connected && live.pc && live.pc.connectionState !== "closed") {{
    uiConnected();
    sendContextUpdate();
  }}

  async function startLive() {{
    if (live.connected || live.connecting) {{
      uiConnected();
      return;
    }}

    live.connecting = true;
    setStatus("🟡 Подключаю Неолу…");

    try {{
      const pc = new RTCPeerConnection();
      const audioEl = document.createElement("audio");
      audioEl.autoplay = true;
      audioEl.setAttribute("playsinline", "");
      audioEl.style.display = "none";
      document.body.appendChild(audioEl);

      pc.ontrack = (event) => {{
        audioEl.srcObject = event.streams[0];
        audioEl.play().catch(() => {{}});
      }};

      const stream = await navigator.mediaDevices.getUserMedia({{
        audio: {{
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true
        }}
      }});

      stream.getAudioTracks().forEach(track => pc.addTrack(track, stream));

      const dc = pc.createDataChannel("oai-events");

      dc.addEventListener("open", () => {{
        live.connected = true;
        live.connecting = false;
        uiConnected();
        sendContextUpdate();

        // Просим Неолу начать первой короткой фразой.
        dc.send(JSON.stringify({{
          type: "response.create",
          response: {{
            instructions:
              "Начни разговор одной короткой фразой приветствия из системной инструкции. " +
              "После этого замолчи и слушай человека."
          }}
        }}));
      }});

      dc.addEventListener("message", (event) => {{
        let msg;
        try {{
          msg = JSON.parse(event.data);
        }} catch (_) {{
          return;
        }}

        if (msg.type === "input_audio_buffer.speech_started") {{
          setStatus("👂 Слушаю вас…");
        }}

        if (msg.type === "input_audio_buffer.speech_stopped") {{
          setStatus("🟡 Поняла. Отвечаю…");
        }}

        if (msg.type === "conversation.item.input_audio_transcription.completed") {{
          appendLine("Вы", msg.transcript || "");
        }}

        if (msg.type === "response.output_audio_transcript.delta") {{
          live.assistantTranscript += (msg.delta || "");
        }}

        if (msg.type === "response.output_audio_transcript.done") {{
          const finalText = (msg.transcript || live.assistantTranscript || "").trim();
          if (finalText) appendLine("Неола", finalText);
          live.assistantTranscript = "";
        }}

        if (msg.type === "response.done") {{
          setStatus("🟢 Говорите — Неола слушает.");
        }}

        if (msg.type === "error") {{
          const message = (msg.error && msg.error.message) || msg.message || "Неизвестная ошибка";
          setStatus("🔴 " + message);
        }}
      }});

      pc.addEventListener("connectionstatechange", () => {{
        if (pc.connectionState === "failed" || pc.connectionState === "closed") {{
          live.connected = false;
          live.connecting = false;
          uiDisconnected();
        }}
      }});

      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);

      const sdpResponse = await fetch(
        "https://api.openai.com/v1/realtime/calls",
        {{
          method: "POST",
          body: offer.sdp,
          headers: {{
            "Authorization": "Bearer " + TOKEN,
            "Content-Type": "application/sdp"
          }}
        }}
      );

      if (!sdpResponse.ok) {{
        const details = await sdpResponse.text();
        throw new Error("Не удалось открыть голосовую сессию: " + details);
      }}

      const answer = {{
        type: "answer",
        sdp: await sdpResponse.text()
      }};

      await pc.setRemoteDescription(answer);

      live.pc = pc;
      live.dc = dc;
      live.stream = stream;
      live.audioEl = audioEl;

    }} catch (error) {{
      live.connecting = false;
      live.connected = false;
      setStatus("🔴 " + (error.message || String(error)));
      if (error && error.name === "NotAllowedError") {{
        appendLine(
          "Подсказка",
          "Разрешите браузеру доступ к микрофону и нажмите «Начать разговор» ещё раз."
        );
      }}
    }}
  }}

  function stopLive() {{
    try {{
      if (live.dc) live.dc.close();
    }} catch (_) {{}}
    try {{
      if (live.pc) live.pc.close();
    }} catch (_) {{}}
    try {{
      if (live.stream) live.stream.getTracks().forEach(track => track.stop());
    }} catch (_) {{}}
    try {{
      if (live.audioEl) {{
        live.audioEl.pause();
        live.audioEl.srcObject = null;
        live.audioEl.remove();
      }}
    }} catch (_) {{}}

    live.pc = null;
    live.dc = null;
    live.stream = null;
    live.audioEl = null;
    live.connected = false;
    live.connecting = false;
    live.assistantTranscript = "";
    uiDisconnected();
  }}

  startBtn.onclick = startLive;
  stopBtn.onclick = stopLive;
}})();
</script>
"""


def render_neola_realtime_voice(
    telegram_id,
    owner_name,
    ui_context,
    onboarding_step=0,
):
    """
    Первый живой голосовой прототип Неолы.

    Важно:
    - основной OPENAI_API_KEY остаётся только на сервере Streamlit;
    - в браузер передаётся короткоживущий ephemeral client secret;
    - разговор идёт напрямую браузер <-> OpenAI Realtime через WebRTC.
    """
    try:
        token, instructions = create_realtime_client_secret(
            telegram_id=telegram_id,
            owner_name=owner_name,
            ui_context=ui_context,
            onboarding_step=onboarding_step,
        )
    except Exception as exc:
        st.error(f"Не удалось подготовить живой голос Неолы: {exc}")
        return False

    html_body = _render_realtime_html(
        ephemeral_key=token,
        instructions=instructions,
        ui_context=ui_context,
    )

    # Новые версии Streamlit умеют безопасно выполнять явно разрешённый JS
    # через st.html без iframe — это предпочтительно для микрофона/WebRTC.
    try:
        params = inspect.signature(st.html).parameters
    except Exception:
        params = {}

    if "unsafe_allow_javascript" not in params:
        st.warning(
            "Для живого голосового режима нужна версия Streamlit, "
            "в которой st.html поддерживает JavaScript. "
            "Сначала обновите Streamlit."
        )
        return False

    st.html(
        html_body,
        unsafe_allow_javascript=True,
        width="stretch",
    )
    return True
