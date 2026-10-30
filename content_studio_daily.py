"""Daily content hub: one idea -> one coordinated multi-channel package."""
from __future__ import annotations

import hashlib
import json
import uuid
from html import escape
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import streamlit as st
import streamlit.components.v1 as components


CHANNELS = {
    "instagram": "Instagram",
    "whatsapp": "WhatsApp Status",
    "threads": "Threads",
    "vk": "ВКонтакте",
    "linkedin": "LinkedIn",
}

FORMATS = {
    "reel": "Reels / короткое видео",
    "story": "Stories / Status",
    "post": "Пост",
    "carousel": "Карусель",
}

GOALS = {
    "value": "Дать пользу и укрепить доверие",
    "engage": "Вовлечь в разговор",
    "zoom": "Мягко пригласить на презентацию в Zoom",
    "story": "Рассказать живую историю",
}

QUEUE_MATRIX = {
    "post": {"instagram", "threads", "vk"},
    "carousel": {"instagram", "vk"},
    "reel": {"instagram", "vk"},
}


class DailyContentError(RuntimeError):
    pass


def _clean(value, maximum):
    value = " ".join(str(value or "").split())
    return value[:maximum]


def _extract_json(answer):
    text = str(answer or "").strip()
    fence = chr(96) * 3
    if text.startswith(fence):
        text = text.strip(chr(96)).strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise DailyContentError("Не удалось разобрать подготовленный пакет. Повторите создание.")
    try:
        data = json.loads(text[start:end + 1])
    except (json.JSONDecodeError, TypeError):
        raise DailyContentError("Не удалось разобрать подготовленный пакет. Повторите создание.") from None
    if not isinstance(data, dict):
        raise DailyContentError("Контент-пакет имеет неверный формат.")
    return data


def _project_context(project):
    project = project if isinstance(project, dict) else {}
    return {
        "name": _clean(project.get("name"), 120),
        "product": _clean(project.get("product"), 3000),
        "audience": _clean(project.get("audience"), 2000),
        "goal": _clean(project.get("goal"), 1500),
        "tone": _clean(project.get("tone"), 700),
        "cta": _clean(project.get("cta"), 500),
        "facts": _clean(project.get("facts"), 3500),
        "visual_style": _clean(project.get("visual_style"), 1200),
        "avoid": _clean(project.get("avoid"), 1200),
    }


def _build_prompt(project, topic, goal, channels, formats):
    ctx = _project_context(project)
    channel_names = [CHANNELS[value] for value in channels]
    format_names = [FORMATS[value] for value in formats]
    return f"""
Подготовь единый контент-пакет на одну тему. Одна центральная мысль должна
узнаваться во всех материалах, но текст нельзя механически копировать между площадками.

Проект: {ctx['name']}
Продукт/система: {ctx['product']}
Аудитория: {ctx['audience']}
Цель проекта: {ctx['goal']}
Стиль общения: {ctx['tone']}
Проверенные факты: {ctx['facts']}
Визуальный стиль: {ctx['visual_style']}
Нельзя обещать/использовать: {ctx['avoid']}
Тема дня: {topic}
Задача материала: {GOALS[goal]}
Площадки: {', '.join(channel_names)}
Форматы: {', '.join(format_names)}

Правила качества:
1) одна ясная мысль, конкретное начало, минимум рекламных штампов;
2) не выдумывай клиентов, доходы, проценты, функции или факты;
3) для Zoom не дави: сначала польза/причина, затем мягкое приглашение;
4) Instagram — живо и визуально; Threads — короче и разговорнее;
   VK — чуть подробнее; LinkedIn — профессионально, но без канцелярита;
   WhatsApp Status — один короткий готовый статус без служебных слов и без фразы
   «ссылка в профиле»; если нужен отклик, предложи написать владельцу слово «Презентация»;
5) Reels: сильный первый кадр/фраза, затем 3–5 смысловых шагов и один CTA;
6) карусель: 5 слайдов, на каждом одна короткая мысль;
7) Stories/Status: 3 коротких кадра, которые понятны без длинного пояснения.

Верни ТОЛЬКО JSON без markdown по схеме:
{{"core_idea":"...","cta":"...",
 "reel":{{"hook":"...","script":"...","caption":"..."}},
 "story":{{"frames":["...","...","..."],"status":"..."}},
 "post":{{"instagram":"...","whatsapp":"...","threads":"...","vk":"...","linkedin":"..."}},
 "carousel":{{"title":"...","slides":["...","...","...","...","..."],"caption":"..."}}}}.
""".strip()

def _normalise_pack(data, channels, formats):
    result = {
        "core_idea": _clean(data.get("core_idea"), 1200),
        "cta": _clean(data.get("cta"), 600),
        "reel": {},
        "story": {},
        "post": {},
        "carousel": {},
    }
    if "reel" in formats:
        reel = data.get("reel") if isinstance(data.get("reel"), dict) else {}
        result["reel"] = {
            "hook": _clean(reel.get("hook"), 300),
            "script": _clean(reel.get("script"), 5000),
            "caption": _clean(reel.get("caption"), 2200),
        }
    if "story" in formats:
        story = data.get("story") if isinstance(data.get("story"), dict) else {}
        frames = story.get("frames") if isinstance(story.get("frames"), list) else []
        result["story"] = {
            "frames": [_clean(item, 350) for item in frames[:3] if _clean(item, 350)],
            "status": _clean(story.get("status"), 700),
        }
    if "post" in formats:
        posts = data.get("post") if isinstance(data.get("post"), dict) else {}
        result["post"] = {
            channel: _clean(posts.get(channel), 6000)
            for channel in channels
            if channel in CHANNELS
        }
    if "carousel" in formats:
        carousel = data.get("carousel") if isinstance(data.get("carousel"), dict) else {}
        slides = carousel.get("slides") if isinstance(carousel.get("slides"), list) else []
        result["carousel"] = {
            "title": _clean(carousel.get("title"), 300),
            "slides": [_clean(item, 500) for item in slides[:5] if _clean(item, 500)],
            "caption": _clean(carousel.get("caption"), 2200),
        }
    if not result["core_idea"]:
        raise DailyContentError("ИИ не сформулировал главную мысль пакета. Повторите создание.")
    return result



WEEKDAY_RU = (
    "Понедельник", "Вторник", "Среда", "Четверг",
    "Пятница", "Суббота", "Воскресенье",
)


def _local_today(project):
    timezone = project.get("timezone") or "Europe/Berlin"
    return datetime.now(ZoneInfo(timezone)).date()


def _week_dates(project):
    today = _local_today(project)
    return [today + timedelta(days=offset) for offset in range(7)]


def _build_weekly_plan_prompt(project, owner_note, dates):
    ctx = _project_context(project)
    period = ", ".join(day.isoformat() for day in dates)
    return f"""
Составь редакционный план Агентства W на ближайшие 7 дней совместно с владельцем.

Проект: {ctx['name']}
Продукт/система: {ctx['product']}
Аудитория: {ctx['audience']}
Цель проекта: {ctx['goal']}
Проверенные факты: {ctx['facts']}
Нельзя обещать/использовать: {ctx['avoid']}
Пожелание владельца на эту неделю: {owner_note or 'специальных пожеланий нет'}
Даты плана: {period}

Твоя роль — Стагирит, координатор. Ты ПРЕДЛАГАЕШЬ темы, но не утверждаешь их
за владельца: окончательное решение принимает владелец. Темы должны чередовать пользу, разговор, мягкое приглашение,
живую историю и объяснение возможностей. Не повторяй одну мысль семь раз.
Не выдумывай доходы, клиентов, функции и гарантии.

Верни только JSON:
{{"focus":"одна фраза — смысл недели","days":[
{{"topic":"тема дня 1","goal":"value"}},
{{"topic":"тема дня 2","goal":"engage"}},
{{"topic":"тема дня 3","goal":"zoom"}},
{{"topic":"тема дня 4","goal":"story"}},
{{"topic":"тема дня 5","goal":"value"}},
{{"topic":"тема дня 6","goal":"engage"}},
{{"topic":"тема дня 7","goal":"value"}}
]}}
Допустимые goal: value, engage, zoom, story.
""".strip()


def _normalise_weekly_plan(data, dates):
    if not isinstance(data, dict):
        raise DailyContentError("Стагирит не смог подготовить план недели.")
    raw_days = data.get("days")
    if not isinstance(raw_days, list) or len(raw_days) < 7:
        raise DailyContentError("Стагирит подготовил неполный план. Повторите ещё раз.")
    days = []
    for index, day in enumerate(dates[:7]):
        raw = raw_days[index] if isinstance(raw_days[index], dict) else {}
        topic = _clean(raw.get("topic"), 500)
        if not topic:
            raise DailyContentError("В плане недели есть пустая тема.")
        goal = raw.get("goal")
        if goal not in GOALS:
            goal = "value"
        days.append({
            "date": day.isoformat(),
            "day": WEEKDAY_RU[day.weekday()],
            "topic": topic,
            "goal": goal,
        })
    return {
        "version": 1,
        "focus": _clean(data.get("focus"), 900),
        "days": days,
    }


def _weekly_plan_text(plan):
    parts = ["ПЛАН КОНТЕНТА НА 7 ДНЕЙ"]
    if plan.get("focus"):
        parts += ["", "Смысл недели:", plan["focus"]]
    for item in plan.get("days") or []:
        parts += [
            "",
            f"{item.get('day')} · {item.get('date')}",
            item.get("topic") or "",
            "Цель: " + GOALS.get(item.get("goal"), GOALS["value"]),
        ]
    return "\n".join(parts).strip()


def _load_weekly_plan(store, owner_id, project):
    try:
        jobs = store.list_jobs(owner_id, project["id"], limit=200)
    except Exception:
        return None
    candidates = []
    for job in jobs:
        content = job.get("content") if isinstance(job, dict) else None
        plan = content.get("weekly_plan") if isinstance(content, dict) else None
        if isinstance(plan, dict) and isinstance(plan.get("days"), list):
            candidates.append(job)
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: str(item.get("updated_at") or item.get("scheduled_at") or ""),
        reverse=True,
    )
    return (candidates[0].get("content") or {}).get("weekly_plan")


def _save_weekly_plan(store, owner_id, project, plan):
    from content_studio_catalog import save_studio_result

    title = (
        "План недели — "
        + str((plan.get("days") or [{}])[0].get("date") or "")
        + " — "
        + str((plan.get("days") or [{}])[-1].get("date") or "")
    )
    job = save_studio_result(
        store,
        owner_id,
        project["id"],
        "text",
        title,
        text=_weekly_plan_text(plan),
        request_id="weekly-plan-" + uuid.uuid4().hex[:24],
    )
    content = dict(job.get("content") or {})
    content["weekly_plan"] = plan
    content["title"] = title
    return store.update_job(
        owner_id,
        job["id"],
        {"content": content},
        expected_updated_at=job.get("updated_at"),
    )


def _today_plan_item(plan, project):
    if not isinstance(plan, dict):
        return None
    today = _local_today(project).isoformat()
    for item in plan.get("days") or []:
        if isinstance(item, dict) and item.get("date") == today:
            return item
    return None

def _pack_text(pack, channels, formats):
    parts = ["ПАКЕТ ДНЯ", "", "Главная мысль", pack["core_idea"]]
    if pack.get("cta"):
        parts += ["", "Следующий шаг", pack["cta"]]
    if "reel" in formats and pack.get("reel"):
        reel = pack["reel"]
        parts += [
            "", "REELS / КОРОТКОЕ ВИДЕО",
            "Хук: " + (reel.get("hook") or ""),
            "", reel.get("script") or "",
            "", "Подпись:", reel.get("caption") or "",
        ]
    if "story" in formats and pack.get("story"):
        story = pack["story"]
        parts += ["", "STORIES / STATUS"]
        for index, frame in enumerate(story.get("frames") or [], 1):
            parts.append(f"{index}. {frame}")
        if story.get("status"):
            parts += ["", "WhatsApp Status: " + story["status"]]
    if "post" in formats and pack.get("post"):
        parts += ["", "ПОСТЫ ПО ПЛОЩАДКАМ"]
        for channel in channels:
            value = pack["post"].get(channel)
            if value:
                parts += ["", CHANNELS[channel], value]
    if "carousel" in formats and pack.get("carousel"):
        carousel = pack["carousel"]
        parts += ["", "КАРУСЕЛЬ", "Обложка: " + (carousel.get("title") or "")]
        for index, slide in enumerate(carousel.get("slides") or [], 1):
            parts.append(f"{index}. {slide}")
        if carousel.get("caption"):
            parts += ["", "Подпись:", carousel["caption"]]
    return "\n".join(parts).strip()


CHANNEL_FILES = {
    "instagram": "instagram.txt",
    "whatsapp": "whatsapp-status.txt",
    "threads": "threads.txt",
    "vk": "vk.txt",
    "linkedin": "linkedin.txt",
}


def _channel_package_text(pack, channel, formats):
    """Prepare one clean copy/paste package for a single social channel."""
    if channel not in CHANNELS:
        return ""

    posts = pack.get("post") if isinstance(pack.get("post"), dict) else {}
    post = _clean(posts.get(channel), 6000)

    # WhatsApp Status must be literally ready to paste. Do not mix an alternate
    # "post" version, labels or service text into the downloaded/copyable body.
    if channel == "whatsapp":
        if "story" in formats and isinstance(pack.get("story"), dict):
            status = _clean(pack["story"].get("status"), 700)
            if status:
                return status
        return post

    parts = [CHANNELS[channel], "Готово к публикации"]
    if post:
        parts += ["", "ТЕКСТ", post]

    if channel == "instagram":
        if "reel" in formats and isinstance(pack.get("reel"), dict):
            reel = pack["reel"]
            if any(reel.get(name) for name in ("hook", "script", "caption")):
                parts += [
                    "", "REELS",
                    "Хук: " + str(reel.get("hook") or ""),
                    "", "Сценарий:", str(reel.get("script") or ""),
                    "", "Подпись:", str(reel.get("caption") or ""),
                ]
        if "story" in formats and isinstance(pack.get("story"), dict):
            story = pack["story"]
            frames = story.get("frames") if isinstance(story.get("frames"), list) else []
            if frames:
                parts += ["", "STORIES"]
                parts.extend(f"{index}. {frame}" for index, frame in enumerate(frames[:3], 1))
        if "carousel" in formats and isinstance(pack.get("carousel"), dict):
            carousel = pack["carousel"]
            slides = carousel.get("slides") if isinstance(carousel.get("slides"), list) else []
            if carousel.get("title") or slides or carousel.get("caption"):
                parts += ["", "КАРУСЕЛЬ", "Обложка: " + str(carousel.get("title") or "")]
                parts.extend(f"{index}. {slide}" for index, slide in enumerate(slides[:5], 1))
                if carousel.get("caption"):
                    parts += ["", "Подпись:", str(carousel["caption"])]

    elif channel == "vk":
        if "reel" in formats and isinstance(pack.get("reel"), dict):
            reel = pack["reel"]
            if reel.get("script") or reel.get("caption"):
                parts += [
                    "", "КОРОТКОЕ ВИДЕО",
                    str(reel.get("script") or ""),
                    "", "Подпись:", str(reel.get("caption") or ""),
                ]
        if "carousel" in formats and isinstance(pack.get("carousel"), dict):
            carousel = pack["carousel"]
            slides = carousel.get("slides") if isinstance(carousel.get("slides"), list) else []
            if carousel.get("title") or slides or carousel.get("caption"):
                parts += ["", "КАРУСЕЛЬ", "Обложка: " + str(carousel.get("title") or "")]
                parts.extend(f"{index}. {slide}" for index, slide in enumerate(slides[:5], 1))
                if carousel.get("caption"):
                    parts += ["", "Подпись:", str(carousel["caption"])]

    text = "\n".join(str(item) for item in parts if item is not None).strip()
    return text if len(text) > len(CHANNELS[channel]) + 25 else ""


def _copy_button(text, key):
    """Small browser-side copy control; no content leaves the user's browser."""
    element_id = "copy_" + hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:12]
    safe = escape(str(text or ""))
    components.html(
        f"""
        <textarea id="{element_id}" style="position:absolute;left:-10000px;top:-10000px;">{safe}</textarea>
        <button id="{element_id}_button"
            style="width:100%;height:38px;border:1px solid #d0d0d0;border-radius:8px;background:white;cursor:pointer;font-size:14px;"
            onclick="(function(){{
                var box=document.getElementById('{element_id}');
                box.focus(); box.select();
                var ok=false;
                try {{ ok=document.execCommand('copy'); }} catch(e) {{}}
                var button=document.getElementById('{element_id}_button');
                button.textContent=ok ? '✓ Скопировано' : 'Выделите текст и скопируйте';
                setTimeout(function(){{ button.textContent='📋 Скопировать текст'; }},1800);
            }})()">
            📋 Скопировать текст
        </button>
        """,
        height=46,
        scrolling=False,
    )


def _render_publish_cards(pack, channels, formats, owner_id, project_id):
    st.markdown("### Готово к публикации")
    st.caption(
        "Контент-завод всё подготовил. Ничего не публикуется автоматически: "
        "владелец сам копирует или скачивает материал и размещает его на своей площадке."
    )
    rendered = 0
    for channel in channels:
        text = _channel_package_text(pack, channel, formats)
        if not text:
            continue
        rendered += 1
        with st.container(border=True):
            st.markdown(f"**{CHANNELS[channel]} — ✅ Готово к публикации**")
            with st.expander("Посмотреть подготовленный материал", expanded=False):
                st.text(text)
            left, right = st.columns(2)
            with left:
                _copy_button(
                    text,
                    f"{owner_id}:{project_id}:{channel}:{hashlib.sha256(text.encode('utf-8')).hexdigest()}",
                )
            with right:
                st.download_button(
                    "⬇️ Скачать",
                    text,
                    file_name=CHANNEL_FILES[channel],
                    mime="text/plain",
                    width="stretch",
                    key=f"daily_publish_{owner_id}_{project_id}_{channel}_download",
                )
    if not rendered:
        st.info("Для выбранных площадок пока нет готового текста.")


def _render_pack(pack, channels, formats):
    with st.container(border=True):
        st.markdown("**Главная мысль**")
        st.write(pack.get("core_idea") or "")
        if pack.get("cta"):
            st.markdown("**Следующий шаг**")
            st.write(pack["cta"])

    if "reel" in formats and pack.get("reel"):
        reel = pack["reel"]
        with st.expander("🎬 Reels / короткое видео", expanded=True):
            st.markdown("**Хук**")
            st.write(reel.get("hook") or "")
            st.markdown("**Сценарий**")
            st.write(reel.get("script") or "")
            st.markdown("**Подпись**")
            st.write(reel.get("caption") or "")

    if "story" in formats and pack.get("story"):
        story = pack["story"]
        with st.expander("📱 Stories / Status", expanded=True):
            for index, frame in enumerate(story.get("frames") or [], 1):
                st.write(f"{index}. {frame}")
            if story.get("status"):
                st.markdown("**WhatsApp Status**")
                st.write(story["status"])

    if "post" in formats and pack.get("post"):
        st.markdown("**Посты по площадкам**")
        for channel in channels:
            value = pack["post"].get(channel)
            if value:
                with st.expander(CHANNELS[channel], expanded=False):
                    st.write(value)

    if "carousel" in formats and pack.get("carousel"):
        carousel = pack["carousel"]
        with st.expander("🖼️ Карусель", expanded=True):
            st.markdown("**Обложка**")
            st.write(carousel.get("title") or "")
            for index, slide in enumerate(carousel.get("slides") or [], 1):
                st.write(f"{index}. {slide}")
            if carousel.get("caption"):
                st.markdown("**Подпись**")
                st.write(carousel["caption"])

def _queue_brief(pack, content_format):
    if content_format == "reel":
        reel = pack.get("reel") or {}
        return (
            "Создай короткий ролик по уже утверждённой идее. "
            f"Главная мысль: {pack.get('core_idea')}. "
            f"Хук: {reel.get('hook')}. "
            f"Сценарий: {reel.get('script')}. "
            f"CTA: {pack.get('cta')}. "
            "Сохрани смысл, но адаптируй подпись под площадку."
        )
    if content_format == "carousel":
        carousel = pack.get("carousel") or {}
        return (
            "Создай карусель из пяти слайдов по утверждённой структуре. "
            f"Главная мысль: {pack.get('core_idea')}. "
            f"Обложка: {carousel.get('title')}. "
            f"Слайды: {' | '.join(carousel.get('slides') or [])}. "
            f"CTA: {pack.get('cta')}."
        )
    posts = pack.get("post") or {}
    source = " | ".join(value for value in posts.values() if value)
    return (
        "Создай пост по утверждённому контент-пакету. "
        f"Главная мысль: {pack.get('core_idea')}. "
        f"Варианты текста: {source}. "
        f"CTA: {pack.get('cta')}. "
        "Адаптируй длину и подачу под конкретную площадку."
    )


def _queue_supported(owner_id, project, store, service, pack, channels, formats, package_id):
    try:
        connected = {
            item.get("platform")
            for item in store.list_connections(owner_id, project["id"])
            if isinstance(item, dict)
        }
    except Exception:
        connected = set()
    project_platforms = set(project.get("platforms") or [])
    project_formats = set(project.get("formats") or [])
    requested_supported = set(channels) & {"instagram", "threads", "vk"}
    jobs = []
    skipped = []
    for content_format in ("reel", "carousel", "post"):
        if content_format not in formats:
            continue
        eligible = [
            platform
            for platform in requested_supported
            if platform in QUEUE_MATRIX[content_format]
            and platform in project_platforms
            and platform in connected
        ]
        if content_format not in project_formats:
            skipped.append(f"{FORMATS[content_format]} не включён в проект")
            continue
        if not eligible:
            skipped.append(f"{FORMATS[content_format]}: нет выбранной подключённой площадки")
            continue
        request_id = f"daily-{package_id[:22]}-{content_format}"
        jobs.extend(service.create_material(
            owner_id,
            project["id"],
            _queue_brief(pack, content_format)[:6000],
            content_format,
            eligible,
            request_id=request_id,
        ))
    return jobs, skipped


def render_daily_content_hub(owner_id, project, store, service, ask_ai_fn):
    st.caption("Одна идея → пакет для всех площадок → готовые материалы и производство.")

    if not project or not store or not service:
        st.info("Сначала создайте проект — его аудитория, стиль и факты будут использоваться автоматически.")
        return

    notice = st.session_state.pop(f"daily_content_{owner_id}_notice", "")
    if notice:
        st.success(notice)

    topic_key = f"daily_content_{owner_id}_{project['id']}_topic"
    goal_key = f"daily_content_{owner_id}_{project['id']}_goal"
    draft_key = f"daily_content_{owner_id}_{project['id']}_weekly_draft"
    plan = _load_weekly_plan(store, owner_id, project)
    today_item = _today_plan_item(plan, project)
    if today_item and not str(st.session_state.get(topic_key) or "").strip():
        st.session_state[topic_key] = today_item.get("topic") or ""
    if today_item and st.session_state.get(goal_key) not in GOALS:
        st.session_state[goal_key] = today_item.get("goal") if today_item.get("goal") in GOALS else "value"

    st.markdown("### План недели: вы + Стагирит")
    st.caption(
        "Стагирит предлагает темы на 7 дней. Владелец меняет их при необходимости "
        "и только потом утверждает план."
    )
    if plan:
        st.success("Недельный план утверждён. Сегодняшняя тема подставляется из него автоматически.")
        with st.expander("Посмотреть утверждённый план"):
            if plan.get("focus"):
                st.write("Смысл недели: " + str(plan["focus"]))
            for item in plan.get("days") or []:
                if isinstance(item, dict):
                    st.write(
                        f"• {item.get('day')} · {item.get('date')} — "
                        f"{item.get('topic')} ({GOALS.get(item.get('goal'), GOALS['value'])})"
                    )

    owner_note = st.text_area(
        "Что важно учесть на этой неделе?",
        placeholder="Например: в среду презентация в Zoom; сделать акцент на возвращении времени и живых примерах.",
        height=85,
        max_chars=1500,
        key=f"daily_content_{owner_id}_{project['id']}_weekly_note",
    )
    if st.button(
        "🧭 Стагирит предложит план на 7 дней",
        width="stretch",
        key=f"daily_content_{owner_id}_{project['id']}_weekly_propose",
    ):
        try:
            with st.spinner("Стагирит готовит предложение на неделю…"):
                dates = _week_dates(project)
                answer = ask_ai_fn(
                    "Ты — Стагирит, координатор Агентства W. Ты предлагаешь редакционный план, "
                    "но окончательное решение всегда принимает владелец.",
                    _build_weekly_plan_prompt(project, owner_note.strip(), dates),
                )
                st.session_state[draft_key] = _normalise_weekly_plan(_extract_json(answer), dates)
            st.rerun()
        except DailyContentError as error:
            st.error(str(error))
        except Exception:
            st.error("Не удалось получить предложение Стагирита. Попробуйте ещё раз.")

    draft = st.session_state.get(draft_key)
    if isinstance(draft, dict) and isinstance(draft.get("days"), list):
        st.info("Это предложение Стагирита. Измените любую тему или цель и только потом утвердите.")
        if draft.get("focus"):
            st.write("Предложенный смысл недели: " + str(draft["focus"]))
        for index, item in enumerate(draft["days"][:7]):
            topic_edit_key = f"{draft_key}_topic_{index}"
            goal_edit_key = f"{draft_key}_goal_{index}"
            if topic_edit_key not in st.session_state:
                st.session_state[topic_edit_key] = item.get("topic") or ""
            if st.session_state.get(goal_edit_key) not in GOALS:
                st.session_state[goal_edit_key] = item.get("goal") if item.get("goal") in GOALS else "value"
            with st.container(border=True):
                st.markdown(f"**{item.get('day')} · {item.get('date')}**")
                st.text_input(
                    "Тема",
                    key=topic_edit_key,
                    max_chars=500,
                    label_visibility="collapsed",
                )
                st.selectbox(
                    "Цель",
                    list(GOALS),
                    format_func=GOALS.get,
                    key=goal_edit_key,
                )
        if st.button(
            "✅ Утвердить план недели",
            type="primary",
            width="stretch",
            key=f"{draft_key}_approve",
        ):
            try:
                approved = {
                    "version": 1,
                    "focus": _clean(draft.get("focus"), 900),
                    "days": [],
                }
                for index, item in enumerate(draft["days"][:7]):
                    topic_value = _clean(st.session_state.get(f"{draft_key}_topic_{index}"), 500)
                    if not topic_value:
                        raise DailyContentError("В утверждённом плане не может быть пустой темы.")
                    goal_value = st.session_state.get(f"{draft_key}_goal_{index}")
                    if goal_value not in GOALS:
                        goal_value = "value"
                    approved["days"].append({
                        "date": item.get("date"),
                        "day": item.get("day"),
                        "topic": topic_value,
                        "goal": goal_value,
                    })
                _save_weekly_plan(store, owner_id, project, approved)
                approved_today = _today_plan_item(approved, project)
                if approved_today:
                    st.session_state[topic_key] = approved_today["topic"]
                    st.session_state[goal_key] = approved_today["goal"]
                st.session_state.pop(draft_key, None)
                st.session_state[f"daily_content_{owner_id}_notice"] = (
                    "План недели утверждён владельцем. Сегодняшняя тема взята из него."
                )
                st.rerun()
            except DailyContentError as error:
                st.error(str(error))
            except Exception:
                st.error("Не удалось сохранить недельный план. Попробуйте ещё раз.")

    st.markdown("### 1. О чём говорим сегодня?")
    if today_item:
        st.caption("Тема взята из утверждённого недельного плана. Перед созданием пакета её можно изменить вручную.")
    else:
        st.caption("Можно написать тему вручную или сначала составить недельный план вместе со Стагиритом.")
    topic = st.text_area(
        "Тема дня",
        placeholder="Например: почему человек теряет вечер на повторяющиеся задачи и что можно передать ИИ",
        height=130,
        max_chars=4000,
        key=topic_key,
        label_visibility="collapsed",
    )
    goal = st.radio(
        "Цель",
        list(GOALS),
        format_func=GOALS.get,
        horizontal=True,
        key=goal_key,
    )

    st.markdown("### 2. Что подготовить?")
    left, right = st.columns(2, gap="large")
    with left:
        channels = st.multiselect(
            "Площадки",
            list(CHANNELS),
            default=list(CHANNELS),
            format_func=CHANNELS.get,
            key=f"daily_content_{owner_id}_{project['id']}_channels",
        )
    with right:
        formats = st.multiselect(
            "Форматы",
            list(FORMATS),
            default=["reel", "story", "post", "carousel"],
            format_func=FORMATS.get,
            key=f"daily_content_{owner_id}_{project['id']}_formats",
        )

    intent_key = f"daily_content_{owner_id}_{project['id']}_intent"
    st.markdown("### 3. Проверить смысл")
    disabled = len(_clean(topic, 4000)) < 10 or not channels or not formats
    if st.button(
        "1. Понять мой замысел",
        type="primary",
        width="stretch",
        disabled=disabled,
        key=f"daily_content_{owner_id}_{project['id']}_understand",
    ):
        from content_studio_intent_ui import begin_intent
        begin_intent(
            intent_key,
            typed_text=topic,
            audio=None,
            kind="text",
            project=project,
            extra={
                "daily_package": True,
                "goal": goal,
                "channels": sorted(channels),
                "formats": sorted(formats),
            },
        )

    from content_studio_intent_ui import render_intent_review
    production = render_intent_review(
        intent_key,
        kind="text",
        project=project,
        extra={
            "daily_package": True,
            "goal": goal,
            "channels": sorted(channels),
            "formats": sorted(formats),
        },
    )

    if isinstance(production, dict):
        st.markdown("### 4. Создать пакет")
        if st.button(
            "✨ Создать пакет дня по утверждённому замыслу",
            type="primary",
            width="stretch",
            key=f"daily_content_{owner_id}_{project['id']}_create",
        ):
            production_prompt = str(production.get("production_prompt") or "").strip()
            fingerprint = hashlib.sha256(json.dumps(
                [production_prompt, goal, sorted(channels), sorted(formats), project.get("id")],
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")).hexdigest()
            pending_key = f"daily_content_{owner_id}_{project['id']}_pending"
            pending = st.session_state.get(pending_key)
            if not isinstance(pending, dict) or pending.get("fingerprint") != fingerprint:
                pending = {"fingerprint": fingerprint, "id": uuid.uuid4().hex}
                st.session_state[pending_key] = pending

            try:
                package = pending.get("package")
                if not isinstance(package, dict):
                    with st.spinner("Собираю единый пакет по утверждённому замыслу…"):
                        system = (
                            "Ты — редактор Контент-завода Агентства W. Утверждённый производственный бриф "
                            "является главным источником смысла. Создавай полезный, человеческий, точный контент "
                            "без выдуманных фактов, доходов, клиентов и неподтверждённых обещаний. "
                            "Верни только валидный JSON."
                        )
                        answer = ask_ai_fn(
                            system,
                            _build_prompt(project, production_prompt, goal, channels, formats),
                        )
                        package = _normalise_pack(_extract_json(answer), channels, formats)
                        pending["package"] = package
                        st.session_state[pending_key] = pending

                package_text = _pack_text(package, channels, formats)
                from content_studio_catalog import save_studio_result
                save_studio_result(
                    store,
                    owner_id,
                    project["id"],
                    "text",
                    "Пакет дня — " + datetime.now(
                        ZoneInfo(project.get("timezone") or "Europe/Berlin")
                    ).strftime("%d.%m.%Y"),
                    text=package_text,
                    request_id="daily-pack-" + pending["id"][:24],
                )
                st.session_state[f"daily_content_{owner_id}_{project['id']}_latest"] = {
                    "pack": package,
                    "text": package_text,
                    "channels": list(channels),
                    "formats": list(formats),
                    "approved_intent": production.get("approved_intent"),
                    "production_plan": production.get("production_plan"),
                    "quality_criteria": production.get("quality_criteria"),
                }
                st.session_state[f"daily_content_{owner_id}_notice"] = (
                    "Пакет дня готов по утверждённому замыслу и сохранён в «Моих генерациях». "
                    "Ничего автоматически не опубликовано."
                )
                st.rerun()
            except DailyContentError as error:
                st.error(str(error))
            except Exception:
                st.error(
                    "Не удалось собрать пакет дня. Сохранённые материалы не затронуты; повторите создание."
                )

    latest = st.session_state.get(f"daily_content_{owner_id}_{project['id']}_latest")
    if isinstance(latest, dict) and latest.get("text") and isinstance(latest.get("pack"), dict):
        latest_channels = [
            value for value in (latest.get("channels") or channels)
            if value in CHANNELS
        ]
        latest_formats = [
            value for value in (latest.get("formats") or formats)
            if value in FORMATS
        ]
        st.divider()
        st.markdown("### Готовый пакет")
        _render_pack(latest["pack"], latest_channels, latest_formats)
        st.download_button(
            "⬇️ Скачать весь пакет",
            latest["text"],
            file_name="agency-w-daily-content.txt",
            mime="text/plain",
            width="stretch",
            key=f"daily_content_{owner_id}_{project['id']}_download",
        )
        _render_publish_cards(
            latest["pack"],
            latest_channels,
            latest_formats,
            owner_id,
            project["id"],
        )
