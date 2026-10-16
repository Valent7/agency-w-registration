"""Append a three-second Agency W end slate after every finished talking video.

Uses the real logo in assets/agency_w_icon.png. No paid AI/image/video
requests and no re-generation of narration or Runway/HeyGen scenes.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

INTRO_SECONDS = 2.0
OUTRO_SECONDS = 2.0


def _font(size: int, *, bold: bool = False):
    candidates = (
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"]
        if bold else
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"]
    )
    for path in candidates:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def render_agency_outro(logo_path: Path, *, size=(720, 1280)) -> bytes:
    """Brand end card with the EXACT original crest; no generated letter W."""
    if not Path(logo_path).is_file():
        raise RuntimeError("Не найден оригинальный логотип Агентства W.")
    width, height = size
    if width < 320 or height < 550:
        raise ValueError("Некорректный размер вертикального видео.")
    image = Image.new("RGB", size, "black")
    draw = ImageDraw.Draw(image)
    # Keep the original logo's dark matte seamless; add the brand's deep blue
    # below it, with restrained gold separators.
    for y in range(height):
        amount = max(0.0, min(1.0, (y / height - 0.53) / 0.47))
        draw.line(
            (0, y, width, y),
            fill=(round(5 * amount), round(32 * amount), round(51 * amount)),
        )
    scale = width / 720.0

    def px(v):
        return round(v * scale)

    draw.line((px(78), px(155), width - px(78), px(155)),
              fill="#A88A43", width=max(2, px(2)))
    draw.line((px(145), px(1123), width - px(145), px(1123)),
              fill="#A88A43", width=max(2, px(2)))
    with Image.open(logo_path) as original:
        logo = original.convert("RGBA")
        logo.thumbnail((px(425), px(425)), Image.Resampling.LANCZOS)
        image.paste(logo, ((width - logo.width) // 2, px(225)), logo)

    draw = ImageDraw.Draw(image)

    def center(line, y, font, color):
        bounds = draw.textbbox((0, 0), line, font=font)
        draw.text(((width - (bounds[2] - bounds[0])) / 2, px(y)),
                  line, font=font, fill=color)

    center("Производство", 790, _font(px(43), bold=True), "#E8C173")
    center("Контент-завода", 855, _font(px(43), bold=True), "#E8C173")
    center("Агентства W", 945, _font(px(48), bold=True), "#FFFFFF")
    center("Мы возвращаем человеку время", 1052, _font(px(24)), "#C8E1EA")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _fit_title(draw, title: str, max_width: int, max_lines: int, scale: float):
    """Build a legible, balanced 2–4 line headline without clipping letters."""
    import re

    title = re.sub(r"\s+", " ", str(title or "").strip())
    title = title or "Агентство W"
    # A two-second hook should be short. Long user-authored titles are
    # still displayed in full, but a smaller type is used when necessary.
    words = title.split()
    for point_size in (70, 66, 62, 58, 54, 50, 46, 42, 38, 34, 30):
        font = _font(max(19, round(point_size * scale)), bold=True)
        lines = []
        current = ""
        failed = False
        for word in words:
            candidate = (current + " " + word).strip()
            if draw.textbbox((0, 0), candidate, font=font)[2] > max_width:
                if not current:
                    failed = True
                    break
                lines.append(current)
                current = word
            else:
                current = candidate
        if failed:
            continue
        if current:
            lines.append(current)
        if len(lines) <= max_lines and all(
            draw.textbbox((0, 0), line, font=font)[2] <= max_width
            for line in lines
        ):
            return lines, font
    raise ValueError(
        "Заголовок не помещается на двухсекундной заставке. "
        "Сократите его до одной цепляющей фразы."
    )


def render_agency_intro(
    title: str, logo_path: Path, *, size=(720, 1280)
) -> bytes:
    """Readable vertical hook over the authentic Agency W golden crest."""
    # Inherit the color system, logo and safe margins of the outro.
    canvas = Image.open(BytesIO(render_agency_outro(logo_path, size=size))).convert("RGB")
    draw = ImageDraw.Draw(canvas)
    width, height = size
    scale = width / 720.0
    px = lambda v: round(v * scale)
    # Cover outro-specific typography, retaining the authentic logo above.
    draw.rectangle((0, px(750), width, height), fill=(2, 16, 27))
    draw.line((px(90), px(740), width - px(90), px(740)),
              fill="#E8C173", width=max(2, px(3)))
    lines, font = _fit_title(draw, title, width - px(90), max_lines=4, scale=scale)
    # Keep a generous optical center and line spacing; never squish letters.
    heights = []
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        heights.append(bbox[3] - bbox[1])
    gap = px(18)
    total = sum(heights) + max(0, len(lines) - 1) * gap
    if total > px(305):
        raise ValueError("Заголовок слишком длинный для читабельной заставки.")
    top = px(912) - total / 2
    for i, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=font)
        line_width = bbox[2] - bbox[0]
        draw.text(
            ((width - line_width) / 2, top - bbox[1]),
            line, font=font,
            fill=("#F5D38B" if i == 0 else "#FFFFFF"),
            stroke_width=max(1, px(2)), stroke_fill="#1A2130",
        )
        top += heights[i] + gap
    draw.line((px(110), px(1102), width - px(110), px(1102)),
              fill="#C39851", width=max(1, px(2)))
    buffer = BytesIO()
    canvas.save(buffer, format="PNG")
    return buffer.getvalue()


def _run_ffmpeg(command, target: Path, *, timeout: int = 450):
    run = subprocess.run(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=timeout, check=False,
    )
    if run.returncode != 0 or not target.is_file() or target.stat().st_size < 1024:
        raise RuntimeError(
            "Не удалось добавить фирменную заставку: "
            + run.stderr.decode("utf-8", errors="replace")[-600:]
        )


def append_agency_outro(
    video_bytes: bytes, *, logo_path: Path, size=(720, 1280), title: str = ""
) -> bytes:
    """Add a readable two-second hook and two-second agency end slate.

    The final audio is silence, not an invented musical track. Existing music
    and narration remain in the original section, unaffected.
    """
    if not video_bytes:
        raise ValueError("Готовое видео пустое.")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("Для фирменной заставки нужен ffmpeg.")
    width, height = size
    with tempfile.TemporaryDirectory(prefix="agency_w_outro_") as folder:
        tmp = Path(folder)
        raw = tmp / "original.mp4"
        opener_card = tmp / "intro.png"
        closer_card = tmp / "outro.png"
        opener = tmp / "intro.mp4"
        closer = tmp / "outro.mp4"
        result = tmp / "video_with_brand.mp4"
        raw.write_bytes(video_bytes)
        opener_card.write_bytes(render_agency_intro(title, logo_path, size=size))
        closer_card.write_bytes(render_agency_outro(logo_path, size=size))
        for card, clip, seconds in (
            (opener_card, opener, INTRO_SECONDS),
            (closer_card, closer, OUTRO_SECONDS),
        ):
            _run_ffmpeg([
                ffmpeg, "-y", "-loop", "1", "-framerate", "30",
                "-i", str(card), "-f", "lavfi", "-i",
                "anullsrc=channel_layout=stereo:sample_rate=48000",
                "-t", str(seconds), "-c:v", "libx264",
                "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k", "-shortest", str(clip),
            ], clip, timeout=120)
        common_scale = (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},fps=30,setsar=1,format=yuv420p,"
            "setpts=PTS-STARTPTS"
        )
        filters = ";".join(
            [
                f"[{i}:v]{common_scale}[v{i}]"
                for i in range(3)
            ] + [
                f"[{i}:a]aresample=48000,"
                "aformat=sample_fmts=fltp:channel_layouts=stereo,"
                f"asetpts=PTS-STARTPTS[a{i}]"
                for i in range(3)
            ] + [
                "[v0][a0][v1][a1][v2][a2]"
                "concat=n=3:v=1:a=1[v][a]"
            ]
        )
        _run_ffmpeg([
            ffmpeg, "-y", "-i", str(opener), "-i", str(raw),
            "-i", str(closer), "-filter_complex", filters,
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", str(result),
        ], result)
        return result.read_bytes()
