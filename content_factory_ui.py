import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests
import streamlit as st


PROFESSIONAL_PORTRAITS = {
    "Стагирит": "content_ref_stagirite.webp",
    "Неония": "content_ref_neonia.webp",
    "Неона": "content_ref_neona.webp",
    "Тео": "content_ref_theo.webp",
    "Неола": "content_ref_neola.webp",
}


def _guess_professionals(item):
    """Определяет героев по смыслу старых и новых пакетов контента."""
    source = " ".join(
        str(value or "")
        for value in (
            item.get("title"),
            item.get("goal"),
            item.get("hook"),
            item.get("script"),
            item.get("post_text"),
            " ".join(item.get("slides") or []),
            item.get("caption"),
            item.get("visual_brief"),
        )
    ).lower()
    if any(marker in source for marker in ("пять ии", "пять профессионал", "вся команда")):
        return list(PROFESSIONAL_PORTRAITS)
    return [name for name in PROFESSIONAL_PORTRAITS if name.lower() in source]


def _portrait_references(names):
    """Загружает только выбранные эталонные портреты в момент генерации."""
    assets_dir = Path(__file__).resolve().parent / "assets"
    references = []
    for name in names or []:
        filename = PROFESSIONAL_PORTRAITS.get(name)
        if not filename:
            continue
        path = assets_dir / filename
        if path.exists():
            references.append(
                {
                    "name": name,
                    "kind": "portrait",
                    "image_bytes": path.read_bytes(),
                    "mime_type": "image/webp",
                }
            )
    logo_path = assets_dir / "agency_w_icon.png"
    if references and logo_path.exists():
        references.append(
            {
                "name": "Официальный знак Агентства W",
                "kind": "logo",
                "image_bytes": logo_path.read_bytes(),
                "mime_type": "image/png",
            }
        )
    return references


FACTORY_SYSTEM_PROMPT = """
Ты — редакция и продюсерский центр «Контент-завода W».

Команда Агентства W состоит ровно из пяти публичных ИИ-профессионалов:
1. Стагирит — координатор.
2. Неония — аналитик целевой аудитории и поиск.
3. Неона — диалоги и встречи.
4. Тео — эксперт-консультант и герой Агентства.
5. Неола — наставник нового партнёра.

Разведчик W не входит в публичную пятёрку. Он работает за кулисами:
исследует рынок и конкурентов, проверяет факты и предлагает лучшие идеи.

Создавай оригинальный контент, а не копии конкурентов. Каждый материал должен:
- приносить человеку практическую пользу;
- звучать естественно и понятно;
- поддерживать главную идею Агентства W: «Мы возвращаем человеку время»;
- вызывать интерес без давления, ложных обещаний и навязчивых продаж;
- завершаться одним ясным и уместным действием читателя;
- быть пригодным для профессионального Instagram-аккаунта.

Верни ТОЛЬКО корректный JSON без Markdown и без пояснений.
""".strip()


def _clean_text(value, limit=12000):
    return str(value or "").strip()[:limit]


def _json_from_answer(answer):
    raw = _clean_text(answer, 100000)
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw)
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("ИИ не вернул готовый недельный пакет.")
    data = json.loads(raw[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("Недельный пакет имеет неверный формат.")
    return data


def _normalise_item(item, index):
    item = item if isinstance(item, dict) else {}
    fmt = _clean_text(item.get("format") or "post", 30).lower()
    if fmt not in {"reel", "post", "carousel"}:
        fmt = "post"
    item_id = _clean_text(item.get("id"), 80) or f"{fmt}_{index + 1}"
    scenes = item.get("scenes") if isinstance(item.get("scenes"), list) else []
    slides = item.get("slides") if isinstance(item.get("slides"), list) else []
    professionals = item.get("professionals") if isinstance(item.get("professionals"), list) else []
    return {
        "id": item_id,
        "format": fmt,
        "day": _clean_text(item.get("day"), 40),
        "title": _clean_text(item.get("title"), 240),
        "goal": _clean_text(item.get("goal"), 500),
        "hook": _clean_text(item.get("hook"), 500),
        "script": _clean_text(item.get("script"), 7000),
        "scenes": [_clean_text(value, 900) for value in scenes[:12]],
        "post_text": _clean_text(item.get("post_text"), 7000),
        "slides": [_clean_text(value, 1200) for value in slides[:12]],
        "caption": _clean_text(item.get("caption"), 4000),
        "cta": _clean_text(item.get("cta"), 800),
        "visual_brief": _clean_text(item.get("visual_brief"), 2500),
        "professionals": [
            name for name in PROFESSIONAL_PORTRAITS if name in professionals
        ],
        "status": "draft",
    }


def _normalise_package(data, owner_id, settings):
    raw_items = data.get("items") if isinstance(data.get("items"), list) else []
    items = [_normalise_item(item, index) for index, item in enumerate(raw_items)]
    if not items:
        raise ValueError("ИИ не создал ни одного материала.")
    now = datetime.now(timezone.utc).isoformat()
    return {
        "package_id": str(uuid.uuid4()),
        "owner_telegram_id": int(owner_id),
        "created_at": now,
        "updated_at": now,
        "status": "draft",
        "week_title": _clean_text(data.get("week_title"), 300)
        or "Недельный комплект",
        "strategy": _clean_text(data.get("strategy"), 3000),
        "settings": settings,
        "items": items,
    }


def _supabase_config():
    url = _clean_text(st.secrets.get("SUPABASE_URL"), 1000).rstrip("/")
    key = _clean_text(st.secrets.get("SUPABASE_SECRET_KEY"), 5000)
    return url, key


def _supabase_headers(key, *, prefer=""):
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def _save_package(package):
    url, key = _supabase_config()
    if not url or not key:
        return False, "Supabase не настроен. Комплект сохранён только на этом экране."
    payload = {
        "package_id": package["package_id"],
        "owner_telegram_id": int(package["owner_telegram_id"]),
        "project_name": _clean_text(package.get("settings", {}).get("project_name"), 240),
        "week_title": _clean_text(package.get("week_title"), 300),
        "status": _clean_text(package.get("status") or "draft", 40),
        "settings": package.get("settings") or {},
        "package": package,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        response = requests.post(
            f"{url}/rest/v1/agency_content_packages?on_conflict=package_id",
            headers=_supabase_headers(
                key,
                prefer="resolution=merge-duplicates,return=minimal",
            ),
            json=payload,
            timeout=25,
        )
        if response.status_code in {404, 400} and "agency_content_packages" in response.text:
            return False, "Сначала создайте таблицу из файла content_factory_setup.sql."
        response.raise_for_status()
        return True, ""
    except requests.RequestException as exc:
        return False, "Не удалось сохранить комплект: " + str(exc)[:240]


def _load_latest_package(owner_id):
    url, key = _supabase_config()
    if not url or not key:
        return None
    try:
        response = requests.get(
            f"{url}/rest/v1/agency_content_packages",
            headers=_supabase_headers(key),
            params={
                "owner_telegram_id": f"eq.{int(owner_id)}",
                "select": "package",
                "order": "updated_at.desc",
                "limit": 1,
            },
            timeout=20,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list) or not rows:
            return None
        package = rows[0].get("package")
        return package if isinstance(package, dict) else None
    except (requests.RequestException, ValueError, TypeError):
        return None


def _audience_text(target_profile):
    if not isinstance(target_profile, dict) or not target_profile:
        return ""
    preferred = []
    for key in (
        "portrait",
        "who_is_this",
        "current_situation",
        "goals",
        "pains",
        "dreams",
        "decision_triggers",
    ):
        value = target_profile.get(key)
        if value:
            preferred.append(f"{key}: {value}")
    return "\n".join(preferred)[:7000]


def _build_generation_prompt(settings):
    return f"""
Создай недельный пакет контента.

ПРОЕКТ: {settings['project_name']}
ЧТО ПРОДВИГАЕМ: {settings['offer']}
ЦЕЛЕВАЯ АУДИТОРИЯ:
{settings['audience']}

ЦЕЛЬ НЕДЕЛИ: {settings['goal']}
ГЛАВНАЯ МЫСЛЬ: {settings['key_message']}
ТОН: {settings['tone']}
ЯЗЫК: {settings['language']}

Нужно создать ровно:
- {settings['reels_count']} Reels;
- {settings['posts_count']} поста;
- {settings['carousels_count']} карусель.

Reels: вертикальный формат 9:16, длительность 20–45 секунд, сильный хук
в первые две секунды, естественная устная речь, 4–7 коротких сцен.
Не обещай доход и не используй давление.

Пост: самостоятельная полезная мысль, живой текст, без канцелярита.

Карусель: обложка плюс 5–7 коротких слайдов, один тезис на слайд.

Используй разные смысловые углы: польза, история, объяснение, возражение,
пример, человеческая ситуация. Не повторяй одну мысль разными словами.

Строгая схема JSON:
{{
  "week_title": "краткое название недели",
  "strategy": "почему этот пакет должен заинтересовать выбранную аудиторию",
  "items": [
    {{
      "id": "reel_1",
      "format": "reel|post|carousel",
      "day": "Понедельник",
      "title": "название",
      "goal": "задача материала",
      "hook": "хук — только для Reels",
      "script": "текст речи — только для Reels",
      "scenes": ["сцена 1", "сцена 2"],
      "post_text": "текст поста",
      "slides": ["обложка", "слайд 2"],
      "caption": "подпись под публикацией",
      "cta": "одно естественное действие",
      "visual_brief": "что должно быть в кадре или на иллюстрации",
      "professionals": ["имена только тех профессионалов Агентства W, которые должны быть в кадре"]
    }}
  ]
}}
""".strip()


def _item_label(item):
    labels = {"reel": "🎬 Reels", "post": "📝 Пост", "carousel": "🖼️ Карусель"}
    return labels.get(item.get("format"), "Материал")


def _item_source_text(item):
    if item.get("format") == "reel":
        return "\n\n".join(
            value for value in (item.get("hook"), item.get("script"), item.get("caption")) if value
        )
    if item.get("format") == "carousel":
        return "\n\n".join((item.get("slides") or []) + [item.get("caption") or ""])
    return "\n\n".join(
        value for value in (item.get("post_text"), item.get("caption")) if value
    )


def _package_as_text(package):
    parts = [package.get("week_title") or "Недельный комплект", "", package.get("strategy") or ""]
    for item in package.get("items", []):
        parts.extend(
            [
                "",
                f"{_item_label(item)} — {item.get('day', '')}",
                item.get("title") or "",
                f"Цель: {item.get('goal') or ''}",
            ]
        )
        if item.get("hook"):
            parts.append("Хук: " + item["hook"])
        if item.get("script"):
            parts.append("Сценарий:\n" + item["script"])
        if item.get("scenes"):
            parts.append("Сцены:\n- " + "\n- ".join(item["scenes"]))
        if item.get("post_text"):
            parts.append("Текст:\n" + item["post_text"])
        if item.get("slides"):
            parts.append("Слайды:\n- " + "\n- ".join(item["slides"]))
        if item.get("caption"):
            parts.append("Подпись:\n" + item["caption"])
        if item.get("cta"):
            parts.append("Действие: " + item["cta"])
    return "\n".join(parts).strip()


def _render_item(
    package,
    item,
    owner_id,
    generate_illustration_fn,
    create_avatar_video_fn,
    get_avatar_video_fn,
):
    package_id = package["package_id"]
    item_id = item["id"]
    status = item.get("status") or "draft"
    status_label = "✅ утверждён" if status == "approved" else "🟡 черновик"

    with st.expander(
        f"{_item_label(item)} · {item.get('day') or 'День не выбран'} · "
        f"{item.get('title') or 'Без названия'} · {status_label}",
        expanded=False,
    ):
        prefix = f"cf_{owner_id}_{package_id}_{item_id}"
        item["title"] = st.text_input("Название", value=item.get("title", ""), key=prefix + "_title")
        item["goal"] = st.text_area("Задача материала", value=item.get("goal", ""), height=80, key=prefix + "_goal")

        if item.get("format") == "reel":
            item["hook"] = st.text_area("Хук — первые две секунды", value=item.get("hook", ""), height=80, key=prefix + "_hook")
            item["script"] = st.text_area("Текст речи", value=item.get("script", ""), height=220, key=prefix + "_script")
            scene_text = "\n".join(item.get("scenes") or [])
            scene_text = st.text_area("План кадров — одна сцена с новой строки", value=scene_text, height=160, key=prefix + "_scenes")
            item["scenes"] = [line.strip() for line in scene_text.splitlines() if line.strip()]
        elif item.get("format") == "carousel":
            slides_text = "\n\n".join(item.get("slides") or [])
            slides_text = st.text_area("Слайды — разделяйте пустой строкой", value=slides_text, height=260, key=prefix + "_slides")
            item["slides"] = [part.strip() for part in slides_text.split("\n\n") if part.strip()]
        else:
            item["post_text"] = st.text_area("Текст поста", value=item.get("post_text", ""), height=260, key=prefix + "_post")

        item["caption"] = st.text_area("Подпись в Instagram", value=item.get("caption", ""), height=150, key=prefix + "_caption")
        item["cta"] = st.text_area("Призыв к действию", value=item.get("cta", ""), height=80, key=prefix + "_cta")
        item["visual_brief"] = st.text_area("Задание для визуала", value=item.get("visual_brief", ""), height=120, key=prefix + "_visual")
        saved_professionals = [
            name
            for name in item.get("professionals", [])
            if name in PROFESSIONAL_PORTRAITS
        ]
        if not saved_professionals:
            saved_professionals = _guess_professionals(item)
        item["professionals"] = st.multiselect(
            "Профессионалы в кадре",
            options=list(PROFESSIONAL_PORTRAITS),
            default=saved_professionals,
            help=(
                "Выбранные герои будут созданы по их эталонным портретам, "
                "в фирменных пиджаках со знаком Агентства W."
            ),
            key=prefix + "_professionals",
        )

        approve_col, image_col = st.columns(2)
        if approve_col.button(
            "✅ Утвердить" if status != "approved" else "↩️ Вернуть в черновики",
            key=prefix + "_approve",
            use_container_width=True,
        ):
            item["status"] = "approved" if status != "approved" else "draft"
            package["updated_at"] = datetime.now(timezone.utc).isoformat()
            st.rerun()

        image_state_key = prefix + "_image_result"
        if image_col.button("🎨 Создать изображение", key=prefix + "_image", use_container_width=True):
            source_text = _item_source_text(item)
            portrait_references = _portrait_references(item.get("professionals") or [])
            available_names = {
                reference.get("name")
                for reference in portrait_references
                if reference.get("kind") == "portrait"
            }
            missing_names = [
                name
                for name in item.get("professionals", [])
                if name not in available_names
            ]
            if missing_names:
                st.error(
                    "Не найдены эталонные портреты: "
                    + ", ".join(missing_names)
                    + ". "
                    "Сначала загрузите пять файлов content_ref_*.webp в папку assets."
                )
                return
            with st.spinner("Стагирит и Художник создают визуал..."):
                result = generate_illustration_fn(
                    source_text,
                    change_request=item.get("visual_brief") or "",
                    size="1024x1536",
                    reference_images=portrait_references,
                )
            st.session_state[image_state_key] = result

        image_result = st.session_state.get(image_state_key)
        if isinstance(image_result, dict):
            if image_result.get("ok") and image_result.get("image_bytes"):
                st.image(image_result["image_bytes"], caption="Визуал Контент-завода W")
                st.download_button(
                    "⬇️ Скачать изображение",
                    data=image_result["image_bytes"],
                    file_name=f"{item_id}.png",
                    mime="image/png",
                    key=prefix + "_image_download",
                    use_container_width=True,
                )
            elif image_result.get("error"):
                st.error(str(image_result["error"]))

        if item.get("format") == "reel":
            st.markdown("#### 🎥 Видео с подключённым аватаром")
            avatar_state_key = prefix + "_avatar_state"
            avatar_state = st.session_state.get(avatar_state_key, {})
            avatar_state = avatar_state if isinstance(avatar_state, dict) else {}
            start_col, check_col = st.columns(2)
            if start_col.button("🎬 Создать видео", key=prefix + "_video_start", use_container_width=True):
                with st.spinner("Аватар записывает Reels..."):
                    result = create_avatar_video_fn(item.get("script") or "")
                if result.get("ok"):
                    avatar_state = result
                    st.session_state[avatar_state_key] = avatar_state
                    st.success("Видео принято в работу.")
                else:
                    st.error(str(result.get("error") or "Не удалось создать видео."))

            video_id = str(avatar_state.get("video_id") or "").strip()
            if check_col.button(
                "🔄 Проверить видео",
                key=prefix + "_video_check",
                disabled=not bool(video_id),
                use_container_width=True,
            ):
                with st.spinner("Проверяем готовность видео..."):
                    result = get_avatar_video_fn(video_id)
                if result.get("ok"):
                    avatar_state.update(result)
                    st.session_state[avatar_state_key] = avatar_state
                else:
                    st.error(str(result.get("error") or "Не удалось проверить видео."))

            video_url = str(avatar_state.get("video_url") or "").strip()
            if video_url and str(avatar_state.get("status") or "").lower() == "completed":
                st.video(video_url)
            elif video_id:
                st.info("Видео ещё создаётся. Через некоторое время нажмите «Проверить видео».")


def render_content_factory(
    owner_telegram_id,
    owner_name,
    ask_ai_fn,
    generate_illustration_fn,
    create_avatar_video_fn,
    get_avatar_video_fn,
    target_profile=None,
):
    """Первая устанавливаемая очередь Контент-завода W."""
    owner_id = int(owner_telegram_id)
    state_key = f"content_factory_package_{owner_id}"
    load_marker = f"content_factory_loaded_{owner_id}"

    st.markdown("## 🏭 Контент-завод W")
    st.caption(
        "Не шестой сотрудник, а общая мастерская пяти героев Агентства W. "
        "Первая очередь создаёт недельный пакет и запускает визуалы и аватарные Reels."
    )
    st.info(
        "Стагирит координирует · Неония понимает аудиторию · Неона готовит "
        "общение · Тео помогает объяснить сложное · Неола сопровождает партнёра. "
        "Разведчик W работает за кулисами и приносит лучшие рыночные идеи."
    )

    if not st.session_state.get(load_marker):
        latest = _load_latest_package(owner_id)
        if latest:
            st.session_state[state_key] = latest
        st.session_state[load_marker] = True

    default_audience = _audience_text(target_profile)
    with st.expander("⚙️ Задание заводу", expanded=not bool(st.session_state.get(state_key))):
        with st.form(f"content_factory_form_{owner_id}"):
            project_name = st.text_input("Проект", value="Агентство W")
            offer = st.text_area(
                "Что продвигаем",
                value=(
                    "Систему из пяти ИИ-профессионалов, которая помогает предпринимателю "
                    "вернуть время, находить партнёров и вести диалоги."
                ),
                height=110,
            )
            audience = st.text_area(
                "Для кого создаём контент",
                value=default_audience,
                placeholder="Опишите человека, его ситуацию, цели и трудности.",
                height=160,
            )
            goal = st.text_input(
                "Цель недели",
                value="Получить содержательные комментарии и новые входящие диалоги",
            )
            key_message = st.text_area(
                "Главная мысль",
                value="Пять ИИ-профессионалов Агентства W возвращают человеку время.",
                height=90,
            )
            tone = st.selectbox(
                "Стиль",
                [
                    "тёплый, умный и понятный",
                    "деловой и убедительный",
                    "вдохновляющий без пафоса",
                    "спокойный экспертный",
                ],
            )
            language = st.selectbox("Язык", ["русский", "немецкий", "английский"])
            reels_count = st.number_input("Reels", min_value=1, max_value=7, value=3, step=1)
            posts_count = st.number_input("Посты", min_value=0, max_value=7, value=2, step=1)
            carousels_count = st.number_input("Карусели", min_value=0, max_value=5, value=1, step=1)
            submitted = st.form_submit_button("🏭 Создать недельный пакет", use_container_width=True)

        if submitted:
            settings = {
                "project_name": _clean_text(project_name, 240),
                "offer": _clean_text(offer, 4000),
                "audience": _clean_text(audience, 7000),
                "goal": _clean_text(goal, 1000),
                "key_message": _clean_text(key_message, 1500),
                "tone": _clean_text(tone, 240),
                "language": _clean_text(language, 40),
                "reels_count": int(reels_count),
                "posts_count": int(posts_count),
                "carousels_count": int(carousels_count),
            }
            if not settings["offer"] or not settings["audience"]:
                st.warning("Заполните, что продвигаем и для кого создаём контент.")
            else:
                with st.spinner("Пять героев Агентства W собирают недельный пакет..."):
                    answer = ask_ai_fn(FACTORY_SYSTEM_PROMPT, _build_generation_prompt(settings))
                    try:
                        package = _normalise_package(_json_from_answer(answer), owner_id, settings)
                    except (ValueError, json.JSONDecodeError) as exc:
                        st.error(str(exc))
                        st.text_area("Ответ ИИ для проверки", value=str(answer), height=240)
                    else:
                        st.session_state[state_key] = package
                        saved, error = _save_package(package)
                        if saved:
                            st.success("✅ Недельный пакет создан и сохранён.")
                        else:
                            st.warning(error)
                        st.rerun()

    package = st.session_state.get(state_key)
    if not isinstance(package, dict):
        st.write(
            f"{owner_name}, заполните короткое задание выше. Контент-завод подготовит "
            "первый недельный комплект, но ничего не опубликует без вашего решения."
        )
        return

    items = package.get("items") if isinstance(package.get("items"), list) else []
    approved_count = sum(1 for item in items if item.get("status") == "approved")
    st.markdown(f"### {package.get('week_title') or 'Недельный комплект'}")
    if package.get("strategy"):
        st.write(package["strategy"])
    st.caption(f"Материалов: {len(items)} · утверждено: {approved_count} · черновиков: {len(items) - approved_count}")

    save_col, approve_col, new_col = st.columns(3)
    if save_col.button("💾 Сохранить изменения", use_container_width=True, key=f"cf_save_{owner_id}"):
        package["updated_at"] = datetime.now(timezone.utc).isoformat()
        saved, error = _save_package(package)
        if saved:
            st.success("Изменения сохранены.")
        else:
            st.warning(error)

    if approve_col.button("✅ Утвердить весь пакет", use_container_width=True, key=f"cf_approve_all_{owner_id}"):
        for item in items:
            item["status"] = "approved"
        package["status"] = "approved"
        package["updated_at"] = datetime.now(timezone.utc).isoformat()
        _save_package(package)
        st.rerun()

    if new_col.button("➕ Новый пакет", use_container_width=True, key=f"cf_new_{owner_id}"):
        st.session_state.pop(state_key, None)
        st.rerun()

    st.download_button(
        "⬇️ Скачать весь комплект текстом",
        data=_package_as_text(package).encode("utf-8"),
        file_name="content_factory_week.txt",
        mime="text/plain",
        use_container_width=True,
        key=f"cf_download_{owner_id}",
    )

    for item in items:
        _render_item(
            package,
            item,
            owner_id,
            generate_illustration_fn,
            create_avatar_video_fn,
            get_avatar_video_fn,
        )

    st.divider()
    st.caption(
        "Следующая очередь: автоматическая сборка ролика из сцен, озвучки и "
        "субтитров, календарь публикаций и отправка в Instagram после утверждения."
    )
