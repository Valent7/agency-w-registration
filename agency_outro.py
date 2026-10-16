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

from functools import lru_cache
import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps

INTRO_SECONDS = 2.0
OUTRO_SECONDS = 2.0

# These are the two user-approved 9:16 designs (uploaded to the owner's
# HeyGen Assets workspace 2026-10-16). Do not substitute stock images.
INTRO_TEMPLATE_URL = (
    "https://dynamic.heygen.ai/image/"
    "8c2d5a0b3c154c3b8827be15be0a7da4/original.png"
)
OUTRO_TEMPLATE_URL = (
    "https://dynamic.heygen.ai/image/"
    "c150a46d7f104170b6cf34017cd3e674/original.png"
)


@lru_cache(maxsize=2)
def _fetch_template(url: str) -> bytes:
    """Read approved background once per process; no paid video API requests."""
    if url not in (INTRO_TEMPLATE_URL, OUTRO_TEMPLATE_URL):
        raise ValueError("Недопустимый адрес фирменного шаблона.")
    try:
        response = requests.get(url, timeout=35)
        response.raise_for_status()
        content = response.content
        with Image.open(BytesIO(content)) as image:
            image.verify()
        return content
    except (requests.RequestException, OSError, ValueError) as exc:
        raise RuntimeError(
            "Фирменный шаблон Агентства W недоступен. "
            "Видео не собираем с чужой заставкой; попробуйте позже."
        ) from exc


def _background(url: str, size: tuple[int, int]) -> Image.Image:
    """Preserve the approved blue/gold artwork, center crop to exact 9:16."""
    if abs(size[0] * 16 - size[1] * 9) > 24:
        raise ValueError("Формат фирменной заставки должен быть 9:16.")
    with Image.open(BytesIO(_fetch_template(url))) as src:
        return ImageOps.fit(
            src.convert("RGB"), size,
            method=Image.Resampling.LANCZOS, centering=(0.5, 0.5),
        )



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
    """Use the exact approved branded end slate, with no retyped text."""
    if not Path(logo_path).is_file():
        raise RuntimeError("Не найден оригинальный логотип Агентства W.")
    image = _background(OUTRO_TEMPLATE_URL, size)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


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
    """Put each film's editable title into the EMPTY central area."""
    if not Path(logo_path).is_file():
        raise RuntimeError("Не найден оригинальный логотип Агентства W.")
    title = str(title or "").strip()
    if not title:
        raise ValueError("Укажите название ролика — оно появится на первой заставке.")
    canvas = _background(INTRO_TEMPLATE_URL, size)
    draw = ImageDraw.Draw(canvas)
    width, height = size
    scale = width / 720.0
    px = lambda v: round(v * scale)
    lines, font = _fit_title(
        draw, title, width - px(92), max_lines=4, scale=scale,
    )
    # The original template was deliberately authored without text here.
    # Keep every word readable within the calm area between the real crest
    # and the blue/gold waves at the bottom.
    gap = px(17)
    heights = []
    for line in lines:
        b = draw.textbbox((0, 0), line, font=font)
        heights.append(b[3] - b[1])
    total = sum(heights) + max(0, len(lines) - 1) * gap
    if total > px(300):
        raise ValueError("Слишком длинное название — сократите заголовок.")
    top = px(708) - total / 2
    for index, line in enumerate(lines):
        bounds = draw.textbbox((0, 0), line, font=font)
        line_width = bounds[2] - bounds[0]
        draw.text(
            ((width - line_width) / 2, top - bounds[1]),
            line, font=font,
            fill="#FFE2A3" if index == 0 else "#FFFFFF",
            stroke_width=max(2, px(3)), stroke_fill="#071729",
        )
        top += heights[index] + gap
    output = BytesIO()
    canvas.save(output, format="PNG")
    return output.getvalue()


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
