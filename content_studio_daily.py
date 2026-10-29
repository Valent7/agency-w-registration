"""Daily content hub: one idea -> one coordinated multi-channel package."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st


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
    return (
        "Подготовь единый контент-пакет на одну тему. Одна центральная мысль должна "
        "узнаваться во всех материалах, но текст нельзя механически копировать между площадками.\\n\\n"
        f"Проект: {ctx['name']}\\n"
        f"Продукт/система: {ctx['product']}\\n"
        f"Аудитория: {ctx['audience']}\\n"
        f"Цель проекта: {ctx['goal']}\\n"
        f"Стиль общения: {ctx['tone']}\\n"
        f"Проверенные факты: {ctx['facts']}\\n"
        f"Визуальный стиль: {ctx['visual_style']}\\n"
        f"Нельзя обещать/использовать: {ctx['avoid']}\\n"
        f"Тема дня: {topic}\\n"
        f"Задача материала: {GOALS[goal]}\\n"
        f"Площадки: {', '.join(channel_names)}\\n"
        f"Форматы: {', '.join(format_names)}\\n\\n"
        "Правила качества:\\n"
        "1) одна ясная мысль, конкретное начало, минимум рекламных штампов;\\n"
        "2) не выдумывай клиентов, доходы, проценты, функции или факты;\\n"
        "3) для Zoom не дави: сначала польза/причина, затем мягкое приглашение;\\n"
        "4) Instagram — живо и визуально; Threads — короче и разговорнее; "
        "VK — чуть подробнее; LinkedIn — профессионально, но без канцелярита; "
        "WhatsApp Status — очень коротко;\\n"
        "5) Reels: сильный первый кадр/фраза, затем 3–5 смысловых шагов и один CTA;\\n"
        "6) карусель: 5 слайдов, на каждом одна короткая мысль;\\n"
        "7) Stories/Status: 3 коротких кадра, которые понятны без длинного пояснения.\\n\\n"
        "Верни ТОЛЬКО JSON без markdown по схеме:\\n"
        "{"
        "\\"core_idea\\":\\"...\\","
        "\\"cta\\":\\"...\\","
        "\\"reel\\":{\\"hook\\":\\"...\\",\\"script\\":\\"...\\",\\"caption\\":\\"...\\"},"
        "\\"story\\":{\\"frames\\":[\\"...\\",\\"...\\",\\"...\\"],\\"status\\":\\"...\\"},"
        "\\"post\\":{\\"instagram\\":\\"...\\",\\"whatsapp\\":\\"...\\",\\"threads\\":\\"...\\",\\"vk\\":\\"...\\",\\"linkedin\\":\\"...\\"},"
        "\\"carousel\\":{\\"title\\":\\"...\\",\\"slides\\":[\\"...\\",\\"...\\",\\"...\\",\\"...\\",\\"...\\"],\\"caption\\":\\"...\\"}"
        "}."
    )


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


def _pack_markdown(pack, channels, formats):
    parts = ["# Пакет дня", "", "## Главная мысль", pack["core_idea"]]
    if pack.get("cta"):
        parts += ["", "## Следующий шаг", pack["cta"]]
    if "reel" in formats and pack.get("reel"):
        reel = pack["reel"]
        parts += [
            "", "## Reels / короткое видео",
            "**Хук:** " + (reel.get("hook") or ""),
            "", reel.get("script") or "",
            "", "**Подпись:**", reel.get("caption") or "",
        ]
    if "story" in formats and pack.get("story"):
        story = pack["story"]
        parts += ["", "## Stories / Status"]
        for index, frame in enumerate(story.get("frames") or [], 1):
            parts.append(f"{index}. {frame}")
        if story.get("status"):
            parts += ["", "**WhatsApp Status:** " + story["status"]]
    if "post" in formats and pack.get("post"):
        parts += ["", "## Посты по площадкам"]
        for channel in channels:
            value = pack["post"].get(channel)
            if value:
                parts += ["", f"### {CHANNELS[channel]}", value]
    if "carousel" in formats and pack.get("carousel"):
        carousel = pack["carousel"]
        parts += ["", "## Карусель", "**Обложка:** " + (carousel.get("title") or "")]
        for index, slide in enumerate(carousel.get("slides") or [], 1):
            parts.append(f"{index}. {slide}")
        if carousel.get("caption"):
            parts += ["", "**Подпись:**", carousel["caption"]]
    return "\\n".join(parts).strip()


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

    st.markdown("### 1. О чём говорим сегодня?")
    topic = st.text_area(
        "Тема дня",
        placeholder="Например: почему человек теряет вечер на повторяющиеся задачи и что можно передать ИИ",
        height=130,
        max_chars=4000,
        key=f"daily_content_{owner_id}_{project['id']}_topic",
        label_visibility="collapsed",
    )
    goal = st.radio(
        "Цель",
        list(GOALS),
        format_func=GOALS.get,
        horizontal=True,
        key=f"daily_content_{owner_id}_{project['id']}_goal",
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

    st.markdown("### 3. Создать пакет")
    disabled = len(_clean(topic, 4000)) < 10 or not channels or not formats
    if st.button(
        "✨ Создать пакет дня",
        type="primary",
        width="stretch",
        disabled=disabled,
        key=f"daily_content_{owner_id}_{project['id']}_create",
    ):
        fingerprint = hashlib.sha256(json.dumps(
            [topic.strip(), goal, sorted(channels), sorted(formats), project.get("id")],
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
                with st.spinner("Собираю единый пакет контента…"):
                    system = (
                        "Ты — редактор Контент-завода Агентства W. "
                        "Создавай полезный, человеческий, точный контент без выдуманных фактов, "
                        "доходов, клиентов и неподтверждённых обещаний. Верни только валидный JSON."
                    )
                    answer = ask_ai_fn(
                        system,
                        _build_prompt(project, topic.strip(), goal, channels, formats),
                    )
                    package = _normalise_pack(_extract_json(answer), channels, formats)
                    pending["package"] = package
                    st.session_state[pending_key] = pending

            markdown = _pack_markdown(package, channels, formats)
            from content_studio_catalog import save_studio_result
            save_studio_result(
                store,
                owner_id,
                project["id"],
                "text",
                "Пакет дня — " + datetime.now(
                    ZoneInfo(project.get("timezone") or "Europe/Berlin")
                ).strftime("%d.%m.%Y"),
                text=markdown,
                request_id="daily-pack-" + pending["id"][:24],
            )
            jobs, skipped = _queue_supported(
                owner_id, project, store, service, package, channels, formats, pending["id"]
            )
            st.session_state[f"daily_content_{owner_id}_{project['id']}_latest"] = {
                "pack": package,
                "markdown": markdown,
                "jobs": len(jobs),
                "skipped": skipped,
            }
            st.session_state[f"daily_content_{owner_id}_notice"] = (
                f"Пакет дня готов. В производство поставлено материалов: {len(jobs)}. "
                "Тексты для ручной публикации сохранены в «Моих генерациях»."
            )
            st.rerun()
        except DailyContentError as error:
            st.error(str(error))
        except Exception:
            st.error(
                "Не удалось собрать пакет дня. Сохранённые материалы не затронуты; повторите создание."
            )

    latest = st.session_state.get(f"daily_content_{owner_id}_{project['id']}_latest")
    if isinstance(latest, dict) and latest.get("markdown"):
        st.divider()
        st.markdown("### Готовый пакет")
        st.markdown(latest["markdown"])
        st.download_button(
            "Скачать пакет текстом",
            latest["markdown"],
            file_name="agency-w-daily-content.md",
            mime="text/markdown",
            key=f"daily_content_{owner_id}_{project['id']}_download",
        )
        if latest.get("jobs"):
            st.success(f"В производство поставлено: {latest['jobs']}.")
        if latest.get("skipped"):
            with st.expander("Что осталось для ручной публикации"):
                for item in latest["skipped"]:
                    st.write("• " + str(item))
        st.caption(
            "WhatsApp Status и LinkedIn сейчас готовятся для ручной публикации. "
            "Instagram, VK и Threads ставятся в производство автоматически, "
            "если они подключены и формат включён в проект."
        )
