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

OUTRO_SECONDS = 3.0


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
    video_bytes: bytes, *, logo_path: Path, size=(720, 1280)
) -> bytes:
    """Append three seconds after untrimmed original speech and video.

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
        card = tmp / "brand.png"
        bumper = tmp / "bumper.mp4"
        result = tmp / "video_with_brand.mp4"
        raw.write_bytes(video_bytes)
        card.write_bytes(render_agency_outro(logo_path, size=size))
        _run_ffmpeg([
            ffmpeg, "-y", "-loop", "1", "-framerate", "30", "-i", str(card),
            "-f", "lavfi", "-i",
            "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-t", str(OUTRO_SECONDS), "-c:v", "libx264",
            "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-shortest", str(bumper),
        ], bumper, timeout=120)
        filters = (
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},fps=30,setsar=1,format=yuv420p,"
            "setpts=PTS-STARTPTS[v0];"
            f"[1:v]scale={width}:{height},fps=30,setsar=1,"
            "format=yuv420p,setpts=PTS-STARTPTS[v1];"
            "[0:a]aresample=48000,"
            "aformat=sample_fmts=fltp:channel_layouts=stereo,"
            "asetpts=PTS-STARTPTS[a0];"
            "[1:a]aresample=48000,"
            "aformat=sample_fmts=fltp:channel_layouts=stereo,"
            "asetpts=PTS-STARTPTS[a1];"
            "[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]"
        )
        _run_ffmpeg([
            ffmpeg, "-y", "-i", str(raw), "-i", str(bumper),
            "-filter_complex", filters,
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", str(result),
        ], result)
        return result.read_bytes()
