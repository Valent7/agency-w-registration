"""Local editorial layouts and a bounded, sequential multi-scene video renderer.

Artwork and speech come from the caller. This module never calls a paid service.
Each scene is encoded separately, so adding scenes does not multiply FFmpeg's
working set. The final video stream is copied rather than encoded a second time.
"""
from __future__ import annotations

import io
import json
import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from agency_video_branding import (
    VideoBrandingError, check_video_logo, is_agency_w_brand, prepare_video_logo, video_logo_filter,
)
from agency_outro import append_agency_outro

class MediaError(RuntimeError):
    """A bounded, credential-free error suitable for the material's status."""


def _font(size, bold=False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for path in (Path("/usr/share/fonts/truetype/dejavu") / name, Path(name)):
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            pass
    raise MediaError("Для оформления материалов нужен шрифт DejaVu Sans.")


def _color(value):
    if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise MediaError("Цвет оформления должен иметь формат #RRGGBB.")
    return value


def _text(value, maximum, *, empty=False):
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        raise MediaError("Текст оформления имеет неверный формат или слишком длинный.")
    if not value.strip() and not empty:
        raise MediaError("Для оформления нужен непустой текст.")
    return value.strip()


def _image(data):
    if not isinstance(data, bytes) or not data or len(data) > 20 * 1024 * 1024:
        raise MediaError("Нужно непустое изображение размером до 20 МБ.")
    try:
        with Image.open(io.BytesIO(data)) as source:
            if source.width * source.height > 25_000_000:
                raise MediaError("Разрешение исходного изображения слишком большое.")
            source.load()
            return ImageOps.exif_transpose(source).convert("RGB")
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise MediaError("Не удалось прочитать исходное изображение.") from exc


def _size(size):
    if (not isinstance(size, tuple) or len(size) != 2 or
            any(isinstance(v, bool) or not isinstance(v, int) for v in size) or
            not 540 <= size[0] <= 2160 or not 540 <= size[1] <= 2160 or
            size[0] * size[1] > 4_700_000):
        raise MediaError("Размер оформления должен быть от 540 до 2160 пикселей по стороне.")
    return size


def _wrap(draw, text, font, width):
    lines = []
    for paragraph in text.splitlines():
        current = ""
        for word in paragraph.split():
            candidate = (current + " " + word).strip()
            if draw.textlength(candidate, font=font) <= width:
                current = candidate
                continue
            if current:
                lines.append(current)
                current = ""
            for char in word:
                if current and draw.textlength(current + char, font=font) > width:
                    lines.append(current)
                    current = ""
                current += char
        lines.append(current)
    return lines


def _fit(draw, text, width, height, *, start=64, minimum=30):
    for size in range(start, minimum - 1, -2):
        font = _font(size, bold=True)
        lines = _wrap(draw, text, font, width)
        line_height = math.ceil(size * 1.28)
        if len(lines) * line_height <= height:
            return font, lines, line_height
    raise MediaError("Текст не помещается на изображении. Сократите текст слайда.")


def _brand(image, brand_name, color):
    width, _ = image.size
    margin = round(width * 0.05)
    font = _font(max(18, round(width * 0.024)), bold=True)
    draw = ImageDraw.Draw(image)
    lines = _wrap(draw, brand_name, font, round(width * 0.66))
    if len(lines) > 2:
        raise MediaError("Название проекта слишком длинное для подписи.")
    line_height = math.ceil(font.size * 1.25)
    padding = max(10, round(width * 0.014))
    badge_width = math.ceil(max(draw.textlength(line, font=font) for line in lines)) + padding * 2 + 10
    badge_height = line_height * len(lines) + padding * 2
    draw.rounded_rectangle((margin, margin, margin + badge_width, margin + badge_height),
                           radius=padding, fill=(16, 19, 27, 225))
    draw.rounded_rectangle((margin, margin + padding, margin + 4, margin + badge_height - padding),
                           radius=2, fill=color)
    for i, line in enumerate(lines):
        draw.text((margin + padding + 5, margin + padding - 3 + i * line_height), line,
                  font=font, fill="white")


def _jpeg(image):
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=95, subsampling=0)
    return buffer.getvalue()


def _slide_text_layout(draw, text, width, height, number):
    """Shared geometry for initial composition and bounded lettering repairs."""
    margin = round(width * 0.055)
    padding = round(width * 0.034)
    font, lines, line_height = _fit(
        draw, text, width - 2 * (margin + padding), round(height * 0.34),
        start=round(width * 0.059), minimum=max(24, round(width * 0.028)),
    )
    text_height = line_height * len(lines)
    number_height = round(width * 0.040) if number else 0
    panel_height = text_height + padding * 2 + number_height
    top = height - margin - panel_height
    return margin, padding, font, lines, line_height, text_height, top


def render_slide(image_bytes, text, brand_name, brand_color, *, size=(1080, 1350),
                 slide_number="", layout="editorial"):
    """Compose readable text while retaining most of the commissioned artwork.

    ``cover`` uses a soft lower gradient, ``editorial`` a compact light card,
    and ``clean`` an edge-to-edge light footer. Text never silently crops.
    """
    width, height = _size(size)
    text, brand_name = _text(text, 700), _text(brand_name, 120)
    number = _text(slide_number, 20, empty=True)
    color = _color(brand_color)
    if layout not in {"cover", "editorial", "clean"}:
        raise MediaError("Неизвестный вариант оформления слайда.")
    image = ImageOps.fit(_image(image_bytes), size, method=Image.Resampling.LANCZOS).convert("RGBA")
    margin, padding, font, lines, line_height, text_height, top = _slide_text_layout(
        ImageDraw.Draw(image), text, width, height, number,
    )
    overlay = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    if layout == "cover":
        gradient_top = max(0, top - round(height * 0.10))
        span = max(1, height - gradient_top)
        for y in range(gradient_top, height):
            alpha = round(235 * ((y - gradient_top) / span) ** 0.72)
            draw.line((0, y, width, y), fill=(8, 12, 20, alpha))
        foreground = "white"
    elif layout == "clean":
        draw.rectangle((0, top, width, height), fill=(250, 250, 247, 255))
        draw.rectangle((margin + padding, top + padding - 10,
                        margin + padding + round(width * 0.08), top + padding - 6), fill=color)
        foreground = "#161B24"
    else:
        draw.rounded_rectangle((margin, top, width - margin, height - margin),
                               radius=round(width * 0.024), fill=(250, 250, 247, 246))
        foreground = "#161B24"
    for i, line in enumerate(lines):
        draw.text((margin + padding, top + padding - 5 + i * line_height), line,
                  font=font, fill=foreground)
    if number:
        number_font = _font(max(20, round(width * 0.025)), bold=True)
        label_width = draw.textlength(number, font=number_font)
        draw.text((width - margin - padding - label_width,
                   top + padding + text_height + 5), number, font=number_font, fill=foreground)
    image = Image.alpha_composite(image, overlay)
    _brand(image, brand_name, color)
    return _jpeg(image)


def reletter_slide(image_bytes, previous_text, corrected_text, *, slide_number="", layout="editorial"):
    """Repair lettering on a known, already composed slide without new artwork.

    The caller must supply the exact previous text, number and layout used by
    ``render_slide``. Only an opaque light panel can be recovered from the
    flattened image. Its original bounds remain fixed even if the new wording
    is shorter; a longer correction must fit inside its original text area.
    Artwork and the brand badge outside that panel are never redrawn or resized.
    """
    if layout not in {"editorial", "clean"}:
        raise MediaError("Исправить надпись без исходного изображения можно только на светлой карточке или светлом нижнем поле.")
    previous_text, corrected_text = _text(previous_text, 700), _text(corrected_text, 700)
    number = _text(slide_number, 20, empty=True)
    image = _image(image_bytes)
    if image.size != (1080, 1350):
        raise MediaError("Для исправления надписи нужен готовый слайд размером 1080 × 1350.")
    width, height = image.size
    draw = ImageDraw.Draw(image)
    margin, padding, _, _, _, old_text_height, top = _slide_text_layout(
        draw, previous_text, width, height, number,
    )
    font, lines, line_height = _fit(
        draw, corrected_text, width - 2 * (margin + padding), old_text_height,
        start=round(width * 0.059), minimum=max(24, round(width * 0.028)),
    )
    # Initial editorial cards are translucent. A repair must be opaque, otherwise
    # the old glyphs would remain visible through the replacement lettering.
    background, foreground = (250, 250, 247), "#161B24"
    if layout == "clean":
        # The accent is already flattened into the image and its color is not
        # part of this repair contract. Preserve that exact existing strip.
        accent_box = (margin + padding, top + padding - 10,
                      margin + padding + round(width * 0.08) + 1, top + padding - 5)
        accent = image.crop(accent_box)
        draw.rectangle((0, top, width, height), fill=background)
        image.paste(accent, accent_box[:2])
    else:
        draw.rounded_rectangle((margin, top, width - margin, height - margin),
                               radius=round(width * 0.024), fill=background)
    for index, line in enumerate(lines):
        draw.text((margin + padding, top + padding - 5 + index * line_height),
                  line, font=font, fill=foreground)
    if number:
        number_font = _font(max(20, round(width * 0.025)), bold=True)
        label_width = draw.textlength(number, font=number_font)
        draw.text((width - margin - padding - label_width,
                   top + padding + old_text_height + 5), number,
                  font=number_font, fill=foreground)
    return _jpeg(image)


def _ass_time(seconds):
    centiseconds = max(0, round(seconds * 100))
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    seconds, fraction = divmod(remainder, 100)
    return f"{hours}:{minutes:02}:{seconds:02}.{fraction:02}"


def _caption_events(narration, duration):
    words = narration.split()
    chunks = [" ".join(words[i:i + 6]) for i in range(0, len(words), 6)]
    total = sum(len(chunk) for chunk in chunks)
    cursor = 0.0
    events = []
    for i, chunk in enumerate(chunks):
        end = duration if i == len(chunks) - 1 else cursor + duration * len(chunk) / total
        # Full-width braces/backslash cannot introduce ASS formatting commands.
        safe = chunk.replace("\\", "＼").replace("{", "｛").replace("}", "｝")
        events.append((cursor, end, safe))
        cursor = end
    return events


def _write_scene_captions(path, events, start, end):
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,52,&H00FFFFFF,&H00FFFFFF,&H00121212,&H80000000,-1,0,0,0,100,100,0,0,1,3,1,2,90,90,240,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    for first, last, text in events:
        if first >= end or last <= start:
            continue
        lines.append(f"Dialogue: 0,{_ass_time(max(first,start)-start)},"
                     f"{_ass_time(min(last,end)-start)},Default,,0,0,0,,{text}")
    path.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")


def _video_frame(data, title, brand_name, color):
    image = ImageOps.fit(_image(data), (1080, 1920), method=Image.Resampling.LANCZOS).convert("RGBA")
    if title:
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        font, lines, line_height = _fit(draw, title, 900, 340, start=62, minimum=32)
        bottom = 175 + len(lines) * line_height + 45
        for y in range(125, min(1000, bottom + 100)):
            opacity = 205 if y <= bottom else round(205 * (1 - (y - bottom) / 100))
            draw.line((0, y, 1080, y), fill=(8, 12, 20, max(0, opacity)))
        for index, line in enumerate(lines):
            draw.text((90, 175 + index * line_height), line, font=font, fill="white")
        image = Image.alpha_composite(image, overlay)
    if not is_agency_w_brand(brand_name):
        _brand(image, brand_name, color)
    return _jpeg(image)


def render_story_reel(
    scene_images, speech_bytes, narration, title, brand_name, brand_color, *,
    intro_image_bytes=None, outro_image_bytes=None, music_bytes=None,
    voice_volume=1.0, music_volume=0.12, apply_finishing=False,
):
    """Make a narrated story and finish Agency W films with selected branding/audio."""
    if not isinstance(scene_images, list) or not 3 <= len(scene_images) <= 5:
        raise MediaError("Для видео нужны от трёх до пяти изображений сцен.")
    if (any(not isinstance(item, bytes) or not item for item in scene_images) or
            sum(len(item) for item in scene_images) > 50 * 1024 * 1024):
        raise MediaError("Изображения сцен отсутствуют или превышают допустимый размер.")
    if not isinstance(speech_bytes, bytes) or not speech_bytes or len(speech_bytes) > 16 * 1024 * 1024:
        raise MediaError("Озвучка отсутствует или превышает допустимый размер.")
    narration, title = _text(narration, 3000), _text(title, 150)
    brand_name, color = _text(brand_name, 120), _color(brand_color)
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise MediaError("Для создания видео нужны FFmpeg и ffprobe.")
    with tempfile.TemporaryDirectory(prefix="content_factory_story_") as directory:
        root = Path(directory)
        (root / "speech.mp3").write_bytes(speech_bytes)
        try:
            probe = subprocess.run([ffprobe,"-v","error","-protocol_whitelist","file,pipe",
                                    "-format_whitelist","mp3,wav","-show_entries","format=duration",
                                    "-of","json","speech.mp3"],cwd=root,capture_output=True,
                                   timeout=15,check=True)
            duration = float(json.loads(probe.stdout)["format"]["duration"])
        except (subprocess.SubprocessError,OSError,ValueError,TypeError,KeyError) as exc:
            raise MediaError("Не удалось определить длительность озвучки.") from exc
        if not math.isfinite(duration) or not 0.3 <= duration <= 59.5:
            raise MediaError("Озвучка видео должна длиться от 0,3 до 59,5 секунды.")
        frames = math.ceil((duration + 0.35) * 24)
        base, extra = divmod(frames, len(scene_images))
        events = _caption_events(narration, duration)
        try:
            logo = prepare_video_logo(root, (1080, 1920), brand_name)
        except VideoBrandingError as error:
            raise MediaError(str(error)) from None
        cursor = 0.0
        for index, artwork in enumerate(scene_images):
            count = base + (index < extra)
            scene_duration = count / 24
            filename = f"scene-{index}"
            (root / f"{filename}.jpg").write_bytes(_video_frame(
                artwork, title if index == 0 else "", brand_name, color))
            _write_scene_captions(root / f"{filename}.ass",events,cursor,cursor+scene_duration)
            movement = "min(1.01+0.00016*on,1.055)" if index % 2 == 0 else "max(1.055-0.00016*on,1.01)"
            movement_filter = (f"zoompan=z='{movement}':x='iw/2-iw/zoom/2':"
                               "y='ih/2-ih/zoom/2':d=1:s=1080x1920:fps=24")
            lettering_filter = f"subtitles={filename}.ass,format=yuv420p"
            inputs = (["-protocol_whitelist", "file,pipe", "-format_whitelist", "png_pipe", "-i", logo.name]
                      if logo else [])
            filters = (["-filter_complex", video_logo_filter(movement_filter, lettering_filter, (1080, 1920)),
                        "-map", "[branded]"] if logo else ["-vf", movement_filter + "," + lettering_filter])
            try:
                subprocess.run([ffmpeg,"-y","-loglevel","error","-filter_threads","1","-filter_complex_threads","1",
                                "-loop","1","-framerate","24","-i",f"{filename}.jpg",
                                *inputs,*filters,"-frames:v",str(count),"-an","-c:v","libx264",
                                "-threads","1","-preset","veryfast","-crf","21",
                                "-pix_fmt","yuv420p",f"{filename}.mp4"],cwd=root,
                               capture_output=True,timeout=240,check=True)
            except (subprocess.SubprocessError,OSError) as exc:
                raise MediaError("Не удалось собрать одну из сцен видео.") from exc
            cursor += scene_duration
        (root / "scenes.txt").write_text("".join(
            f"file 'scene-{index}.mp4'\n" for index in range(len(scene_images))),encoding="utf-8")
        try:
            subprocess.run([ffmpeg,"-y","-loglevel","error","-f","concat","-safe","1",
                            "-i","scenes.txt","-protocol_whitelist","file,pipe",
                            "-format_whitelist","mp3,wav","-i","speech.mp3","-map","0:v:0","-map","1:a:0",
                            "-c:v","copy","-c:a","aac","-threads","1","-b:a","128k",
                            "-af","apad","-t",f"{frames / 24:.6f}","-movflags","+faststart",
                            "story.mp4"],cwd=root,capture_output=True,timeout=120,check=True)
            output = root / "story.mp4"
            if not output.is_file() or not 0 < output.stat().st_size <= 100 * 1024 * 1024:
                raise MediaError("Видео отсутствует или превышает допустимый размер.")
            result = output.read_bytes()
            if apply_finishing and is_agency_w_brand(brand_name):
                try:
                    result = append_agency_outro(
                        result, logo_path=check_video_logo(brand_name), size=(1080, 1920), title=title,
                        intro_image_bytes=intro_image_bytes, outro_image_bytes=outro_image_bytes,
                        music_bytes=music_bytes, voice_volume=voice_volume, music_volume=music_volume,
                    )
                except (RuntimeError, ValueError, VideoBrandingError) as exc:
                    raise MediaError(str(exc)) from None
            return result
        except (subprocess.SubprocessError,OSError) as exc:
            raise MediaError("Не удалось соединить сцены и озвучку видео.") from exc
