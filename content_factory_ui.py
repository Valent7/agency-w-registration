import json
import math
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
            "Агентства W, тёмно-синие фирменные пиджаки у профессионалов, "
            "золотой знак W на лацкане. Без букв, подписей, интерфейсного мусора, "
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
    if index == 0:
        return selected[:2]
    return [selected[(index - 1) % len(selected)]]


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
            "Премиальная деловая editorial-фотография, глубокий тёмно-синий фон, "
            "золотые световые акценты, единый стиль всей серии. "
            "Оставь визуально спокойное тёмное пространство в нижней трети для "
            "последующего размещения текста. Не рисуй буквы, цифры, логотипы, "
            "водяные знаки, интерфейсы и рамки."
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
Поле script содержит ТОЛЬКО слова диктора. В нём запрещены номера сцен,
ремарки, скобки и указания «в кадре», «на экране», «камера», «переход».
Технические описания записывай исключительно в массив scenes.
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
            st.caption(
                "Завод создаст каждый пункт отдельным слайдом 1080×1350, "
                "наложит точный русский текст и сложит всё в один ZIP."
            )
            st.warning(
                "Создание использует платный Художник OpenAI: одно изображение "
                "на каждый слайд. Нажмите кнопку один раз и дождитесь окончания."
            )
            if image_col.button(
                "🏭 Создать готовую карусель",
                key=prefix + "_carousel",
                use_container_width=True,
            ):
                with st.spinner(
                    f"Художник создаёт {len(item.get('slides') or [])} отдельных слайдов..."
                ):
                    carousel_result = _generate_ready_carousel(
                        item, generate_illustration_fn
                    )
                st.session_state[carousel_state_key] = carousel_result

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
