"""Private, on-demand MP4 clean-up for user-supplied, authorized short videos.

No TikTok/API access, no downloading third-party posts, no third-party upload.
FFmpeg delogo reconstructs pixels locally; effectiveness depends on background.
"""
from __future__ import annotations

import json
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont
import shutil
import subprocess
import tempfile
from pathlib import Path

MAX_VIDEO_BYTES = 120 * 1024 * 1024
MAX_VIDEO_SECONDS = 180
ALLOWED_MASK_MODES = {"both", "left", "right", "none"}


class VideoCleanupError(ValueError):
    """Readable video clean-up validation failure."""


def _binary(name: str) -> str:
    value = shutil.which(name)
    if not value:
        raise VideoCleanupError(
            "На сервере не найден FFmpeg. Попросите администратора проверить packages.txt."
        )
    return value


def _probe_video(source: Path) -> dict:
    command = [
        _binary("ffprobe"), "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height:format=duration",
        "-of", "json", str(source),
    ]
    try:
        result = subprocess.run(
            command, check=True, capture_output=True, text=True, timeout=15,
        )
        obj = json.loads(result.stdout)
        stream = (obj.get("streams") or [{}])[0]
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
        seconds = float((obj.get("format") or {}).get("duration") or 0)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            ValueError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise VideoCleanupError("Не удалось открыть видео. Загрузите обычный MP4 или MOV.") from exc
    if not (0 < seconds <= MAX_VIDEO_SECONDS):
        raise VideoCleanupError(
            "Сейчас поддерживаются ролики длительностью до трёх минут."
        )
    if not (160 <= width <= 2560 and 160 <= height <= 2560):
        raise VideoCleanupError("Размер кадра не поддерживается. Используйте видео до 2560 пикселей по каждой стороне.")
    return {"width": width, "height": height, "duration": seconds}


def _mask_regions(
    width: int,
    height: int,
    mode: str,
    position_pct: int = 82,
    *,
    left_y_pct: int | None = None,
    right_y_pct: int | None = None,
    mask_width_pct: int = 38,
    mask_height_pct: int = 16,
) -> list[tuple[int, int, int, int]]:
    """Exactly the same pixel regions are used for preview and FFmpeg."""
    if mode not in ALLOWED_MASK_MODES:
        raise VideoCleanupError("Неизвестный вариант положения знака.")
    for value, label in (
        (position_pct, "Высота"),
        (left_y_pct if left_y_pct is not None else 82, "Положение слева"),
        (right_y_pct if right_y_pct is not None else 82, "Положение справа"),
    ):
        if not isinstance(value, int) or not (8 <= value <= 92):
            raise VideoCleanupError(f"{label}: выберите значение от 8 до 92%.")
    if not 10 <= mask_width_pct <= 58 or not 6 <= mask_height_pct <= 30:
        raise VideoCleanupError("Неверная ширина или высота выделенной области.")
    if mode == "none":
        return []
    w = min(width - 8, max(16, round(width * mask_width_pct / 100)))
    h = min(height - 8, max(16, round(height * mask_height_pct / 100)))
    pad = max(2, round(width * 0.012))
    rects = []
    for side in ("left", "right"):
        if mode != "both" and mode != side:
            continue
        pct = (
            left_y_pct if side == "left" and left_y_pct is not None else
            right_y_pct if side == "right" and right_y_pct is not None else
            position_pct
        )
        x = pad if side == "left" else width - w - pad
        center = round(height * pct / 100)
        y = max(2, min(height - h - 2, center - h // 2))
        rects.append((x, y, w, h))
    return rects


def _delogo_filters(
    width: int,
    height: int,
    mode: str,
    position_pct: int,
    *,
    left_y_pct: int | None = None,
    right_y_pct: int | None = None,
    mask_width_pct: int = 38,
    mask_height_pct: int = 16,
) -> str:
    return ",".join(
        f"delogo=x={x}:y={y}:w={w}:h={h}:show=0"
        for x, y, w, h in _mask_regions(
            width, height, mode, position_pct,
            left_y_pct=left_y_pct, right_y_pct=right_y_pct,
            mask_width_pct=mask_width_pct, mask_height_pct=mask_height_pct,
        )
    )


def sample_video_frames(video_bytes: bytes, filename: str = "video.mp4") -> tuple[bytes, ...]:
    """Extract at two different times to reveal jumping TikTok marks.

    Returns small JPEG frames only. No uploads, no external processing.
    """
    if not isinstance(video_bytes, bytes) or not video_bytes:
        raise VideoCleanupError("Загрузите видео для просмотра.")
    if len(video_bytes) > MAX_VIDEO_BYTES:
        raise VideoCleanupError("Видео превышает 120 МБ.")
    ext = Path(str(filename)).suffix.lower()
    if ext not in {".mp4", ".mov", ".m4v"}:
        raise VideoCleanupError("Поддерживаются MP4, MOV и M4V.")
    output = []
    with tempfile.TemporaryDirectory(prefix="agency_w_preview_") as temp:
        p = Path(temp) / ("preview" + ext)
        p.write_bytes(video_bytes)
        info = _probe_video(p)
        moments = (min(0.5, info["duration"] * 0.20),
                   max(0.1, info["duration"] * 0.65))
        for moment in moments:
            cmd = [
                _binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin",
                "-ss", str(round(moment, 3)), "-i", str(p),
                "-frames:v", "1", "-vf", "scale=400:-2",
                "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "4", "-",
            ]
            try:
                result = subprocess.run(
                    cmd, capture_output=True, check=True, timeout=24,
                )
                with Image.open(BytesIO(result.stdout)) as frame:
                    rgb = frame.convert("RGB")
                    buf = BytesIO()
                    rgb.save(buf, "JPEG", quality=80)
                    output.append(buf.getvalue())
            except (OSError, ValueError, subprocess.CalledProcessError,
                    subprocess.TimeoutExpired) as exc:
                raise VideoCleanupError(
                    "Не получилось получить кадры для предпросмотра."
                ) from exc
    return tuple(output)


def preview_mask_image(
    frame_bytes: bytes,
    mode: str,
    position_pct: int = 82,
    *,
    left_y_pct: int | None = None,
    right_y_pct: int | None = None,
    mask_width_pct: int = 38,
    mask_height_pct: int = 16,
) -> bytes:
    """Red regions mark intended clean-up; not a processed video sample."""
    with Image.open(BytesIO(frame_bytes)) as original:
        image = original.convert("RGBA")
    width, height = image.size
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for i, (x, y, w, h) in enumerate(_mask_regions(
        width, height, mode, position_pct,
        left_y_pct=left_y_pct, right_y_pct=right_y_pct,
        mask_width_pct=mask_width_pct, mask_height_pct=mask_height_pct,
    )):
        draw.rectangle((x, y, x + w, y + h), fill=(255, 60, 20, 60),
                       outline=(255, 35, 20, 255), width=3)
        draw.text((x + 4, y + 4), "1" if i == 0 else "2", fill=(255, 255, 255, 255))
    image = Image.alpha_composite(image, overlay).convert("RGB")
    result = BytesIO()
    image.save(result, "JPEG", quality=83)
    return result.getvalue()


def clean_user_video(
    video_bytes: bytes,
    *,
    filename: str = "video.mp4",
    mask_mode: str = "both",
    position_pct: int = 82,
    left_y_pct: int | None = None,
    right_y_pct: int | None = None,
    mask_width_pct: int = 38,
    mask_height_pct: int = 16,
    trim_end_seconds: float = 0.0,
) -> tuple[bytes, dict]:
    """Return cleaned MP4 and its metadata. Never makes network/API calls.

    Does not add brand marks, change video dimensions, or publish anything.
    Does not claim to identify moving watermark pixels automatically.
    """
    if not isinstance(video_bytes, bytes) or not video_bytes:
        raise VideoCleanupError("Сначала загрузите видео.")
    if len(video_bytes) > MAX_VIDEO_BYTES:
        raise VideoCleanupError("Видео больше 120 МБ. Загрузите файл меньшего размера.")
    extension = Path(str(filename or "video.mp4")).suffix.lower()
    if extension not in {".mp4", ".mov", ".m4v"}:
        raise VideoCleanupError("Поддерживаются MP4, MOV и M4V.")
    if mask_mode not in ALLOWED_MASK_MODES:
        raise VideoCleanupError("Выберите расположение знака.")
    try:
        trim = float(trim_end_seconds)
    except (TypeError, ValueError) as exc:
        raise VideoCleanupError("Укажите корректное число секунд для обрезки.") from exc
    if not (0 <= trim <= 20):
        raise VideoCleanupError("Можно убрать не более 20 последних секунд.")

    with tempfile.TemporaryDirectory(prefix="agency_w_video_cleanup_") as tmp:
        root = Path(tmp)
        source = root / f"source{extension}"
        destination = root / "result.mp4"
        source.write_bytes(video_bytes)
        properties = _probe_video(source)
        final_seconds = properties["duration"] - trim
        if final_seconds < 1:
            raise VideoCleanupError("После обрезки должен остаться минимум 1 секунда видео.")
        filters = _delogo_filters(
            properties["width"], properties["height"], mask_mode, position_pct,
            left_y_pct=left_y_pct, right_y_pct=right_y_pct,
            mask_width_pct=mask_width_pct, mask_height_pct=mask_height_pct,
        )
        if mask_mode == "none" and trim == 0:
            raise VideoCleanupError("Выберите скрытие знака или обрезку конца.")
        cmd = [
            _binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin",
            "-y", "-i", str(source),
            "-map", "0:v:0", "-map", "0:a:0?", "-map_metadata", "-1",
            "-t", str(round(final_seconds, 3)),
        ]
        if filters:
            cmd += ["-vf", filters]
        cmd += [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart", "-threads", "2", str(destination),
        ]
        try:
            subprocess.run(
                cmd, check=True, capture_output=True, text=True,
                timeout=min(360, max(90, round(properties["duration"] * 3.5))),
            )
        except subprocess.TimeoutExpired as exc:
            raise VideoCleanupError(
                "Обработка заняла слишком много времени. Попробуйте более короткий ролик."
            ) from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or "")[-600:]
            raise VideoCleanupError(
                "Не удалось обработать этот видеоролик. Попробуйте обычный MP4. "
                + detail.replace("\n", " ")[:240]
            ) from exc
        if not destination.is_file() or destination.stat().st_size < 100:
            raise VideoCleanupError("После обработки не получился корректный файл MP4.")
        return destination.read_bytes(), {
            "original_duration": properties["duration"],
            "estimated_duration": final_seconds,
            "width": properties["width"], "height": properties["height"],
            "mask_mode": mask_mode,
            "mask_regions": _mask_regions(
                properties["width"], properties["height"], mask_mode, position_pct,
                left_y_pct=left_y_pct, right_y_pct=right_y_pct,
                mask_width_pct=mask_width_pct, mask_height_pct=mask_height_pct,
            ),
        }
