"""Original, reusable Agency W briefs; selecting one never starts generation."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import MutableMapping

import streamlit as st


CATEGORIES = {
    "all": "Все",
    "texts": "Тексты",
    "images": "Изображения",
    "carousel": "Карусели",
    "video": "Видео и мультфильмы",
    "speech": "Озвучка",
}

# These are our own task recipes. No paid-provider names, competitor prompts,
# invented performance claims, or unavailable output formats are advertised.
_TEMPLATES = (
    {
        "id": "post-small-story", "category": "texts", "tool": "texts", "content_format": "post",
        "title": "История из жизни", "description": "Узнаваемая ситуация, конкретное действие и понятный вывод.",
        "thumbnail": "content_ref_theo.webp",
        "prompt": "Напиши короткий пост для проекта «{name}». Начни с конкретной узнаваемой ситуации из жизни аудитории: {audience}. Покажи одну повторяющуюся задачу, способ упростить её и человеческий результат. Используй только сведения о проекте, не выдумывай истории клиентов и цифры. Заверши одним мягким приглашением: {cta}. Голос — живой и ясный, без рекламных штампов.",
    },
    {
        "id": "post-checklist", "category": "texts", "tool": "texts", "content_format": "post",
        "title": "Полезный чек-лист", "description": "Пять небольших действий, которые читатель может проверить сегодня.",
        "prompt": "Подготовь пост-чек-лист для аудитории проекта «{name}»: {audience}. Выбери одну задачу, связанную с продуктом: {product}. Дай пять конкретных шагов, а не общие советы. Каждый пункт — одно действие и его смысл. Не обещай гарантированный результат и не приписывай продукту неподтверждённые возможности. В конце предложи: {cta}.",
    },
    {
        "id": "post-objection", "category": "texts", "tool": "texts", "content_format": "post",
        "title": "Ответ на сомнение", "description": "Спокойно объяснить пользу, не давя на читателя.",
        "prompt": "Напиши короткий ответ на распространённое сомнение аудитории {audience} о продукте проекта «{name}»: {product}. Сначала точно сформулируй сомнение, затем объясни, как оценить решение на своей задаче. Дай один практический пример без выдуманных клиентов, процентов и доходов. Уважай право человека отказаться. Следующий шаг: {cta}.",
    },
    {
        "id": "image-life", "category": "images", "tool": "images", "content_format": "image",
        "title": "Живой момент", "description": "Естественная фотографическая сцена с понятным действием.",
        "thumbnail": "content_ref_office.webp",
        "prompt": "Создай реалистичное вертикальное изображение для проекта «{name}». В кадре человек из аудитории {audience} завершает одну рутинную задачу и переключается на важное для себя дело. Одно отчётливо видимое действие, естественные эмоции, правдоподобные руки и предметы. Тёплый свет, спокойная современная композиция. Без надписей, искусственных роботов, светящихся сеток и случайных логотипов. Стиль проекта: {visual_style}.",
    },
    {
        "id": "image-product", "category": "images", "tool": "images", "content_format": "image",
        "title": "Товар в красивой сцене", "description": "Добавьте фото товара как образец в инструменте изображений.",
        "prompt": "Создай аккуратную рекламную фотографию товара из загруженного образца для проекта «{name}». Сохрани форму, цвет, упаковку и видимые отличительные детали товара. Размести его в естественной сцене использования, подходящей аудитории {audience}. Товар — главный объект, фон поддерживает его и не отвлекает. Правдоподобный свет и тени, без дополнительных надписей, лишних предметов и выдуманных свойств. Стиль: {visual_style}.",
    },
    {
        "id": "image-story-frame", "category": "images", "tool": "images", "content_format": "image",
        "title": "Вертикальный кадр для истории", "description": "Один выразительный визуал, удобный для сторис.",
        "prompt": "Подготовь одно вертикальное изображение для истории проекта «{name}». Одна главная мысль о пользе продукта: {product}. Покажи её через действие или ясную визуальную метафору для аудитории {audience}. Оставь свободное место сверху и снизу под будущую подпись, не рисуй сам текст. Избегай сложного коллажа и мелких деталей. Оформление: {visual_style}.",
    },
    {
        "id": "carousel-five-steps", "category": "carousel", "tool": "carousel", "content_format": "carousel",
        "title": "Пять шагов к решению", "description": "Карусель: проблема → действия → следующий шаг.",
        "thumbnail": "content_ref_stagirite.webp",
        "prompt": "Создай карусель из пяти слайдов для проекта «{name}» и аудитории {audience}. Тема — как решить одну конкретную задачу с помощью продукта: {product}. Первый слайд — узнаваемая проблема и причина читать дальше. Следующие три — последовательные полезные действия с коротким примером. Последний — ясный вывод и приглашение: {cta}. На каждом слайде одна мысль, короткий заголовок, мало текста. Не выдумывай факты, кейсы и цифры.",
    },
    {
        "id": "carousel-three-mistakes", "category": "carousel", "tool": "carousel", "content_format": "carousel",
        "title": "Три ошибки и что делать вместо", "description": "Практичный разбор без стыда и громких обещаний.",
        "prompt": "Подготовь карусель для аудитории {audience} проекта «{name}»: три частые ошибки в задаче, связанной с продуктом {product}. Структура: цепляющая обложка; по одному слайду на ошибку с конкретной альтернативой; заключительный слайд с действием {cta}. Не обвиняй читателя. Все примеры обозначай как ситуации, а не подтверждённые истории клиентов. Одна короткая мысль на слайд, понятная без дополнительных пояснений.",
    },
    {
        "id": "video-live-action", "category": "video", "tool": "video", "content_format": "reel", "video_style": "live",
        "title": "Живой сюжет", "description": "Люди и действия в кадре, событие с первой секунды.",
        "prompt": "Создай короткий живой ролик для проекта «{name}» и аудитории {audience}. С первой секунды покажи конкретное действие или неожиданную ситуацию, связанную с продуктом {product}. Дальше — развитие, понятный поворот и развязка. Люди действительно двигаются и взаимодействуют с предметами; каждый план добавляет смысл. Голос энергичный, естественный, без крика. Собственный сюжет и персонажи, без копирования чужих лиц, голоса, текста и бренда. Финал: {cta}. Официальный логотип проекта должен присутствовать в готовом видео.",
    },
    {
        "id": "video-cartoon-action", "category": "video", "tool": "video", "content_format": "reel", "video_style": "cartoon",
        "title": "Мультфильм с собственной историей", "description": "Выразительные персонажи, движение и короткая сюжетная дуга.",
        "prompt": "Создай короткий авторский мультфильм для проекта «{name}» и аудитории {audience}. Герой сталкивается с одной узнаваемой трудностью, пробует изменить подход и получает понятную развязку, связанную с продуктом {product}. Начни с события, а не представления бренда. Придумай своих персонажей с единым обликом во всех сценах; живые движения, выразительная мимика и ясные действия. Не используй известных чужих персонажей и не обещай неподтверждённые результаты. Голос вовлечённый и разговорный. Следующий шаг зрителя: {cta}. Используй настоящий логотип проекта в финальном оформлении.",
    },
    {
        "id": "speech-invitation", "category": "speech", "tool": "speech", "content_format": "audio",
        "title": "Короткое приглашение", "description": "Готовый текст для пробы голоса; слова можно изменить перед озвучкой.",
        "prompt": "Каждый день находится дело, которое снова забирает ваше время. Попробуйте выбрать одно такое дело и подумать, какую часть подготовки можно упростить. Начните с небольшой задачи, проверьте результат и решите, подходит ли вам этот способ. Познакомьтесь с проектом «{name}» и выберите свой следующий шаг.",
    },
)


def _owner(value):
    if isinstance(value, bool) or not (type(value) is int or isinstance(value, str) and value.isdecimal()):
        raise ValueError("Не удалось определить владельца шаблонов.")
    value = int(value)
    if value <= 0:
        raise ValueError("Не удалось определить владельца шаблонов.")
    return value


def list_templates(category="all", query=""):
    """Return bounded copies, so callers cannot modify the reusable recipes."""
    if category not in CATEGORIES or not isinstance(query, str) or len(query) > 500:
        raise ValueError("Выберите раздел и укажите короткий поисковый запрос.")
    query = query.strip().casefold()
    return [copy.deepcopy(item) for item in _TEMPLATES
            if (category == "all" or item["category"] == category) and
            (not query or query in " ".join(str(item.get(field) or "") for field in
                                           ("title", "description", "prompt")).casefold())]


def template_handoff(template_id, project=None):
    """Build a recipe for the selected project without model or storage calls."""
    item = next((value for value in _TEMPLATES if value["id"] == template_id), None)
    if item is None:
        raise ValueError("Шаблон не найден.")
    project = project if isinstance(project, dict) else {}
    defaults = {"name": "мой проект", "audience": "люди, которым полезен проект",
                "product": "продукт или услуга проекта", "cta": "узнать больше о проекте",
                "visual_style": "ясная композиция и естественные цвета"}
    limits = {"name": 120, "audience": 700, "product": 1000, "cta": 500, "visual_style": 600}
    context = {name: str(project.get(name) or default).strip()[:limits[name]] or default
               for name, default in defaults.items()}
    prompt = item["prompt"].format_map(context)
    return {"template_id": item["id"], "tool": item["tool"], "prompt": prompt,
            "content_format": item["content_format"], "video_style": item.get("video_style"),
            "project_id": project.get("id")}


def apply_template_handoff(owner_id, project, session_state: MutableMapping):
    """Populate the destination before its widgets render; never submit it."""
    owner_id = _owner(owner_id)
    key = f"content_studio_{owner_id}_template_handoff"
    pending = session_state.get(key)
    if (not isinstance(pending, dict) or not isinstance(project, dict) or
            not isinstance(project.get("id"), str) or not 1 <= len(project["id"]) <= 100):
        return False
    try:
        if _owner(project.get("owner_id")) != owner_id or _owner(pending.get("owner_id")) != owner_id:
            return False
    except ValueError:
        return False
    if pending.get("project_id") is not None and pending.get("project_id") != project["id"]:
        return False
    try:
        selection = template_handoff(pending.get("template_id"), project)
    except ValueError:
        return False
    prefix = f"studio_tool_{owner_id}_{project['id']}"
    tool = selection["tool"]
    if tool in {"texts", "images", "speech"}:
        kind = {"texts": "text", "images": "image", "speech": "audio"}[tool]
        session_state[prefix + f"_{kind}_prompt"] = selection["prompt"]
        if selection["template_id"] == "image-story-frame":
            session_state[prefix + "_image_size"] = "1024x1536"
    else:
        prefix = f"cf_auto_{owner_id}_{project['id']}_brief"
        session_state[prefix + "_topic"] = selection["prompt"]
        session_state[prefix + "_format"] = selection["content_format"]
    if selection.get("video_style"):
        session_state[f"content_studio_{owner_id}_video_style"] = selection["video_style"]
    session_state.pop(key, None)
    return True


def render_templates(owner_id, project=None):
    """Show a searchable recipe gallery with explicit, generation-free use."""
    from agency_access_control import is_agency_access_revoked

    owner_id = _owner(owner_id)
    if is_agency_access_revoked(owner_id):
        return
    if project is not None:
        try:
            if not isinstance(project, dict) or _owner(project.get("owner_id")) != owner_id:
                raise ValueError
        except ValueError:
            st.error("Проект не найден в вашем кабинете.")
            return
    st.caption("Выберите задачу — готовый пример появится в нужном инструменте. Его можно изменить перед запуском.")
    search, filters = st.columns([2, 1])
    query = search.text_input("Найти пример", max_chars=500, key=f"studio_templates_{owner_id}_query")
    category = filters.selectbox("Категория", list(CATEGORIES), format_func=CATEGORIES.get,
                                 key=f"studio_templates_{owner_id}_category")
    items = list_templates(category, query)
    if not items:
        st.info("По этому запросу примеров пока нет. Попробуйте другое слово или категорию.")
        return
    root = Path(__file__).resolve().parent / "assets"
    for start in range(0, len(items), 3):
        for column, item in zip(st.columns(3), items[start:start + 3]):
            with column, st.container(border=True):
                thumbnail = root / item.get("thumbnail", "")
                if item.get("thumbnail") and thumbnail.is_file():
                    st.image(str(thumbnail), caption="Визуальный образец Агентства W", width="stretch")
                st.caption(CATEGORIES[item["category"]])
                st.markdown("**" + item["title"] + "**")
                st.write(item["description"])
                selection = template_handoff(item["id"], project)
                with st.expander("Посмотреть и скопировать задание"):
                    st.code(selection["prompt"], language=None, wrap_lines=True)
                if st.button("Использовать", key=f"studio_templates_{owner_id}_{item['id']}_use", width="stretch"):
                    selection["owner_id"] = owner_id
                    st.session_state[f"content_studio_{owner_id}_template_handoff"] = selection
                    st.session_state[f"content_studio_{owner_id}_next"] = selection["tool"]
                    st.rerun()
