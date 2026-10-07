import base64
import json
import hashlib
import math
import os
import re
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import requests
import streamlit as st
from PIL import Image, ImageDraw, ImageFont, ImageOps


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


def _office_reference_path():
    """Единый эталон виртуального офиса Агентства W."""
    base = Path(__file__).resolve().parent / "assets"
    for name in ("content_ref_office.png", "content_ref_office.webp"):
        path = base / name
        if path.is_file():
            return path
    return None


def _portrait_references(names, *, use_office=False, include_logo=True):
    """Подключает портреты, фирменный знак и офис только к нужной сцене."""
    base = Path(__file__).resolve().parent / "assets"
    references = []
    if use_office:
        office = _office_reference_path()
        if office:
            references.append({
                "name": "Точный интерьер виртуального офиса Агентства W",
                "kind": "office",
                "image_bytes": office.read_bytes(),
                "mime_type": "image/png" if office.suffix == ".png" else "image/webp",
            })
    for name in names or []:
        filename = PROFESSIONAL_PORTRAITS.get(name)
        if filename and (base / filename).is_file():
            references.append({
                "name": name,
                "kind": "portrait",
                "image_bytes": (base / filename).read_bytes(),
                "mime_type": "image/webp",
            })
    if include_logo and (base / "agency_w_icon.png").is_file():
        references.append({
            "name": "Официальный логотип Агентства W",
            "kind": "logo",
            "image_bytes": (base / "agency_w_icon.png").read_bytes(),
            "mime_type": "image/png",
        })
    return references

AGENCY_W_AUDIENCE_BASELINE = (
    "Предприниматели, сетевые лидеры, эксперты и руководители, которые строят "
    "команду и структуру и хотят передать цифровой команде часть рутинного поиска, "
    "переписки, организации встреч и сопровождения, чтобы высвободить время для жизни."
)

_AUDIENCE_FOREIGN_PROJECT_MARKERS = (
    "24/7",
    "закрытый клуб",
    "клуб/лагерь",
    "лагерь",
    "lodge",
    "монетизац",
    "реферал",
    "nft",
    "крипт",
    "инвестиц",
    "экосистем",
    "привилеги",
)


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

Агентство W — это цифровая команда и рабочая структура вокруг предпринимателя.
Не называй Агентство W «закрытым клубом», «лагерем», NFT-/реферальной экосистемой,
инвестиционным или криптопроектом. Эти темы относятся к другим проектам и не должны
проникать в контент Агентства W.

Создавай оригинальный контент, а не копии конкурентов. Каждый материал должен:
- приносить человеку практическую пользу;
- звучать естественно и понятно;
- поддерживать главную идею Агентства W: «Мы возвращаем человеку время»;
- вызывать интерес без давления, ложных обещаний и навязчивых продаж;
- завершаться одним ясным и уместным действием читателя;
- быть пригодным для профессионального Instagram-аккаунта.

Контент-завод сам выполняет редакционную работу: из человеческого задания сам строит
драматургию, последовательность слайдов/сцен, тексты и визуальные задачи. Не требуй от
владельца заранее придумывать структуру материала.

Постоянное правило визуального мира Агентства W:
- если действие происходит в виртуальном офисе Агентства W, это один и тот же
  узнаваемый фирменный офис; не придумывай каждый раз новый интерьер;
- если действие происходит в реальном мире — дома, в кафе, городе, на прогулке,
  у моря, в лесу, горах или другом природном пейзаже — окружение остаётся
  естественным и правдоподобным; не перекрашивай природу и реальный мир в
  сине-золотую фирменную декорацию;
- фирменность в реальном мире передавай деликатно через героя, аксессуар или
  композицию, не ломая естественный вид места.

Верни ТОЛЬКО корректный JSON без Markdown и без пояснений.
""".strip()


def _clean_text(value, limit=12000):
    return str(value or "").strip()[:limit]


_DIRECTION_MARKERS = re.compile(
    r"(?im)(?:^|[\n.!?]\s*)(?:сцена|кадр|план|камера|переход|подпись|экран)\s*\d*\s*[:—-]"
    r"|\b(?:в кадре|крупный план|общий план|на экране|появляется надпись|смена сцены)\b"
)


def _has_direction_markers(text):
    """Не разрешает отправлять режиссёрские команды в платную озвучку."""
    return bool(_DIRECTION_MARKERS.search(str(text or "")))


def _prepare_reel_content(item, ask_ai_fn):
    """Создаёт отдельно чистую речь диктора и технический план кадров."""
    source_scenes = "\n".join(item.get("scenes") or [])
    user_prompt = f"""
Подготовь финальную основу короткого Instagram Reels длительностью 25–45 секунд.

ТЕМА: {item.get('title') or ''}
ХУК: {item.get('hook') or ''}
ИСХОДНЫЙ ТЕКСТ: {item.get('script') or ''}
ТЕХНИЧЕСКИЙ ПЛАН:
{source_scenes}
ПРИЗЫВ: {item.get('cta') or ''}

Верни только JSON:
{{
  "spoken_text": "только слова, которые естественно произносит диктор",
  "scenes": ["короткое визуальное описание сцены 1", "сцена 2"]
}}

ОБЯЗАТЕЛЬНО:
- spoken_text содержит 65–105 слов и звучит как живая русская речь;
- в spoken_text запрещены слова и конструкции «сцена», «кадр», «план»,
  «камера», «на экране», «появляется надпись», номера сцен, ремарки в скобках;
- не описывай, что зритель увидит; говори с самим зрителем о его ситуации;
- первая фраза цепляет, последняя естественно приводит к одному действию;
- scenes содержит 4–6 визуальных сцен без реплик диктора;
- не обещай доход и не используй давление.
""".strip()
    system_prompt = (
        "Ты — режиссёр коротких деловых Reels Агентства W. "
        "Никогда не смешивай произносимую речь с техническими указаниями. "
        "Возвращай только корректный JSON без Markdown."
    )

    last_error = ""
    for _ in range(2):
        try:
            answer = ask_ai_fn(system_prompt, user_prompt)
            data = _json_from_answer(answer)
            spoken_text = _clean_text(data.get("spoken_text"), 1800)
            scenes = data.get("scenes") if isinstance(data.get("scenes"), list) else []
            scenes = [_clean_text(scene, 700) for scene in scenes[:6] if _clean_text(scene, 700)]
            word_count = len(re.findall(r"\b[\wЁёА-Яа-я-]+\b", spoken_text))
            if not spoken_text or word_count < 35:
                last_error = "Текст диктора получился слишком коротким."
            elif _has_direction_markers(spoken_text):
                last_error = "В речи диктора остались технические команды."
            elif len(scenes) < 3:
                last_error = "Получилось слишком мало сцен."
            else:
                return {
                    "ok": True,
                    "spoken_text": spoken_text,
                    "scenes": scenes,
                }
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            last_error = str(exc)

        user_prompt += (
            "\n\nПредыдущий вариант не прошёл проверку. Перепиши его: "
            + (last_error or "строго раздели речь и технический план.")
        )

    return {
        "ok": False,
        "error": (
            "Не удалось безопасно подготовить речь диктора. HeyGen не запущен, "
            "деньги не списаны. " + (last_error or "")
        ).strip(),
    }


def _scene_professionals(scene, selected_names):
    scene_lower = str(scene or "").lower()
    named = [
        name for name in selected_names or [] if name.lower() in scene_lower
    ]
    return named


def _generate_reel_scenes(item, scenes, spoken_text, generate_illustration_fn):
    """Создаёт отдельный вертикальный визуал для каждой сцены."""
    generated = []
    selected = item.get("professionals") or []
    for index, scene in enumerate(scenes[:6], start=1):
        names = _scene_professionals(scene, selected)
        references = _portrait_references(names)
        visual_task = (
            f"Вертикальный кадр {index} из {len(scenes[:6])} для одного Reels. "
            f"Содержание сцены: {scene}. "
            "Фотореалистичная кинематографичная сцена, единый премиальный стиль "
            "Агентства W, тёмно-синие фирменные пиджаки у профессионалов. "
            "Если сцена в виртуальном офисе W — сохраняй один и тот же узнаваемый "
            "офис. Если сцена в городе, доме, кафе, путешествии или на природе — "
            "окружение и цвета должны оставаться естественными, без искусственного "
            "сине-золотого перекрашивания всего мира. "
            "Если виден лацкан или фирменный пиджак, используй только полный "
            "официальный золотой знак из приложенного эталона: щит с двойной "
            "W и светящейся точкой. Обычная одиночная буква W запрещена. "
            "Без других букв, подписей, интерфейсного мусора, "
            "рамок и белых полей. Не добавляй людей, которых нет в описании."
        )
        result = generate_illustration_fn(
            spoken_text,
            change_request=visual_task,
            size="1024x1536",
            reference_images=references,
            fast_mode=True,
        )
        if not isinstance(result, dict) or not result.get("ok") or not result.get("image_bytes"):
            return {
                "ok": False,
                "error": (
                    f"Не удалось создать сцену {index}. "
                    + str((result or {}).get("error") or "Неизвестная ошибка Художника.")
                ),
            }
        generated.append(result["image_bytes"])
    return {"ok": True, "images": generated}


_CAROUSEL_BAD_PHRASES = (
    "лагерь",
    "закрытый клуб",
    "клуб/лагерь",
    "lodge",
    "видите вклад",
    "гарантия привилегий",
    "реферальная схема",
    "реферал",
    "монетизац",
    "nft",
    "крипт",
    "инвестиц",
    "экосистем",
    "уникальная возможность",
    "новый уровень успеха",
)


def _carousel_copy_errors(data):
    """Останавливает бессмыслицу до запуска платного Художника."""
    slides = data.get("slides") if isinstance(data, dict) else None
    errors = []
    if not isinstance(slides, list) or not (6 <= len(slides) <= 8):
        return ["Нужно от 6 до 8 слайдов: обложка, развитие одной истории и финальный CTA."]

    cleaned = [_clean_text(value, 500) for value in slides]
    if any(not value for value in cleaned):
        errors.append("Один из слайдов пустой.")
    if len(cleaned[0]) > 100:
        errors.append("Заголовок обложки длиннее 100 знаков.")
    for index, value in enumerate(cleaned[1:], start=2):
        if len(value) < 25:
            errors.append(f"На слайде {index} мысль не раскрыта.")
        if len(value) > 190:
            errors.append(f"Слайд {index} перегружен текстом.")

    combined = " ".join(cleaned).lower()
    bad = [phrase for phrase in _CAROUSEL_BAD_PHRASES if phrase in combined]
    if bad:
        errors.append("Запрещённые или бессмысленные выражения: " + ", ".join(bad))
    if "/" in combined:
        errors.append("Нельзя соединять непонятные варианты слов через косую черту.")
    if not any(
        marker in combined
        for marker in ("время", "задач", "работ", "диалог", "партнёр", "клиент")
    ):
        errors.append("Не показана конкретная польза для предпринимателя.")

    normalised = [re.sub(r"[^а-яёa-z0-9]+", " ", value.lower()).strip() for value in cleaned]
    if len(set(normalised)) != len(normalised):
        errors.append("В карусели повторяются одинаковые мысли.")
    return errors


def _prepare_carousel_copy(package, item, ask_ai_fn):
    """Сначала создаёт и проверяет продающий текст — без платных изображений."""
    settings = package.get("settings") if isinstance(package.get("settings"), dict) else {}
    project_name = _clean_text(settings.get("project_name") or "Агентство W", 240)
    offer = _clean_text(settings.get("offer"), 4000)
    audience = _clean_text(settings.get("audience"), 5000)
    goal = _clean_text(settings.get("goal"), 1000)
    key_message = _clean_text(settings.get("key_message"), 1500)
    item_goal = _clean_text(item.get("goal"), 700)
    understanding = package.get("understanding") if isinstance(package.get("understanding"), dict) else {}
    original_request = _clean_text(understanding.get("original_request"), 7000)

    prompt = f"""
Создай профессиональный текст Instagram-карусели для незнакомого читателя.

ПРОЕКТ: {project_name}
ЧТО МЫ ПРЕДЛАГАЕМ: {offer}
ДЛЯ КОГО: {audience}
ЦЕЛЬ: {goal}
ГЛАВНАЯ МЫСЛЬ: {key_message}
ТЕМА ЭТОГО МАТЕРИАЛА: {item_goal}
ИСХОДНОЕ ЗАДАНИЕ ВЛАДЕЛЬЦА:
{original_request}

Исходное задание владельца — главный источник смысла, тона, юмора, ограничений и CTA.
Не заменяй явно заданный призыв своим и не упрощай авторский замысел до типовой рекламы.

Карусель должна вести человека по одной ясной логической линии:
узнаваемая проблема → почему она возникает → понятное решение → как работают
профессионалы Агентства W → конкретный результат → спокойный призыв узнать больше.

Верни только JSON:
{{
  "title": "рабочее название материала",
  "slides": [
    "Обложка: короткий сильный заголовок",
    "Дальнейшие слайды: последовательные части одной истории",
    "Финальный слайд: итог и конкретный призыв к действию"
  ],
  "caption": "полезная подпись к публикации из 450–750 знаков",
  "cta": "одно простое действие"
}}

ЖЁСТКИЕ ТРЕБОВАНИЯ:
- сам выбери от 6 до 8 слайдов — столько, сколько нужно для ясной истории без растягивания;
- заголовок обложки — максимум 9 слов, он должен останавливать взгляд за счёт
  узнаваемой боли, важного результата или честного любопытства, но без кликбейта;
- каждый следующий слайд понятен человеку, который впервые слышит о проекте;
- один слайд — одна мысль; никакой воды и канцелярита;
- объясняй конкретно: какая рутина забирает время и кто именно её принимает;
- используй только понятные русские слова;
- запрещены непояснённые термины, аббревиатуры, англицизмы, NFT, Lodge,
  «лагерь», «реферальная схема», «видите вклад», «гарантия привилегий»;
- не смешивай Агентство W с другими проектами, криптовалютой и инвестициями;
- не обещай доход и не выдумывай факты;
- текст должен звучать как работа сильного редактора, а не рекламного бота.
""".strip()
    system = (
        "Ты — главный редактор делового медиа и direct-response копирайтер. "
        "Твоя задача — ясность, смысл, сильный заголовок и логическое движение. "
        "Любая непонятная фраза считается браком. Возвращай только JSON."
    )

    last_errors = []
    for _ in range(3):
        try:
            answer = ask_ai_fn(system, prompt)
            data = _json_from_answer(answer)
            result = {
                "title": _clean_text(data.get("title"), 240),
                "slides": [
                    _clean_text(value, 500)
                    for value in (data.get("slides") or [])
                    if _clean_text(value, 500)
                ],
                "caption": _clean_text(data.get("caption"), 1800),
                "cta": _clean_text(data.get("cta"), 300),
            }
            last_errors = _carousel_copy_errors(result)
            if not last_errors and result["caption"] and result["cta"]:
                result["ok"] = True
                return result
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            last_errors = [str(exc)]
        prompt += (
            "\n\nПредыдущий вариант забракован редактором. Исправь ВСЕ замечания:\n- "
            + "\n- ".join(last_errors or ["Не заполнены подпись или призыв."])
        )

    return {
        "ok": False,
        "error": (
            "Редактор не пропустил текст. Платные изображения не запускались. "
            + "; ".join(last_errors or ["Текст не прошёл проверку смысла."])
        ),
    }


def _font(size, bold=False):
    """Шрифт с кириллицей для подписей на готовых слайдах."""
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


def _wrap_image_text(draw, text, font, max_width, max_lines=6):
    """Переносит русский текст по фактической ширине, а не по числу букв."""
    words = str(text or "").split()
    lines = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and draw.textlength(candidate, font=font) > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        while lines[-1] and draw.textlength(lines[-1] + "…", font=font) > max_width:
            lines[-1] = lines[-1][:-1].rstrip()
        lines[-1] += "…"
    return lines


def _carousel_slide_parts(slide_text, index):
    """Отделяет служебную метку «Шаг 1» от текста, который увидит читатель."""
    text = _clean_text(slide_text, 700)
    match = re.match(
        r"^\s*(обложка|шаг\s*\d+|слайд\s*\d+)\s*[:.\-—]\s*(.+)$",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match:
        kicker = match.group(1).upper().replace("СЛАЙД", "ШАГ")
        body = match.group(2).strip()
    else:
        kicker = "ОБЛОЖКА" if index == 0 else f"ШАГ {index}"
        body = text
    return kicker, body


def _carousel_slide_names(item, slide_text, index):
    """Берёт только нужных героев: так лица сохраняются заметно точнее."""
    selected = [
        name for name in (item.get("professionals") or []) if name in PROFESSIONAL_PORTRAITS
    ]
    named = _scene_professionals(slide_text, selected)
    if named:
        return named[:2]
    if not selected:
        return []
    if not named:
        return []
    return named[:2]


def _render_carousel_slide(image_bytes, slide_text, index, total):
    """Создаёт готовый Instagram-слайд 1080x1350 с точным русским текстом."""
    with Image.open(BytesIO(image_bytes)) as source:
        source = ImageOps.exif_transpose(source).convert("RGB")
        frame = ImageOps.fit(
            source,
            (1080, 1350),
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.42),
        ).convert("RGBA")

    overlay = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    # Мягкое затемнение снизу оставляет иллюстрацию видимой и делает текст читаемым.
    gradient_top = 690
    for y in range(gradient_top, 1350):
        ratio = (y - gradient_top) / (1350 - gradient_top)
        alpha = int(25 + 210 * ratio)
        overlay_draw.line((0, y, 1080, y), fill=(4, 15, 31, alpha), width=1)
    overlay_draw.rounded_rectangle(
        (52, 820, 1028, 1300),
        radius=32,
        fill=(5, 18, 36, 205),
        outline=(202, 157, 54, 160),
        width=2,
    )
    frame = Image.alpha_composite(frame, overlay)
    draw = ImageDraw.Draw(frame)

    gold = (238, 191, 79, 255)
    white = (255, 255, 255, 255)
    muted = (220, 228, 238, 255)
    brand_font = _font(30, bold=True)
    count_font = _font(28, bold=True)
    kicker_font = _font(34, bold=True)

    draw.ellipse((52, 45, 116, 109), fill=(8, 25, 48, 220), outline=gold, width=3)
    w_font = _font(34, bold=True)
    w_box = draw.textbbox((0, 0), "W", font=w_font)
    draw.text(
        (84 - (w_box[2] - w_box[0]) / 2, 77 - (w_box[3] - w_box[1]) / 2 - 3),
        "W",
        font=w_font,
        fill=gold,
    )
    draw.text((134, 58), "АГЕНТСТВО W", font=brand_font, fill=white)
    counter = f"{index + 1}/{total}"
    counter_width = draw.textlength(counter, font=count_font)
    draw.text((1028 - counter_width, 61), counter, font=count_font, fill=muted)

    kicker, body = _carousel_slide_parts(slide_text, index)
    draw.text((92, 865), kicker, font=kicker_font, fill=gold)

    # Подбираем размер так, чтобы даже длинный тезис не вылезал за карточку.
    title_font = None
    title_lines = []
    for size in (62, 58, 54, 50, 46, 42):
        candidate_font = _font(size, bold=True)
        candidate_lines = _wrap_image_text(draw, body, candidate_font, 896, max_lines=5)
        line_height = int(size * 1.20)
        if len(candidate_lines) * line_height <= 320:
            title_font = candidate_font
            title_lines = candidate_lines
            break
    if title_font is None:
        title_font = _font(40, bold=True)
        title_lines = _wrap_image_text(draw, body, title_font, 896, max_lines=6)

    y = 925
    line_height = int(getattr(title_font, "size", 40) * 1.20)
    for line in title_lines:
        draw.text((92, y), line, font=title_font, fill=white)
        y += line_height

    output = BytesIO()
    frame.convert("RGB").save(output, "PNG", optimize=True)
    return output.getvalue()


def _carousel_zip(slide_images, caption):
    """Один архив: слайды уже названы в правильном порядке публикации."""
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, image_bytes in enumerate(slide_images, start=1):
            archive.writestr(f"{index:02d}_carousel_slide.png", image_bytes)
        archive.writestr(
            "caption.txt",
            (str(caption or "").strip() + "\n").encode("utf-8"),
        )
        archive.writestr(
            "README.txt",
            (
                "Instagram-карусель Агентства W\n\n"
                "Загрузите все PNG одним постом по порядку: 01, 02, 03...\n"
                "Текст публикации находится в caption.txt.\n"
            ).encode("utf-8"),
        )
    return output.getvalue()


def _generate_ready_carousel(item, generate_illustration_fn):
    """Генерирует отдельную иллюстрацию и точную типографику для каждого тезиса."""
    slides = [_clean_text(value, 700) for value in (item.get("slides") or [])]
    slides = [value for value in slides if value][:8]
    if len(slides) < 2:
        return {
            "ok": False,
            "error": "Для карусели нужны минимум обложка и один отдельный слайд.",
        }

    completed = []
    for index, slide_text in enumerate(slides):
        names = _carousel_slide_names(item, slide_text, index)
        references = _portrait_references(names)
        kicker, body = _carousel_slide_parts(slide_text, index)
        visual_task = (
            f"Фоновая иллюстрация для слайда {index + 1} из {len(slides)} "
            f"Instagram-карусели Агентства W. Смысл: {body}. "
            f"Общее задание серии: {item.get('visual_brief') or ''}. "
            "Сохраняй единую художественную манеру всей карусели, но НЕ делай "
            "одинаковый искусственный фон на всех слайдах. Если сцена происходит "
            "в виртуальном офисе Агентства W, показывай один и тот же узнаваемый "
            "фирменный офис, а не новый интерьер. Если сцена происходит дома, "
            "в кафе, городе, на прогулке, в путешествии или на природе, показывай "
            "реальное окружение естественно и правдоподобно — природные цвета, "
            "нормальный дневной/вечерний свет, без сине-золотого перекрашивания мира. "
            "Фирменные синий и золотой используй только как деликатные акценты там, "
            "где это уместно. Оставь визуально спокойное пространство в нижней "
            "трети для последующего размещения текста. Не рисуй буквы, цифры, "
            "логотипы, водяные знаки, интерфейсы и рамки."
        )
        result = generate_illustration_fn(
            f"{item.get('title') or ''}. {kicker}. {body}",
            change_request=visual_task,
            size="1024x1536",
            reference_images=references,
            fast_mode=True,
        )
        if not isinstance(result, dict) or not result.get("ok") or not result.get("image_bytes"):
            return {
                "ok": False,
                "error": (
                    f"Не удалось создать слайд {index + 1}. "
                    + str((result or {}).get("error") or "Неизвестная ошибка Художника.")
                ),
            }
        try:
            completed.append(
                _render_carousel_slide(
                    result["image_bytes"], slide_text, index, len(slides)
                )
            )
        except (OSError, ValueError) as exc:
            return {
                "ok": False,
                "error": f"Не удалось оформить слайд {index + 1}: {exc}",
            }

    return {
        "ok": True,
        "slides": completed,
        "zip_bytes": _carousel_zip(completed, item.get("caption") or ""),
    }


def _download_video_bytes(video_url):
    response = requests.get(str(video_url or "").strip(), timeout=180)
    response.raise_for_status()
    if not response.content:
        raise RuntimeError("HeyGen вернул пустой видеофайл.")
    return response.content


def _runway_api_secret():
    """Возвращает ключ Runway из Streamlit Secrets, не показывая его в интерфейсе."""
    try:
        secret = str(st.secrets.get("RUNWAYML_API_SECRET") or "").strip()
    except (FileNotFoundError, KeyError, TypeError):
        secret = ""
    return secret or str(os.getenv("RUNWAYML_API_SECRET") or "").strip()


def _runway_error(response):
    """Преобразует ответ Runway в короткое понятное сообщение без секретов."""
    try:
        payload = response.json()
    except (ValueError, requests.JSONDecodeError):
        payload = {}
    if isinstance(payload, dict):
        detail = (
            payload.get("error")
            or payload.get("message")
            or payload.get("detail")
        )
        if isinstance(detail, dict):
            detail = detail.get("message") or detail.get("reason")
        if detail:
            return _clean_text(detail, 700)
    return f"Runway вернул ошибку {response.status_code}."


def _image_data_uri(image_bytes):
    mime_type = "image/png"
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            mime_type = Image.MIME.get(image.format, mime_type)
    except OSError:
        pass
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _runway_motion_prompt(scene, index, total):
    """Задание только на естественное движение, без перерисовки героев и знака."""
    return _clean_text(
        f"""
Live-action cinematic motion for vertical scene {index} of {total}.
Scene: {scene}
Animate the existing image naturally: subtle human gestures, breathing, blinking,
realistic fabric and hair movement, gentle environmental motion and a slow stable
camera move. Preserve every person's face, age, clothing and body proportions.
Preserve the official Agency W gold emblem exactly as it appears in the source
image: shield, double W and glowing dot. Do not replace it with a plain letter W.
No morphing, no new people, no extra fingers, no captions, no letters, no logos
other than the existing Agency W emblem. Premium realistic commercial video.
""",
        1000,
    )


def _start_runway_scene(image_bytes, scene, index, total):
    secret = _runway_api_secret()
    if not secret:
        raise RuntimeError(
            "Ключ Runway не найден. В Streamlit → Settings → Secrets добавьте "
            "RUNWAYML_API_SECRET и нажмите Save."
        )
    response = requests.post(
        "https://api.dev.runwayml.com/v1/image_to_video",
        headers={
            "Authorization": f"Bearer {secret}",
            "X-Runway-Version": "2024-11-06",
            "Content-Type": "application/json",
        },
        json={
            "model": "gen4_turbo",
            "promptImage": _image_data_uri(image_bytes),
            "promptText": _runway_motion_prompt(scene, index, total),
            "ratio": "720:1280",
            "duration": 5,
        },
        timeout=120,
    )
    if response.status_code >= 400:
        raise RuntimeError(_runway_error(response))
    try:
        payload = response.json()
    except (ValueError, requests.JSONDecodeError) as exc:
        raise RuntimeError("Runway вернул непонятный ответ при запуске сцены.") from exc
    task_id = str(payload.get("id") or "").strip()
    if not task_id:
        raise RuntimeError("Runway не вернул номер задачи для сцены.")
    return task_id


def _start_runway_scenes(scene_images, scenes):
    if not scene_images:
        raise RuntimeError("Нет изображений, которые можно оживить.")
    task_ids = []
    total = len(scene_images)
    for index, image_bytes in enumerate(scene_images, start=1):
        scene = scenes[index - 1] if index <= len(scenes) else f"Сцена {index}"
        task_ids.append(_start_runway_scene(image_bytes, scene, index, total))
    return task_ids


def _runway_output_url(payload):
    output = payload.get("output") if isinstance(payload, dict) else None
    if isinstance(output, list) and output:
        first = output[0]
        if isinstance(first, str):
            return first.strip()
        if isinstance(first, dict):
            return str(first.get("url") or first.get("uri") or "").strip()
    if isinstance(output, str):
        return output.strip()
    return ""


def _check_runway_scenes(task_ids):
    secret = _runway_api_secret()
    if not secret:
        raise RuntimeError("Ключ Runway не найден в Streamlit Secrets.")
    headers = {
        "Authorization": f"Bearer {secret}",
        "X-Runway-Version": "2024-11-06",
    }
    urls = []
    pending = 0
    for task_id in task_ids:
        response = requests.get(
            f"https://api.dev.runwayml.com/v1/tasks/{task_id}",
            headers=headers,
            timeout=60,
        )
        if response.status_code >= 400:
            raise RuntimeError(_runway_error(response))
        try:
            payload = response.json()
        except (ValueError, requests.JSONDecodeError) as exc:
            raise RuntimeError("Runway вернул непонятный статус сцены.") from exc
        status = str(payload.get("status") or "").upper()
        if status in {"FAILED", "CANCELLED"}:
            reason = (
                payload.get("failure")
                or payload.get("failureCode")
                or payload.get("error")
                or "Runway не смог оживить одну из сцен."
            )
            raise RuntimeError(_clean_text(reason, 700))
        url = _runway_output_url(payload)
        if status == "SUCCEEDED" and url:
            urls.append(url)
        else:
            pending += 1
    return {
        "ready": len(urls),
        "total": len(task_ids),
        "pending": pending,
        "video_urls": urls if pending == 0 else [],
    }


def _probe_video_duration(video_path):
    ffprobe_path = shutil.which("ffprobe")
    if not ffprobe_path:
        raise RuntimeError("На сервере не найден ffprobe.")
    result = subprocess.run(
        [
            ffprobe_path,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )
    try:
        duration = float(result.stdout.decode("utf-8", errors="ignore").strip())
    except ValueError as exc:
        raise RuntimeError("Не удалось определить длительность озвучки.") from exc
    if duration <= 1:
        raise RuntimeError("Озвучка получилась слишком короткой.")
    return duration


def _ass_time(seconds):
    value = max(0.0, float(seconds))
    hours = int(value // 3600)
    minutes = int((value % 3600) // 60)
    secs = value % 60
    return f"{hours}:{minutes:02d}:{secs:05.2f}"


def _subtitle_chunks(text):
    sentences = [
        part.strip()
        for part in re.split(r"(?<=[.!?])\s+", str(text or "").strip())
        if part.strip()
    ]
    chunks = []
    for sentence in sentences:
        words = sentence.split()
        current = []
        for word in words:
            candidate = " ".join(current + [word])
            if current and (len(candidate) > 31 or len(current) >= 5):
                chunks.append(" ".join(current))
                current = [word]
            else:
                current.append(word)
        if current:
            chunks.append(" ".join(current))
    return chunks or [str(text or "").strip()]


def _normalised_words(text):
    return " ".join(re.findall(r"[\wЁёА-Яа-я]+", str(text or "").lower()))


def _wrap_caption(text, max_chars=29):
    words = str(text or "").split()
    lines = []
    current = []
    for word in words:
        candidate = " ".join(current + [word])
        if current and len(candidate) > max_chars:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))
    return "\n".join(lines[:3])


def _ass_escape(text):
    return (
        str(text or "")
        .replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("\n", r"\N")
    )


def _write_subtitles(path, spoken_text, cta, duration):
    chunks = _subtitle_chunks(spoken_text)
    cta_normalised = _normalised_words(cta)
    if cta_normalised and chunks:
        last_normalised = _normalised_words(chunks[-1])
        if last_normalised and (
            last_normalised in cta_normalised or cta_normalised in last_normalised
        ):
            chunks = chunks[:-1] or chunks
    weights = [max(4, len(chunk)) for chunk in chunks]
    available = max(1.0, duration - 0.4)
    cursor = 0.2
    events = []
    for chunk, weight in zip(chunks, weights):
        chunk_duration = available * weight / sum(weights)
        end = min(duration, cursor + chunk_duration)
        events.append(
            f"Dialogue: 0,{_ass_time(cursor)},{_ass_time(end)},Default,,0,0,0,,{_ass_escape(chunk)}"
        )
        cursor = end
    if cta:
        start = max(0.0, duration - 3.8)
        events.append(
            f"Dialogue: 1,{_ass_time(start)},{_ass_time(duration)},CTA,,0,0,0,,{_ass_escape(_wrap_caption(cta))}"
        )
    content = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Default,DejaVu Sans,48,&H00FFFFFF,&H000000FF,&H00101010,&H98000000,-1,0,0,0,100,100,0,0,3,2,0,2,90,90,170,1
Style: CTA,DejaVu Sans,50,&H0000D7FF,&H000000FF,&H00101010,&HBA000000,-1,0,0,0,100,100,0,0,3,2,0,8,90,90,135,1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
""" + "\n".join(events) + "\n"
    path.write_text(content, encoding="utf-8")


def _render_vertical_frame(image_bytes, output_path, logo_path=None):
    with Image.open(BytesIO(image_bytes)) as source:
        source = ImageOps.exif_transpose(source).convert("RGB")
        frame = ImageOps.fit(
            source,
            (1080, 1920),
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )
    if logo_path and Path(logo_path).exists():
        try:
            with Image.open(logo_path) as logo_source:
                logo = logo_source.convert("RGBA")
                logo.thumbnail((120, 120), Image.Resampling.LANCZOS)
                overlay = Image.new("RGBA", frame.size, (0, 0, 0, 0))
                overlay.alpha_composite(logo, (900, 55))
                frame = Image.alpha_composite(frame.convert("RGBA"), overlay).convert("RGB")
        except OSError:
            pass
    frame.save(output_path, "JPEG", quality=94, optimize=True)


def _assemble_ready_reel(scene_images, narration_video, spoken_text, cta):
    """Собирает 1080x1920 MP4: сцены + голос + субтитры + знак W."""
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        raise RuntimeError(
            "На сервере не найден ffmpeg. Добавьте packages.txt со строкой ffmpeg."
        )
    if not scene_images:
        raise RuntimeError("Нет изображений для сцен Reels.")

    with tempfile.TemporaryDirectory(prefix="agency_w_reel_") as temp_dir:
        temp_path = Path(temp_dir)
        narration_path = temp_path / "narration.mp4"
        narration_path.write_bytes(narration_video)
        duration = _probe_video_duration(narration_path)
        scene_duration = duration / len(scene_images)
        logo_path = Path(__file__).resolve().parent / "assets" / "agency_w_icon.png"
        segment_paths = []

        for index, image_bytes in enumerate(scene_images):
            frame_path = temp_path / f"frame_{index:02d}.jpg"
            segment_path = temp_path / f"segment_{index:02d}.mp4"
            _render_vertical_frame(image_bytes, frame_path, logo_path)
            frame_count = max(30, int(math.ceil(scene_duration * 30)))
            fade_out = max(0.0, scene_duration - 0.25)
            filter_value = (
                "zoompan="
                "z='min(zoom+0.0005,1.06)':"
                "x='iw/2-(iw/zoom/2)':"
                "y='ih/2-(ih/zoom/2)':"
                f"d={frame_count}:s=1080x1920:fps=30,"
                "fade=t=in:st=0:d=0.20,"
                f"fade=t=out:st={fade_out:.3f}:d=0.20,"
                "format=yuv420p"
            )
            result = subprocess.run(
                [
                    ffmpeg_path, "-y", "-loop", "1", "-i", str(frame_path),
                    "-t", f"{scene_duration:.3f}", "-vf", filter_value,
                    "-an", "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "21", "-pix_fmt", "yuv420p", str(segment_path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=240,
                check=False,
            )
            if result.returncode != 0 or not segment_path.exists():
                raise RuntimeError(
                    "Не удалось собрать одну из сцен: "
                    + result.stderr.decode("utf-8", errors="ignore")[-700:]
                )
            segment_paths.append(segment_path)

        concat_path = temp_path / "segments.txt"
        concat_path.write_text(
            "\n".join(f"file '{path.as_posix()}'" for path in segment_paths) + "\n",
            encoding="utf-8",
        )
        silent_path = temp_path / "silent.mp4"
        concat_result = subprocess.run(
            [
                ffmpeg_path, "-y", "-f", "concat", "-safe", "0",
                "-i", str(concat_path), "-c", "copy", str(silent_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=180,
            check=False,
        )
        if concat_result.returncode != 0 or not silent_path.exists():
            raise RuntimeError("Не удалось соединить сцены Reels.")

        subtitle_path = temp_path / "captions.ass"
        _write_subtitles(subtitle_path, spoken_text, cta, duration)
        final_path = temp_path / "ready_reel.mp4"
        final_result = subprocess.run(
            [
                ffmpeg_path, "-y", "-i", str(silent_path),
                "-i", str(narration_path),
                "-vf", f"subtitles={subtitle_path.as_posix()}",
                "-map", "0:v:0", "-map", "1:a:0", "-t", f"{duration:.3f}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", "-shortest", str(final_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=300,
            check=False,
        )
        if final_result.returncode != 0 or not final_path.exists():
            raise RuntimeError(
                "Не удалось наложить голос и субтитры: "
                + final_result.stderr.decode("utf-8", errors="ignore")[-700:]
            )
        final_bytes = final_path.read_bytes()
        if not final_bytes:
            raise RuntimeError("Готовый Reels получился пустым.")
        return final_bytes


def _download_runway_videos(video_urls):
    clips = []
    for index, video_url in enumerate(video_urls, start=1):
        response = requests.get(str(video_url or "").strip(), timeout=240)
        response.raise_for_status()
        if not response.content:
            raise RuntimeError(f"Runway вернул пустую сцену {index}.")
        clips.append(response.content)
    return clips


def _assemble_animated_reel(scene_videos, narration_video, spoken_text, cta):
    """Собирает живые сцены Runway, голос, субтитры, знак и доступную музыку."""
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        raise RuntimeError(
            "На сервере не найден ffmpeg. Добавьте packages.txt со строкой ffmpeg."
        )
    if not scene_videos:
        raise RuntimeError("Runway ещё не вернул живые сцены.")

    with tempfile.TemporaryDirectory(prefix="agency_w_live_reel_") as temp_dir:
        temp_path = Path(temp_dir)
        narration_path = temp_path / "narration.mp4"
        narration_path.write_bytes(narration_video)
        duration = _probe_video_duration(narration_path)
        scene_duration = duration / len(scene_videos)
        segment_paths = []

        for index, video_bytes in enumerate(scene_videos):
            source_path = temp_path / f"runway_{index:02d}.mp4"
            source_path.write_bytes(video_bytes)
            source_duration = _probe_video_duration(source_path)
            stretch = scene_duration / source_duration
            fade_out = max(0.0, scene_duration - 0.22)
            segment_path = temp_path / f"live_segment_{index:02d}.mp4"
            filter_value = (
                "scale=1080:1920:force_original_aspect_ratio=increase,"
                "crop=1080:1920,fps=30,"
                f"setpts={stretch:.8f}*PTS,"
                "fade=t=in:st=0:d=0.18,"
                f"fade=t=out:st={fade_out:.3f}:d=0.18,"
                "format=yuv420p"
            )
            result = subprocess.run(
                [
                    ffmpeg_path, "-y", "-i", str(source_path),
                    "-t", f"{scene_duration:.3f}", "-vf", filter_value,
                    "-an", "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "21", "-pix_fmt", "yuv420p", str(segment_path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=300,
                check=False,
            )
            if result.returncode != 0 or not segment_path.exists():
                raise RuntimeError(
                    "Не удалось подготовить живую сцену: "
                    + result.stderr.decode("utf-8", errors="ignore")[-700:]
                )
            segment_paths.append(segment_path)

        concat_path = temp_path / "live_segments.txt"
        concat_path.write_text(
            "\n".join(f"file '{path.as_posix()}'" for path in segment_paths) + "\n",
            encoding="utf-8",
        )
        silent_path = temp_path / "live_silent.mp4"
        concat_result = subprocess.run(
            [
                ffmpeg_path, "-y", "-f", "concat", "-safe", "0",
                "-i", str(concat_path), "-c", "copy", str(silent_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=180,
            check=False,
        )
        if concat_result.returncode != 0 or not silent_path.exists():
            raise RuntimeError("Не удалось соединить живые сцены Reels.")

        subtitle_path = temp_path / "captions.ass"
        _write_subtitles(subtitle_path, spoken_text, cta, duration)
        assets_dir = Path(__file__).resolve().parent / "assets"
        logo_path = assets_dir / "agency_w_icon.png"
        music_path = assets_dir / "reel_music.mp3"
        final_path = temp_path / "agency_w_live_reel.mp4"

        command = [
            ffmpeg_path, "-y", "-i", str(silent_path), "-i", str(narration_path)
        ]
        logo_index = None
        music_index = None
        next_index = 2
        if logo_path.exists():
            command.extend(["-loop", "1", "-i", str(logo_path)])
            logo_index = next_index
            next_index += 1
        if music_path.exists():
            command.extend(["-stream_loop", "-1", "-i", str(music_path)])
            music_index = next_index

        filters = []
        current_video = "0:v"
        if logo_index is not None:
            filters.extend(
                [
                    f"[{logo_index}:v]scale=120:-1[agencylogo]",
                    f"[{current_video}][agencylogo]overlay=W-w-55:55:shortest=1[withlogo]",
                ]
            )
            current_video = "withlogo"
        filters.append(
            f"[{current_video}]subtitles={subtitle_path.as_posix()}[finalvideo]"
        )

        if music_index is not None:
            filters.extend(
                [
                    "[1:a]volume=1.0[voice]",
                    f"[{music_index}:a]volume=0.09[music]",
                    "[voice][music]amix=inputs=2:duration=first:dropout_transition=2[finalaudio]",
                ]
            )

        command.extend(["-filter_complex", ";".join(filters), "-map", "[finalvideo]"])
        if music_index is not None:
            command.extend(["-map", "[finalaudio]"])
        else:
            command.extend(["-map", "1:a:0"])
        command.extend(
            [
                "-t", f"{duration:.3f}", "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac",
                "-b:a", "192k", "-movflags", "+faststart", "-shortest",
                str(final_path),
            ]
        )
        final_result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=420,
            check=False,
        )
        if final_result.returncode != 0 or not final_path.exists():
            raise RuntimeError(
                "Не удалось собрать живой Reels: "
                + final_result.stderr.decode("utf-8", errors="ignore")[-900:]
            )
        final_bytes = final_path.read_bytes()
        if not final_bytes:
            raise RuntimeError("Готовый живой Reels получился пустым.")
        return final_bytes


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


def _contains_foreign_project_markers(value):
    text = str(value or "").lower()
    return any(marker in text for marker in _AUDIENCE_FOREIGN_PROJECT_MARKERS)


def _audience_text(target_profile):
    """Даёт Контент-заводу только контекст Агентства W, без наследия других проектов."""
    safe_parts = []
    if isinstance(target_profile, dict):
        for key in (
            "portrait",
            "who_is_this",
            "current_situation",
            "goals",
            "pains",
            "dreams",
            "decision_triggers",
        ):
            value = _clean_text(target_profile.get(key), 1200)
            if not value or _contains_foreign_project_markers(value):
                continue
            safe_parts.append(value)

    context = [AGENCY_W_AUDIENCE_BASELINE]
    if safe_parts:
        context.append(
            "Дополнительный безопасный контекст о людях: "
            + " ".join(safe_parts)[:2200]
        )
    return "\n".join(context)[:3200]


def _safe_agency_w_audience(value):
    audience = _clean_text(value, 700)
    if not audience or _contains_foreign_project_markers(audience):
        return AGENCY_W_AUDIENCE_BASELINE
    return audience


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
Поле script содержит ТОЛЬКО слова диктора. В нём запрещены номера сцен,
ремарки, скобки и указания «в кадре», «на экране», «камера», «переход».
Технические описания записывай исключительно в массив scenes.
Не обещай доход и не используй давление.

Пост: самостоятельная полезная мысль, живой текст, без канцелярита.

Карусель: сам выбери от 6 до 8 слайдов — обложка, развитие одной истории
и финальный CTA. Заголовок обложки — максимум 9 слов. Каждый следующий
слайд должен быть понятен человеку, который впервые слышит об Агентстве W.
Один слайд — одна конкретная мысль. Запрещены вода, канцелярит,
непояснённые термины, англицизмы и смешение разных проектов.

Агентство W — цифровая команда и рабочая структура вокруг предпринимателя.
Не называй его закрытым клубом, лагерем, NFT-/реферальной экосистемой,
крипто- или инвестиционным проектом.

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
      "script": "только произносимая речь диктора — без описания кадров",
      "scenes": ["техническое описание сцены 1", "техническое описание сцены 2"],
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


FORMAT_LABELS = {
    "post": "Пост",
    "carousel": "Карусель",
    "reel": "Reels",
}


def _transcribe_content_audio(audio_bytes, filename="content-request.wav"):
    """Распознаёт простое голосовое задание и не отправляет его повторно."""
    api_key = _clean_text(st.secrets.get("OPENAI_API_KEY"), 5000)
    if not api_key:
        raise RuntimeError("Ключ OpenAI не найден в настройках приложения.")

    audio_hash = hashlib.sha256(audio_bytes).hexdigest()
    cache_key = f"content_factory_voice_transcript_{audio_hash}"
    cached = st.session_state.get(cache_key)
    if cached:
        return str(cached)

    response = requests.post(
        "https://api.openai.com/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {api_key}"},
        data={"model": "gpt-4o-mini-transcribe", "language": "ru"},
        files={"file": (filename, audio_bytes, "audio/wav")},
        timeout=120,
    )
    response.raise_for_status()
    transcript = _clean_text(response.json().get("text"), 7000)
    st.session_state[cache_key] = transcript
    return transcript


def _analyse_content_request(content_format, request_text, target_profile, ask_ai_fn):
    """Превращает разговорное задание в короткое и проверяемое понимание."""
    audience_context = _audience_text(target_profile)
    format_label = FORMAT_LABELS.get(content_format, "Пост")
    prompt = f"""
Человек выбрал формат: {format_label}.
Он объяснил задачу своими словами:
{request_text}

КОНТЕКСТ АУДИТОРИИ АГЕНТСТВА W:
{audience_context}

Пойми замысел человека, даже если он говорил разговорно, с повторами или
непрофессиональными словами. Не меняй выбранный формат и не придумывай факты.

ВАЖНО ПРО ПОЛЕ «audience»:
- опиши только самих людей и их рабочую ситуацию;
- Агентство W создаёт цифровую команду и структуру вокруг предпринимателя;
- не перечисляй в этом поле функции продукта и не добавляй чужие предложения;
- запрещены: AI-поддержка 24/7 как оффер, закрытый клуб/лагерь, Lodge, NFT,
  реферальная монетизация, криптовалюта, инвестиции и «привилегии»;
- если исходная задача уже ясно говорит, для кого материал, следуй ей в первую очередь.

Верни только JSON:
{{
  "topic": "о чём материал — одним предложением",
  "main_idea": "главная мысль, которую должен понять человек",
  "audience": "кому адресован материал",
  "desired_result": "какое изменение должно произойти после просмотра",
  "cta": "одно простое и измеримое действие читателя или зрителя"
}}
""".strip()
    system = (
        "Ты — Стагирит, координатор Контент-завода Агентства W. "
        "Ты переводишь обычную человеческую речь в ясную редакционную задачу. "
        "Пиши конкретно и понятно. Верни только корректный JSON."
    )
    data = _json_from_answer(ask_ai_fn(system, prompt))
    result = {
        "format": content_format,
        "topic": _clean_text(data.get("topic"), 500),
        "main_idea": _clean_text(data.get("main_idea"), 900),
        "audience": _safe_agency_w_audience(data.get("audience")),
        "desired_result": _clean_text(data.get("desired_result"), 800),
        "cta": _clean_text(data.get("cta"), 500),
        "original_request": _clean_text(request_text, 7000),
    }
    if not result["topic"] or not result["main_idea"]:
        raise ValueError("Не удалось выделить главную мысль. Скажите задачу ещё раз.")
    if not result["audience"]:
        result["audience"] = AGENCY_W_AUDIENCE_BASELINE
    if not result["desired_result"]:
        result["desired_result"] = "Человек понимает пользу и хочет узнать больше"
    if not result["cta"]:
        result["cta"] = "Написать в комментарии, какая задача забирает больше всего времени"
    return result


def _single_content_prompt(understanding):
    content_format = understanding["format"]
    format_label = FORMAT_LABELS.get(content_format, "Пост")
    common = f"""
Создай один законченный материал для Instagram Агентства W.

ВЫБРАННЫЙ ЧЕЛОВЕКОМ ФОРМАТ: {format_label}
ИСХОДНОЕ ЗАДАНИЕ: {understanding.get('original_request') or ''}
ТЕМА: {understanding.get('topic') or ''}
ГЛАВНАЯ МЫСЛЬ: {understanding.get('main_idea') or ''}
ДЛЯ КОГО: {understanding.get('audience') or ''}
ЖЕЛАЕМЫЙ РЕЗУЛЬТАТ: {understanding.get('desired_result') or ''}
ДЕЙСТВИЕ ЧЕЛОВЕКА: {understanding.get('cta') or ''}

Требования ко всем форматам:
- сильный, честный заголовок, на котором останавливается взгляд;
- одна ясная смысловая линия без воды, канцелярита и пустых обещаний;
- простой русский язык, понятный человеку без знаний маркетинга и ИИ;
- конкретная польза вместо общих слов;
- не обещай доход и не выдумывай возможности Агентства W;
- Агентство W — цифровая команда и структура, а не клуб, лагерь, NFT-/реферальная
  экосистема, крипто- или инвестиционный проект;
- не переноси в материал предложения и терминологию других проектов;
- призыв должен быть один, естественный и измеримый;
- если владелец в исходном задании дал точную формулировку CTA, сохрани её по смыслу
  и не подменяй другим действием;
- если владелец задал тон (например, добрый юмор, самоиронию, спокойствие), этот тон
  обязателен во всём материале;
- Контент-завод сам строит структуру материала; владелец не обязан перечислять слайды;
- если нужны герои, используй только: Стагирит, Неония, Неона, Тео, Неола;
- Разведчик не является публичным героем.

Верни только JSON с объектом item по схеме:
{{
  "item": {{
    "id": "content_1",
    "format": "{content_format}",
    "title": "название",
    "goal": "задача материала",
    "hook": "хук для Reels или пустая строка",
    "script": "только произносимая речь Reels или пустая строка",
    "scenes": ["отдельные визуальные сцены только для Reels"],
    "post_text": "готовый текст поста или пустая строка",
    "slides": ["готовые тексты слайдов только для карусели"],
    "caption": "готовая подпись для публикации",
    "cta": "одно действие",
    "visual_brief": "профессиональное задание Художнику без текста на изображении",
    "professionals": ["имена только нужных профессионалов"]
  }}
}}
""".strip()

    if content_format == "post":
        return common + """

ДЛЯ ПОСТА:
- post_text — полностью готовый текст публикации на 700–1300 знаков;
- первая строка цепляет узнаваемой ситуацией, а не дешёвым кликбейтом;
- текст раскрывает одну мысль, даёт полезный вывод и заканчивается CTA;
- caption не повторяет весь пост: это короткий вариант подписи до 350 знаков;
- visual_brief описывает одну сильную понятную обложку без надписей.
"""
    if content_format == "carousel":
        return common + """

ДЛЯ КАРУСЕЛИ:
- сам выбери от 6 до 8 слайдов в зависимости от истории;
- слайд 1 — обложка, максимум 9 слов;
- следующие слайды должны ощущаться как одна маленькая история, а не как набор
  одинаковых рекламных тезисов;
- если задача построена на узнаваемой бытовой ситуации, сначала покажи её развитие,
  затем поворот к решению и только потом Агентство W;
- финальный слайд показывает главный вывод и содержит ровно один CTA;
- один слайд — одна завершённая мысль, 35–170 знаков;
- юмор и самоирония должны рождаться из ситуации, а не из шуток ради шутки;
- caption — самостоятельная полезная подпись на 450–750 знаков;
- visual_brief должен описывать визуальную драматургию серии, включая правило:
  виртуальный офис W всегда один и узнаваемый; природные и реальные локации
  остаются естественными и правдоподобными;
- никаких слов «лагерь», Lodge, «видите вклад», NFT и непонятных сокращений.
"""
    return common + """

ДЛЯ REELS:
- длительность 25–45 секунд;
- hook цепляет в первые две секунды;
- script содержит 65–105 слов живой речи диктора;
- script не содержит слов «сцена», «кадр», «камера», описаний изображения,
  номеров сцен и ремарок в скобках;
- scenes содержит 4–6 коротких визуальных сцен без текста диктора;
- caption дополняет ролик, а не пересказывает его;
- последняя фраза речи естественно ведёт к CTA.
"""


def _single_item_errors(item):
    """Не пропускает бессмыслицу к платным изображениям и видео."""
    errors = []
    content_format = item.get("format")
    if not _clean_text(item.get("title"), 500):
        errors.append("Нет заголовка.")
    if not _clean_text(item.get("cta"), 500):
        errors.append("Нет одного понятного действия для человека.")

    if content_format == "post":
        text = _clean_text(item.get("post_text"), 8000)
        if len(text) < 350:
            errors.append("Текст поста слишком короткий и не раскрывает мысль.")
    elif content_format == "carousel":
        errors.extend(_carousel_copy_errors({"slides": item.get("slides") or []}))
        if not _clean_text(item.get("caption"), 4000):
            errors.append("Нет подписи к карусели.")
    elif content_format == "reel":
        script = _clean_text(item.get("script"), 7000)
        word_count = len(re.findall(r"\b[\wЁёА-Яа-я-]+\b", script))
        if word_count < 45:
            errors.append("Речь для Reels слишком короткая.")
        if _has_direction_markers(script):
            errors.append("В речь диктора попали описания сцен или камеры.")
        scenes = [value for value in (item.get("scenes") or []) if _clean_text(value)]
        if len(scenes) < 4:
            errors.append("Для Reels нужно не меньше четырёх визуальных сцен.")
    else:
        errors.append("Неизвестный формат материала.")
    return errors


def _generate_single_package(owner_id, understanding, ask_ai_fn):
    """Создаёт один материал и сам повторяет запрос при повреждённом JSON."""
    prompt = _single_content_prompt(understanding)
    last_errors = []

    for attempt in range(1, 4):
        try:
            answer = ask_ai_fn(FACTORY_SYSTEM_PROMPT, prompt)
            data = _json_from_answer(answer)
        except (ValueError, TypeError, json.JSONDecodeError):
            # Технический JSON-брак не должен попадать на экран пользователю.
            # Просим модель собрать ответ заново и продолжаем в пределах тех же
            # трёх бесплатных по отношению к изображениям/видео текстовых попыток.
            last_errors = ["Ответ ИИ пришёл в повреждённом JSON-формате."]
            if attempt < 3:
                prompt += (
                    "\n\nТехническая проверка не смогла прочитать предыдущий JSON. "
                    "Сформируй ответ ЗАНОВО по исходной схеме. Не копируй "
                    "повреждённый JSON. Проверь все кавычки, запятые, фигурные "
                    "и квадратные скобки. Верни ТОЛЬКО один корректный JSON-объект "
                    "без Markdown и без пояснений."
                )
                continue
            break

        item = _normalise_item(data.get("item") or data, 0)
        item["format"] = understanding["format"]
        last_errors = _single_item_errors(item)
        if not last_errors:
            now = datetime.now(timezone.utc).isoformat()
            return {
                "package_id": str(uuid.uuid4()),
                "owner_telegram_id": int(owner_id),
                "created_at": now,
                "updated_at": now,
                "status": "draft",
                "workflow_version": "single_v2",
                "week_title": item.get("title") or "Новый материал",
                "strategy": understanding.get("main_idea") or "",
                "understanding": understanding,
                "settings": {
                    "project_name": "Агентство W",
                    "offer": "Пять ИИ-профессионалов возвращают предпринимателю время",
                    "audience": understanding.get("audience") or "",
                    "goal": understanding.get("desired_result") or "",
                    "key_message": understanding.get("main_idea") or "",
                },
                "items": [item],
            }

        if attempt < 3:
            prompt += (
                "\n\nПредыдущий материал забракован до платного производства. "
                "Исправь все ошибки и верни полный JSON заново:\n- "
                + "\n- ".join(last_errors)
            )

    raise ValueError(
        "Материал не удалось подготовить автоматически после нескольких попыток. "
        "Нажмите «Всё верно — подготовить материал» ещё раз. "
        "Платные изображения и видео не запускались."
    )


def _stagirite_review(package, item, ask_ai_fn):
    """Стагирит исправляет материал и допускает его к производству."""
    prompt = f"""
Проведи окончательную редакторскую проверку материала Агентства W.

Подтверждённая задача:
{json.dumps(package.get('understanding') or {{}}, ensure_ascii=False)}

Материал:
{json.dumps(item, ensure_ascii=False)}

Проверь: соответствие задаче, ясность для незнакомого человека, сильный
заголовок, логику, фактическую осторожность, отсутствие воды и один CTA.
Сам исправь все найденные недостатки. Не меняй выбранный человеком формат.

Верни только JSON:
{{
  "summary": "одним предложением — почему материал готов",
  "item": {{полный исправленный объект материала по исходной схеме}}
}}
""".strip()
    system = (
        "Ты — Стагирит, координатор и финальный контролёр Контент-завода W. "
        "Ты не пропускаешь красивую бессмыслицу и не перекладываешь исправления "
        "на владельца. Верни только корректный JSON."
    )
    last_errors = []
    for _ in range(3):
        data = _json_from_answer(ask_ai_fn(system, prompt))
        reviewed = _normalise_item(data.get("item") or {}, 0)
        reviewed["format"] = item.get("format")
        reviewed["professionals"] = [
            name
            for name in (
                reviewed.get("professionals") or item.get("professionals") or []
            )
            if name in PROFESSIONAL_PORTRAITS
        ]
        last_errors = _single_item_errors(reviewed)
        if not last_errors:
            reviewed["status"] = "approved"
            reviewed["review_summary"] = _clean_text(
                data.get("summary"), 700
            ) or "Смысл, структура и призыв проверены Стагиритом."
            reviewed["revision"] = int(item.get("revision") or 0) + 1
            return reviewed
        prompt += (
            "\n\nПроверка всё ещё нашла ошибки. Исправь их и верни весь объект:\n- "
            + "\n- ".join(last_errors)
        )
    raise ValueError("Стагирит не утвердил материал: " + "; ".join(last_errors))


def _item_as_text(item):
    parts = [item.get("title") or "Материал"]
    if item.get("hook"):
        parts.extend(["", "Хук:", item["hook"]])
    if item.get("script"):
        parts.extend(["", "Текст речи:", item["script"]])
    if item.get("scenes"):
        parts.extend(["", "Сцены:", "\n".join(
            f"{index}. {value}" for index, value in enumerate(item["scenes"], 1)
        )])
    if item.get("post_text"):
        parts.extend(["", "Текст публикации:", item["post_text"]])
    if item.get("slides"):
        parts.extend(["", "Слайды:", "\n\n".join(
            f"Слайд {index}\n{value}" for index, value in enumerate(item["slides"], 1)
        )])
    if item.get("caption"):
        parts.extend(["", "Подпись:", item["caption"]])
    if item.get("cta"):
        parts.extend(["", "Призыв:", item["cta"]])
    return "\n".join(parts).strip()


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


def _render_legacy_item(
    package,
    item,
    owner_id,
    ask_ai_fn,
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
        pending_copy_key = prefix + "_pending_carousel_copy"
        pending_copy = st.session_state.pop(pending_copy_key, None)
        if isinstance(pending_copy, dict) and pending_copy.get("ok"):
            item["title"] = pending_copy.get("title") or item.get("title", "")
            item["slides"] = pending_copy.get("slides") or []
            item["caption"] = pending_copy.get("caption") or ""
            item["cta"] = pending_copy.get("cta") or ""
            # Ключи виджетов меняются до их создания — Streamlit покажет новый текст.
            st.session_state[prefix + "_title"] = item["title"]
            st.session_state[prefix + "_slides"] = "\n\n".join(item["slides"])
            st.session_state[prefix + "_caption"] = item["caption"]
            st.session_state[prefix + "_cta"] = item["cta"]
            package["updated_at"] = datetime.now(timezone.utc).isoformat()
            _save_package(package)

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

        if item.get("format") == "carousel":
            carousel_state_key = prefix + "_ready_carousel_result"
            copy_state_key = prefix + "_carousel_copy_result"
            st.caption(
                "Сначала главный редактор перепишет и проверит смысл всей карусели. "
                "На этом этапе платные изображения не создаются."
            )
            if image_col.button(
                "✍️ Подготовить сильный текст",
                key=prefix + "_carousel_copy",
                use_container_width=True,
            ):
                with st.spinner("Главный редактор создаёт и проверяет 6 связанных слайдов..."):
                    copy_result = _prepare_carousel_copy(package, item, ask_ai_fn)
                st.session_state[copy_state_key] = copy_result
                if copy_result.get("ok"):
                    st.session_state[pending_copy_key] = copy_result
                    st.session_state.pop(carousel_state_key, None)
                    st.rerun()
                else:
                    st.error(str(copy_result.get("error") or "Текст не прошёл проверку."))

            copy_result = st.session_state.get(copy_state_key)
            if isinstance(copy_result, dict) and copy_result.get("ok"):
                st.success("✅ Текст прошёл автоматическую проверку смысла и ясности.")
                st.info(
                    "Прочитайте шесть тезисов в поле «Слайды» выше. Только если "
                    "они вам понятны и нравятся, запускайте платные изображения."
                )
                current_copy = {"slides": item.get("slides") or []}
                current_errors = _carousel_copy_errors(current_copy)
                if current_errors:
                    st.error(
                        "Платная генерация заблокирована:\n- "
                        + "\n- ".join(current_errors)
                    )
                st.warning(
                    "Следующая кнопка использует платный Художник OpenAI: одно "
                    "изображение на каждый слайд. Нажимайте её только после проверки текста."
                )
                if st.button(
                    f"🎨 Текст подходит — создать {len(item.get('slides') or [])} слайдов",
                    key=prefix + "_carousel_images",
                    disabled=bool(current_errors),
                    use_container_width=True,
                ):
                    with st.spinner(
                        f"Художник создаёт {len(item.get('slides') or [])} отдельных слайдов..."
                    ):
                        carousel_result = _generate_ready_carousel(
                            item, generate_illustration_fn
                        )
                    st.session_state[carousel_state_key] = carousel_result
            else:
                st.info(
                    "Платный Художник заблокирован. Сначала нажмите "
                    "«Подготовить сильный текст»."
                )

            carousel_result = st.session_state.get(carousel_state_key)
            if isinstance(carousel_result, dict):
                if carousel_result.get("ok") and carousel_result.get("slides"):
                    st.success(
                        f"✅ Готовая карусель: {len(carousel_result['slides'])} слайдов."
                    )
                    preview_columns = st.columns(2)
                    for slide_index, slide_bytes in enumerate(
                        carousel_result["slides"], start=1
                    ):
                        column = preview_columns[(slide_index - 1) % 2]
                        with column:
                            st.image(slide_bytes, caption=f"Слайд {slide_index}")
                            st.download_button(
                                f"⬇️ Скачать слайд {slide_index}",
                                data=slide_bytes,
                                file_name=f"{slide_index:02d}_carousel_slide.png",
                                mime="image/png",
                                key=prefix + f"_carousel_slide_{slide_index}",
                                use_container_width=True,
                            )
                    st.download_button(
                        "⬇️ Скачать всю карусель ZIP",
                        data=carousel_result["zip_bytes"],
                        file_name=f"{item_id}_ready_carousel.zip",
                        mime="application/zip",
                        key=prefix + "_carousel_zip",
                        use_container_width=True,
                    )
                    st.success(
                        "В ZIP слайды уже пронумерованы в правильном порядке "
                        "для публикации в Instagram."
                    )
                elif carousel_result.get("error"):
                    st.error(str(carousel_result["error"]))
        else:
            image_state_key = prefix + "_image_result"
            if image_col.button(
                "🎨 Создать изображение",
                key=prefix + "_image",
                use_container_width=True,
            ):
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
            st.markdown("#### 🎬 Готовый Reels")
            st.caption(
                "Контент-завод сам отделяет речь от режиссёрского плана, создаёт "
                "сцены, получает голос Неоны, добавляет субтитры и собирает MP4."
            )
            st.warning(
                "Создание использует платные сервисы: до 6 изображений OpenAI и "
                "одно видео HeyGen. Нажимайте один раз и дождитесь результата."
            )

            reel_state_key = prefix + "_ready_reel_state"
            reel_state = st.session_state.get(reel_state_key, {})
            reel_state = reel_state if isinstance(reel_state, dict) else {}
            video_id = str(reel_state.get("video_id") or "").strip()
            final_bytes = reel_state.get("final_video")
            start_col, check_col = st.columns(2)

            if start_col.button(
                "🏭 Подготовить готовый Reels",
                key=prefix + "_ready_reel_start",
                disabled=bool(video_id) and not bool(final_bytes),
                use_container_width=True,
            ):
                with st.spinner("Редактор отделяет речь диктора от плана кадров..."):
                    prepared = _prepare_reel_content(item, ask_ai_fn)
                if not prepared.get("ok"):
                    st.error(str(prepared.get("error") or "Не удалось подготовить речь."))
                else:
                    spoken_text = prepared["spoken_text"]
                    prepared_scenes = prepared["scenes"]
                    with st.spinner(
                        f"Художник создаёт {len(prepared_scenes)} сцен. "
                        "Это может занять несколько минут..."
                    ):
                        scene_result = _generate_reel_scenes(
                            item,
                            prepared_scenes,
                            spoken_text,
                            generate_illustration_fn,
                        )
                    if not scene_result.get("ok"):
                        st.error(str(scene_result.get("error") or "Не удалось создать сцены."))
                    else:
                        reel_state = {
                            "spoken_text": spoken_text,
                            "scenes": prepared_scenes,
                            "scene_images": scene_result["images"],
                            "status": "scenes_ready",
                        }
                        st.session_state[reel_state_key] = reel_state
                        with st.spinner("Неона записывает только чистую речь диктора..."):
                            avatar_result = create_avatar_video_fn(spoken_text)
                        if avatar_result.get("ok"):
                            reel_state.update(avatar_result)
                            st.session_state[reel_state_key] = reel_state
                            st.rerun()
                        else:
                            reel_state["status"] = "voice_error"
                            reel_state["error"] = str(
                                avatar_result.get("error") or "Не удалось запустить голос."
                            )
                            st.session_state[reel_state_key] = reel_state
                            st.error(reel_state["error"])

            if check_col.button(
                "🔄 Проверить и собрать",
                key=prefix + "_ready_reel_check",
                disabled=not bool(video_id) or bool(final_bytes),
                use_container_width=True,
            ):
                with st.spinner("Проверяем голос Неоны..."):
                    result = get_avatar_video_fn(video_id)
                if not result.get("ok"):
                    st.error(str(result.get("error") or "Не удалось проверить голос."))
                else:
                    reel_state.update(result)
                    if str(result.get("status") or "").lower() == "completed" and result.get("video_url"):
                        try:
                            with st.spinner(
                                "Голос готов. Собираем сцены, субтитры и знак W в один MP4..."
                            ):
                                narration_video = _download_video_bytes(result["video_url"])
                                reel_state["final_video"] = _assemble_ready_reel(
                                    reel_state.get("scene_images") or [],
                                    narration_video,
                                    reel_state.get("spoken_text") or "",
                                    item.get("cta") or "",
                                )
                                reel_state["status"] = "ready"
                            st.success("✅ Готовый Reels собран.")
                        except (requests.exceptions.RequestException, RuntimeError, OSError) as exc:
                            reel_state["status"] = "assembly_error"
                            reel_state["error"] = str(exc)
                            st.error("Не удалось собрать Reels: " + str(exc))
                    elif str(result.get("status") or "").lower() == "failed":
                        st.error(str(result.get("error") or "HeyGen не смог записать голос."))
                    else:
                        st.info(
                            "Неона ещё записывает голос. Подождите немного и снова "
                            "нажмите «Проверить и собрать»."
                        )
                    st.session_state[reel_state_key] = reel_state

            if reel_state.get("spoken_text"):
                with st.expander("🗣 Текст, который произносит Неона", expanded=False):
                    st.write(reel_state["spoken_text"])

            final_bytes = reel_state.get("final_video")
            if final_bytes:
                st.video(final_bytes)
                st.download_button(
                    "⬇️ Скачать готовый Reels MP4",
                    data=final_bytes,
                    file_name=f"{item_id}_ready_reel.mp4",
                    mime="video/mp4",
                    key=prefix + "_ready_reel_download",
                    use_container_width=True,
                )
                st.success("Этот MP4 уже можно публиковать в Instagram Reels.")
            elif video_id:
                st.info(
                    "Сцены сохранены в текущем сеансе. Когда голос будет готов, "
                    "нажмите «Проверить и собрать»."
                )


def _render_legacy_content_factory(
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
            ask_ai_fn,
            generate_illustration_fn,
            create_avatar_video_fn,
            get_avatar_video_fn,
        )

    st.divider()
    st.caption(
        "Готовый Reels собирается автоматически из сцен, чистой речи Неоны, "
        "субтитров и фирменного знака. Публикация в Instagram остаётся только "
        "после вашего утверждения."
    )


def _render_item(
    package,
    item,
    owner_id,
    ask_ai_fn,
    generate_illustration_fn,
    create_avatar_video_fn,
    get_avatar_video_fn,
):
    """Показывает один материал: текст → Стагирит → платное производство."""
    package_id = package["package_id"]
    item_id = item.get("id") or "content_1"
    prefix = f"cf2_{owner_id}_{package_id}_{item_id}"
    revision = int(item.get("revision") or 0)
    widget_prefix = f"{prefix}_r{revision}"
    content_format = item.get("format") or "post"
    format_label = FORMAT_LABELS.get(content_format, "Пост")
    approved = item.get("status") == "approved"

    st.markdown(f"### {format_label}: {item.get('title') or 'Новый материал'}")

    if not approved:
        st.info(
            "Это текстовый черновик. Исправьте любые слова, если хотите. "
            "Платные изображения и видео пока не создаются."
        )
        item["title"] = st.text_input(
            "Заголовок",
            value=item.get("title") or "",
            key=widget_prefix + "_title",
        )
        item["goal"] = st.text_area(
            "Задача материала",
            value=item.get("goal") or "",
            height=80,
            key=widget_prefix + "_goal",
        )

        if content_format == "post":
            item["post_text"] = st.text_area(
                "Готовый текст публикации",
                value=item.get("post_text") or "",
                height=300,
                key=widget_prefix + "_post",
            )
        elif content_format == "carousel":
            slides_text = "\n\n".join(item.get("slides") or [])
            slides_text = st.text_area(
                "Слайды карусели — разделены пустой строкой",
                value=slides_text,
                height=330,
                key=widget_prefix + "_slides",
            )
            item["slides"] = [
                value.strip() for value in slides_text.split("\n\n") if value.strip()
            ]
        else:
            item["hook"] = st.text_area(
                "Первые две секунды — сильный хук",
                value=item.get("hook") or "",
                height=80,
                key=widget_prefix + "_hook",
            )
            item["script"] = st.text_area(
                "Только слова, которые произносит Неона",
                value=item.get("script") or "",
                height=230,
                key=widget_prefix + "_script",
            )
            scenes_text = "\n".join(item.get("scenes") or [])
            scenes_text = st.text_area(
                "Визуальные сцены — одна с новой строки",
                value=scenes_text,
                height=170,
                key=widget_prefix + "_scenes",
            )
            item["scenes"] = [
                value.strip() for value in scenes_text.splitlines() if value.strip()
            ]

        item["caption"] = st.text_area(
            "Подпись к публикации",
            value=item.get("caption") or "",
            height=150,
            key=widget_prefix + "_caption",
        )
        item["cta"] = st.text_area(
            "Одно действие для читателя или зрителя",
            value=item.get("cta") or "",
            height=80,
            key=widget_prefix + "_cta",
        )
        item["visual_brief"] = st.text_area(
            "Задание для изображения или видеосцен",
            value=item.get("visual_brief") or "",
            height=110,
            key=widget_prefix + "_visual",
        )
        selected_professionals = [
            name
            for name in (item.get("professionals") or _guess_professionals(item))
            if name in PROFESSIONAL_PORTRAITS
        ]
        item["professionals"] = st.multiselect(
            "Кого из пяти профессионалов показать",
            options=list(PROFESSIONAL_PORTRAITS),
            default=selected_professionals,
            help="Будут использованы только официальные портреты Агентства W.",
            key=widget_prefix + "_professionals",
        )

        st.caption(
            "Следующий шаг проверяет смысл и качество. Платные изображения и видео "
            "останутся заблокированы."
        )
        if st.button(
            "✅ Стагирит: проверить и утвердить",
            key=prefix + "_review",
            type="primary",
            use_container_width=True,
        ):
            errors = _single_item_errors(item)
            if errors:
                st.warning(
                    "Стагирит сначала исправит замечания:\n- " + "\n- ".join(errors)
                )
            try:
                with st.spinner("Стагирит проверяет смысл, заголовок и призыв..."):
                    reviewed = _stagirite_review(package, item, ask_ai_fn)
                item.clear()
                item.update(reviewed)
                package["status"] = "approved"
                package["week_title"] = item.get("title") or package.get("week_title")
                package["updated_at"] = datetime.now(timezone.utc).isoformat()
                _save_package(package)
                st.rerun()
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                # Не показываем человеку внутренние JSON-ошибки вроде
                # "Expecting ',' delimiter: line ...". Это кухня Контент-завода.
                message = str(exc).strip()
                if (
                    isinstance(exc, json.JSONDecodeError)
                    or "delimiter" in message.lower()
                    or "json" in message.lower() and "формат" not in message.lower()
                ):
                    message = (
                        "Материал не удалось подготовить с этой попытки. "
                        "Попробуйте ещё раз — Контент-завод сам пересоберёт ответ."
                    )
                st.error(message or "Материал не удалось подготовить. Попробуйте ещё раз.")
        return

    st.success(
        "✅ Стагирит утвердил материал. "
        + str(item.get("review_summary") or "Смысл и качество проверены.")
    )
    with st.container(border=True):
        st.markdown(f"**{item.get('title') or 'Без названия'}**")
        if content_format == "post":
            st.write(item.get("post_text") or "")
        elif content_format == "carousel":
            for index, slide in enumerate(item.get("slides") or [], start=1):
                st.markdown(f"**Слайд {index}**")
                st.write(slide)
        else:
            st.markdown("**Хук**")
            st.write(item.get("hook") or "")
            st.markdown("**Речь Неоны**")
            st.write(item.get("script") or "")
            with st.expander("Посмотреть план видеосцен"):
                for index, scene in enumerate(item.get("scenes") or [], start=1):
                    st.write(f"{index}. {scene}")
        if item.get("caption"):
            st.markdown("**Подпись к публикации**")
            st.write(item["caption"])
        st.markdown("**Действие человека**")
        st.write(item.get("cta") or "")

    edit_col, download_col = st.columns(2)
    if edit_col.button(
        "✏️ Исправить текст",
        key=prefix + "_edit",
        use_container_width=True,
    ):
        item["status"] = "draft"
        item["revision"] = revision + 1
        package["status"] = "draft"
        package["updated_at"] = datetime.now(timezone.utc).isoformat()
        _save_package(package)
        st.rerun()
    download_col.download_button(
        "⬇️ Скачать текст",
        data=(_item_as_text(item) + "\n").encode("utf-8"),
        file_name=f"{content_format}_agency_w.txt",
        mime="text/plain",
        key=prefix + "_text_download",
        use_container_width=True,
    )

    st.markdown("### Производство готового материала")
    if content_format == "post":
        image_state_key = prefix + "_image_result"
        st.warning(
            "Следующая кнопка использует платного Художника OpenAI. "
            "Нажмите её один раз."
        )
        if st.button(
            "🎨 Создать готовое изображение",
            key=prefix + "_make_image",
            use_container_width=True,
        ):
            references = _portrait_references(item.get("professionals") or [])
            available = {
                value.get("name")
                for value in references
                if value.get("kind") == "portrait"
            }
            missing = [
                name for name in (item.get("professionals") or []) if name not in available
            ]
            if missing:
                st.error("Не найдены официальные портреты: " + ", ".join(missing))
            else:
                with st.spinner("Художник создаёт изображение..."):
                    result = generate_illustration_fn(
                        _item_source_text(item),
                        change_request=item.get("visual_brief") or "",
                        size="1024x1536",
                        reference_images=references,
                    )
                st.session_state[image_state_key] = result

        image_result = st.session_state.get(image_state_key)
        if isinstance(image_result, dict):
            if image_result.get("ok") and image_result.get("image_bytes"):
                st.image(image_result["image_bytes"], caption="Готовое изображение")
                st.download_button(
                    "⬇️ Скачать изображение PNG",
                    data=image_result["image_bytes"],
                    file_name="agency_w_post.png",
                    mime="image/png",
                    key=prefix + "_image_download",
                    use_container_width=True,
                )
            elif image_result.get("error"):
                st.error(str(image_result["error"]))

    elif content_format == "carousel":
        carousel_state_key = prefix + "_carousel_result"
        current_errors = _carousel_copy_errors({"slides": item.get("slides") or []})
        slide_count = len(item.get("slides") or [])
        st.warning(
            f"Следующая кнопка создаст {slide_count} платных изображений — по одному на слайд."
        )
        if st.button(
            "🖼️ Создать готовую карусель",
            key=prefix + "_make_carousel",
            disabled=bool(current_errors),
            use_container_width=True,
        ):
            with st.spinner(
                f"Художник создаёт {slide_count} связанных слайдов..."
            ):
                result = _generate_ready_carousel(item, generate_illustration_fn)
            st.session_state[carousel_state_key] = result

        carousel_result = st.session_state.get(carousel_state_key)
        if isinstance(carousel_result, dict):
            if carousel_result.get("ok") and carousel_result.get("slides"):
                preview_columns = st.columns(2)
                for index, slide_bytes in enumerate(carousel_result["slides"], start=1):
                    with preview_columns[(index - 1) % 2]:
                        st.image(slide_bytes, caption=f"Слайд {index}")
                st.download_button(
                    "⬇️ Скачать готовую карусель ZIP",
                    data=carousel_result["zip_bytes"],
                    file_name="agency_w_carousel.zip",
                    mime="application/zip",
                    key=prefix + "_carousel_download",
                    use_container_width=True,
                )
                st.success("Слайды уже пронумерованы в порядке публикации.")
            elif carousel_result.get("error"):
                st.error(str(carousel_result["error"]))

    else:
        reel_state_key = prefix + "_reel_state"
        reel_state = st.session_state.get(reel_state_key)
        reel_state = reel_state if isinstance(reel_state, dict) else {}
        video_id = _clean_text(reel_state.get("video_id"), 300)
        final_video = reel_state.get("final_video")
        scene_images = reel_state.get("scene_images") or []
        runway_available = bool(_runway_api_secret())

        # Если сцены уже созданы, не запускаем их платную генерацию повторно
        # только из-за проблемы с HeyGen.
        if not video_id and not final_video and not scene_images:
            st.warning(
                "Следующая кнопка использует изображения OpenAI, голос HeyGen и "
                "оживление сцен Runway. Нажмите её один раз."
            )
            if runway_available:
                st.success("Runway подключён. Сцены будут действительно двигаться.")
            else:
                st.error(
                    "Runway пока не подключён. Сохраните RUNWAYML_API_SECRET в "
                    "Streamlit Secrets, иначе живой ролик создать нельзя."
                )
            if st.button(
                "🎬 Создать живой Reels",
                key=prefix + "_make_reel",
                disabled=not runway_available,
                use_container_width=True,
            ):
                with st.spinner("Художник создаёт видеосцены..."):
                    scene_result = _generate_reel_scenes(
                        item,
                        item.get("scenes") or [],
                        item.get("script") or "",
                        generate_illustration_fn,
                    )
                if not scene_result.get("ok"):
                    st.error(str(scene_result.get("error") or "Не созданы сцены."))
                else:
                    reel_state = {
                        "spoken_text": item.get("script") or "",
                        "scenes": item.get("scenes") or [],
                        "scene_images": scene_result["images"],
                        "status": "scenes_ready",
                    }
                    with st.spinner("Неона записывает утверждённый текст..."):
                        avatar_result = create_avatar_video_fn(item.get("script") or "")
                    if avatar_result.get("ok"):
                        reel_state.update(avatar_result)
                    else:
                        reel_state["status"] = "voice_error"
                        reel_state["error"] = str(
                            avatar_result.get("error") or "Не удалось запустить озвучку."
                        )
                    st.session_state[reel_state_key] = reel_state
                    st.rerun()

        video_id = _clean_text(reel_state.get("video_id"), 300)
        final_video = reel_state.get("final_video")
        narration_video = reel_state.get("narration_video")
        runway_tasks = reel_state.get("runway_tasks") or []

        if video_id and not narration_video and not final_video:
            st.info(
                "Неона записывает голос. Можно заниматься другими делами и вернуться "
                "сюда позже. Кнопка ниже не блокируется: нажмите её для проверки."
            )
            if st.button(
                "🔄 Проверить голос и оживить сцены",
                key=prefix + "_check_voice",
                use_container_width=True,
            ):
                with st.spinner("Проверяем озвучку..."):
                    result = get_avatar_video_fn(video_id)
                if not result.get("ok"):
                    error_text = str(
                        result.get("error") or "Не удалось проверить видео."
                    )
                    reel_state["status"] = "voice_error"
                    reel_state["error"] = error_text

                    # Если HeyGen потерял старый video_id, сохраняем уже готовые сцены
                    # и сбрасываем только ссылку на озвучку.
                    if "not found" in error_text.lower():
                        reel_state.pop("video_id", None)
                        reel_state.pop("video_url", None)
                    st.session_state[reel_state_key] = reel_state
                    st.rerun()
                else:
                    reel_state.update(result)
                    status = str(result.get("status") or "").lower()
                    if status == "completed" and result.get("video_url"):
                        try:
                            with st.spinner("Озвучка готова. Готовим Reels..."):
                                narration = _download_video_bytes(result["video_url"])
                                reel_state["narration_video"] = narration

                                # Если это перезапись голоса для уже готового Reels,
                                # старые Runway-сцены сохраняем и НЕ запускаем их заново.
                                existing_runway_tasks = (
                                    reel_state.get("runway_tasks") or []
                                )
                                if existing_runway_tasks:
                                    reel_state["status"] = "voice_ready"
                                else:
                                    reel_state["runway_tasks"] = _start_runway_scenes(
                                        reel_state.get("scene_images") or [],
                                        reel_state.get("scenes") or [],
                                    )
                                    reel_state["status"] = "animating"

                                reel_state.pop("error", None)
                        except (requests.RequestException, RuntimeError, OSError) as exc:
                            reel_state["status"] = "animation_error"
                            reel_state["error"] = str(exc)
                    elif status == "failed":
                        reel_state["error"] = str(
                            result.get("error") or "HeyGen не создал озвучку."
                        )
                    st.session_state[reel_state_key] = reel_state
                    st.rerun()

        narration_video = reel_state.get("narration_video")
        runway_tasks = reel_state.get("runway_tasks") or []
        final_video = reel_state.get("final_video")
        video_id = _clean_text(reel_state.get("video_id"), 300)
        scene_images = reel_state.get("scene_images") or []

        # Если HeyGen потерял video_id, изображения не создаём заново.
        # Пользователь повторяет только озвучку.
        if scene_images and not video_id and not narration_video and not final_video:
            st.info(
                "Изображения сцен уже сохранены. Повторно оплачивать их создание не нужно."
            )
            if st.button(
                "🔊 Повторить только озвучку",
                key=prefix + "_retry_voice",
                use_container_width=True,
            ):
                with st.spinner("Повторно создаём только озвучку в HeyGen..."):
                    avatar_result = create_avatar_video_fn(
                        reel_state.get("spoken_text") or item.get("script") or ""
                    )
                if avatar_result.get("ok"):
                    reel_state.update(avatar_result)
                    reel_state["status"] = "voice_started"
                    reel_state.pop("error", None)
                else:
                    reel_state["status"] = "voice_error"
                    reel_state["error"] = str(
                        avatar_result.get("error") or "Не удалось повторить озвучку."
                    )
                st.session_state[reel_state_key] = reel_state
                st.rerun()

        if narration_video and not runway_tasks and not final_video:
            st.info("Голос и изображения сохранены. Осталось оживить сцены.")
            if st.button(
                "🎞 Оживить сцены в Runway",
                key=prefix + "_start_runway",
                disabled=not runway_available,
                use_container_width=True,
            ):
                try:
                    with st.spinner("Runway запускает живые сцены..."):
                        reel_state["runway_tasks"] = _start_runway_scenes(
                            reel_state.get("scene_images") or [],
                            reel_state.get("scenes") or [],
                        )
                    reel_state["status"] = "animating"
                    reel_state.pop("error", None)
                except (requests.RequestException, RuntimeError, OSError) as exc:
                    reel_state["status"] = "animation_error"
                    reel_state["error"] = str(exc)
                st.session_state[reel_state_key] = reel_state
                st.rerun()

        runway_tasks = reel_state.get("runway_tasks") or []
        if runway_tasks and not final_video:
            ready_count = int(reel_state.get("runway_ready_count") or 0)
            total_count = len(runway_tasks)
            rebuilding_voice_only = bool(reel_state.get("rebuild_voice_only"))

            if rebuilding_voice_only and narration_video:
                st.info(
                    "Новая озвучка готова. Уже созданные живые сцены Runway "
                    "сохранены — повторно оплачивать их не нужно."
                )
                runway_button_label = "🔄 Собрать Reels с новой озвучкой"
            else:
                st.info(
                    f"Runway оживляет сцены: готово {ready_count} из {total_count}. "
                    "Обычно это занимает несколько минут. Можно уйти со страницы и вернуться."
                )
                runway_button_label = "🔄 Проверить живые сцены и собрать Reels"

            if st.button(
                runway_button_label,
                key=prefix + "_check_runway",
                use_container_width=True,
            ):
                try:
                    with st.spinner("Проверяем готовность живых сцен..."):
                        runway_result = _check_runway_scenes(runway_tasks)
                    reel_state["runway_ready_count"] = runway_result["ready"]
                    if runway_result["pending"]:
                        reel_state["status"] = "animating"
                    else:
                        with st.spinner(
                            "Сцены готовы. Добавляем голос, субтитры и фирменный знак..."
                        ):
                            live_clips = _download_runway_videos(
                                runway_result["video_urls"]
                            )
                            reel_state["final_video"] = _assemble_animated_reel(
                                live_clips,
                                narration_video,
                                reel_state.get("spoken_text") or "",
                                item.get("cta") or "",
                            )
                        reel_state["status"] = "ready"
                        reel_state.pop("error", None)
                        reel_state.pop("rebuild_voice_only", None)
                except (requests.RequestException, RuntimeError, OSError) as exc:
                    reel_state["status"] = "assembly_error"
                    reel_state["error"] = str(exc)
                st.session_state[reel_state_key] = reel_state
                st.rerun()

        if reel_state.get("error"):
            st.error(str(reel_state["error"]))
            retry_col, reset_col = st.columns(2)

            if (
                scene_images
                and not narration_video
                and retry_col.button(
                    "🔊 Повторить только озвучку",
                    key=prefix + "_retry_voice_error",
                    use_container_width=True,
                )
            ):
                with st.spinner("Повторно создаём только озвучку в HeyGen..."):
                    avatar_result = create_avatar_video_fn(
                        reel_state.get("spoken_text") or item.get("script") or ""
                    )
                if avatar_result.get("ok"):
                    reel_state.pop("video_id", None)
                    reel_state.pop("video_url", None)
                    reel_state.update(avatar_result)
                    reel_state["status"] = "voice_started"
                    reel_state.pop("error", None)
                else:
                    reel_state["status"] = "voice_error"
                    reel_state["error"] = str(
                        avatar_result.get("error") or "Не удалось повторить озвучку."
                    )
                st.session_state[reel_state_key] = reel_state
                st.rerun()

            if narration_video and retry_col.button(
                "Повторить только оживление",
                key=prefix + "_retry_animation",
                use_container_width=True,
            ):
                reel_state.pop("runway_tasks", None)
                reel_state.pop("runway_ready_count", None)
                reel_state.pop("error", None)
                reel_state["status"] = "voice_ready"
                st.session_state[reel_state_key] = reel_state
                st.rerun()
            if reset_col.button(
                "Начать Reels заново",
                key=prefix + "_reset_reel",
                use_container_width=True,
            ):
                st.session_state.pop(reel_state_key, None)
                st.rerun()

        final_video = reel_state.get("final_video")
        if final_video:
            st.video(final_video)
            st.download_button(
                "⬇️ Скачать готовый Reels MP4",
                data=final_video,
                file_name="agency_w_reels.mp4",
                mime="video/mp4",
                key=prefix + "_reel_download",
                use_container_width=True,
            )

            st.caption(
                "Если озвучка обрезалась или вы хотите перезаписать голос, "
                "сцены OpenAI и Runway останутся прежними."
            )
            if st.button(
                "🔊 Перезаписать голос и пересобрать Reels",
                key=prefix + "_rebuild_voice_only",
                use_container_width=True,
            ):
                spoken_text = (
                    reel_state.get("spoken_text")
                    or item.get("script")
                    or ""
                )
                with st.spinner(
                    "HeyGen создаёт новую озвучку. Изображения и Runway-сцены "
                    "не пересоздаются..."
                ):
                    avatar_result = create_avatar_video_fn(spoken_text)

                if avatar_result.get("ok"):
                    # Удаляем только старую озвучку и готовую сборку.
                    # Уже оплаченные изображения и Runway-задачи сохраняются.
                    reel_state.pop("final_video", None)
                    reel_state.pop("narration_video", None)
                    reel_state.pop("video_url", None)
                    reel_state.pop("error", None)
                    reel_state.update(avatar_result)
                    reel_state["rebuild_voice_only"] = True
                    reel_state["status"] = "voice_started"
                else:
                    reel_state["status"] = "voice_error"
                    reel_state["error"] = str(
                        avatar_result.get("error")
                        or "Не удалось запустить новую озвучку."
                    )

                st.session_state[reel_state_key] = reel_state
                st.rerun()
            music_path = Path(__file__).resolve().parent / "assets" / "reel_music.mp3"
            if music_path.exists():
                st.success(
                    "Готово: живые сцены, голос, субтитры, музыка и официальный "
                    "знак. Этот MP4 можно публиковать в Instagram Reels."
                )
            else:
                st.success(
                    "Готово: живые сцены, голос, субтитры и официальный знак. "
                    "Этот MP4 можно публиковать в Instagram Reels."
                )
                st.caption(
                    "Музыка добавится автоматически, когда в assets появится "
                    "лицензированный файл reel_music.mp3."
                )


def render_content_factory(
    owner_telegram_id,
    owner_name,
    ask_ai_fn,
    generate_illustration_fn,
    create_avatar_video_fn,
    get_avatar_video_fn,
    target_profile=None,
):
    """Контент-завод: одно человеческое задание превращается в готовый материал."""
    owner_id = int(owner_telegram_id)
    state_key = f"content_factory_package_{owner_id}"
    stage_key = f"content_factory_stage_{owner_id}"
    understanding_key = f"content_factory_understanding_{owner_id}"
    request_key = f"content_factory_request_{owner_id}"
    load_marker = f"content_factory_v2_loaded_{owner_id}"

    st.markdown("## 🏭 Контент-завод W")
    st.caption(
        "Скажите задачу обычными словами. Завод подготовит профессиональный текст, "
        "Стагирит проверит его, а платные инструменты включатся только после утверждения."
    )

    if not st.session_state.get(load_marker):
        latest = _load_latest_package(owner_id)
        if isinstance(latest, dict) and latest.get("workflow_version") == "single_v2":
            st.session_state[state_key] = latest
            st.session_state[stage_key] = "result"
        st.session_state[load_marker] = True

    stage = st.session_state.get(stage_key) or "start"
    package = st.session_state.get(state_key)

    if stage == "start":
        st.write(
            f"{owner_name}, вам не нужно знать, как писать промты. Выберите формат и "
            "расскажите, что хотите получить."
        )
        if st.button(
            "✨ Создать контент",
            key=f"cf2_start_{owner_id}",
            type="primary",
            use_container_width=True,
        ):
            st.session_state[stage_key] = "input"
            st.session_state.pop(state_key, None)
            st.session_state.pop(understanding_key, None)
            st.rerun()
        with st.expander("📚 Готовые материалы"):
            st.caption("Здесь будут храниться утверждённые и созданные материалы.")
        return

    if stage == "input":
        st.markdown("### 1. Что вы хотите создать?")
        format_choice = st.radio(
            "Формат",
            options=["post", "carousel", "reel"],
            format_func=lambda value: FORMAT_LABELS[value],
            horizontal=True,
            key=f"cf2_format_{owner_id}",
            label_visibility="collapsed",
        )

        st.markdown("### 2. Расскажите задачу")
        st.caption(
            "Можно говорить как в обычном разговоре. Например: «Хочу Reels о том, "
            "как предпринимателю перестать тратить вечер на переписку»."
        )
        if hasattr(st, "audio_input"):
            audio = st.audio_input(
                "🎙 Нажмите микрофон и скажите задачу",
                key=f"cf2_audio_{owner_id}",
            )
            if audio is not None:
                audio_bytes = audio.getvalue()
                audio_hash = hashlib.sha256(audio_bytes).hexdigest()
                processed_key = f"cf2_audio_processed_{owner_id}"
                if st.session_state.get(processed_key) != audio_hash:
                    try:
                        with st.spinner("Контент-завод слушает..."):
                            transcript = _transcribe_content_audio(
                                audio_bytes,
                                getattr(audio, "name", "content-request.wav"),
                            )
                        st.session_state[processed_key] = audio_hash
                        if transcript:
                            st.session_state[request_key] = transcript
                            st.rerun()
                    except (requests.RequestException, RuntimeError, ValueError):
                        st.error("Не удалось разобрать голос. Напишите задачу в поле ниже.")

        request_text = st.text_area(
            "Или напишите задачу",
            placeholder=(
                "Например: Создай карусель для предпринимателей. Покажи, как пять "
                "ИИ-профессионалов забирают повторяющиеся задачи и возвращают время."
            ),
            height=170,
            key=request_key,
        )
        action_col, back_col = st.columns([2, 1])
        if action_col.button(
            "Передать задачу заводу",
            key=f"cf2_analyse_{owner_id}",
            type="primary",
            use_container_width=True,
        ):
            if not _clean_text(request_text, 7000):
                st.warning("Сначала скажите или напишите, какой материал вам нужен.")
            else:
                try:
                    with st.spinner("Стагирит выделяет главную мысль..."):
                        understanding = _analyse_content_request(
                            format_choice,
                            request_text,
                            target_profile,
                            ask_ai_fn,
                        )
                    for suffix in ("topic", "idea", "audience", "result", "cta"):
                        st.session_state.pop(f"cf2_u_{suffix}_{owner_id}", None)
                    st.session_state[understanding_key] = understanding
                    st.session_state[stage_key] = "understanding"
                    st.rerun()
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    st.error(str(exc))
        if back_col.button(
            "Отмена",
            key=f"cf2_cancel_{owner_id}",
            use_container_width=True,
        ):
            st.session_state[stage_key] = "start"
            st.rerun()
        return

    if stage == "understanding":
        understanding = st.session_state.get(understanding_key)
        if not isinstance(understanding, dict):
            st.session_state[stage_key] = "input"
            st.rerun()

        st.markdown("### Я понял вашу задачу так")
        st.caption("Проверьте только смысл. Красивый профессиональный текст завод напишет сам.")
        with st.container(border=True):
            st.write("**Формат:** " + FORMAT_LABELS.get(understanding["format"], "Пост"))
            understanding["topic"] = st.text_area(
                "Тема",
                value=understanding.get("topic") or "",
                height=75,
                key=f"cf2_u_topic_{owner_id}",
            )
            understanding["main_idea"] = st.text_area(
                "Главная мысль",
                value=understanding.get("main_idea") or "",
                height=90,
                key=f"cf2_u_idea_{owner_id}",
            )
            understanding["audience"] = st.text_area(
                "Для кого",
                value=understanding.get("audience") or "",
                height=90,
                key=f"cf2_u_audience_{owner_id}",
            )
            understanding["desired_result"] = st.text_area(
                "Что должно измениться после просмотра",
                value=understanding.get("desired_result") or "",
                height=80,
                key=f"cf2_u_result_{owner_id}",
            )
            understanding["cta"] = st.text_area(
                "Какое одно действие должен сделать человек",
                value=understanding.get("cta") or "",
                height=80,
                key=f"cf2_u_cta_{owner_id}",
            )

        confirm_col, change_col = st.columns([2, 1])
        if confirm_col.button(
            "✅ Всё верно — подготовить материал",
            key=f"cf2_generate_{owner_id}",
            type="primary",
            use_container_width=True,
        ):
            try:
                with st.spinner("Пять профессионалов готовят качественный материал..."):
                    package = _generate_single_package(owner_id, understanding, ask_ai_fn)
                st.session_state[state_key] = package
                st.session_state[stage_key] = "result"
                saved, error = _save_package(package)
                if not saved:
                    st.session_state[f"cf2_save_warning_{owner_id}"] = error
                st.rerun()
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                st.error(str(exc))
        if change_col.button(
            "← Изменить задачу",
            key=f"cf2_change_{owner_id}",
            use_container_width=True,
        ):
            for suffix in ("topic", "idea", "audience", "result", "cta"):
                st.session_state.pop(f"cf2_u_{suffix}_{owner_id}", None)
            st.session_state[stage_key] = "input"
            st.rerun()
        return

    if not isinstance(package, dict) or not package.get("items"):
        st.session_state[stage_key] = "start"
        st.rerun()

    save_warning = st.session_state.pop(f"cf2_save_warning_{owner_id}", "")
    if save_warning:
        st.caption("Материал создан. История пока не сохранена: " + str(save_warning))

    _render_item(
        package,
        package["items"][0],
        owner_id,
        ask_ai_fn,
        generate_illustration_fn,
        create_avatar_video_fn,
        get_avatar_video_fn,
    )

    st.divider()
    if st.button(
        "✨ Создать контент",
        key=f"cf2_next_{owner_id}",
        use_container_width=True,
    ):
        st.session_state.pop(state_key, None)
        st.session_state.pop(understanding_key, None)
        st.session_state.pop(request_key, None)
        st.session_state[stage_key] = "input"
        st.rerun()

    with st.expander("📚 Готовые материалы"):
        current = package["items"][0]
        status = "Готово к производству" if current.get("status") == "approved" else "На согласовании"
        st.write(f"**{current.get('title') or 'Материал'}** — {status}")
