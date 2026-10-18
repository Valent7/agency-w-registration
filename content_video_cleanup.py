"""Private, on-demand MP4 clean-up for user-supplied, authorized short videos.

No TikTok/API access, no downloading third-party posts, no third-party upload.
FFmpeg delogo reconstructs pixels locally; effectiveness depends on background.
"""
from __future__ import annotations

import json
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


def _delogo_filters(width: int, height: int, mode: str, position_pct: int) -> str:
    if mode not in ALLOWED_MASK_MODES:
        raise VideoCleanupError("Неизвестный вариант положения логотипа.")
    if not isinstance(position_pct, int) or not (10 <= position_pct <= 90):
        raise VideoCleanupError("Положение логотипа должно быть от 10 до 90 процентов.")
    if mode == "none":
        return ""
    # For a portrait watermark and @handle. Keep the masks within the frame.
    mask_w = min(round(width * 0.25), width - 8)
    mask_h = min(round(height * 0.14), height - 8)
    pad = max(2, round(width * 0.012))
    center_y = round(height * position_pct / 100)
    top = max(2, min(height - mask_h - 2, center_y - mask_h // 2))
    left_x = max(2, pad)
    right_x = max(2, width - mask_w - pad)
    selected = []
    if mode in ("both", "left"):
        selected.append(left_x)
    if mode in ("both", "right"):
        selected.append(right_x)
    return ",".join(
        f"delogo=x={x}:y={top}:w={mask_w}:h={mask_h}:show=0"
        for x in selected
    )


def clean_user_video(
    video_bytes: bytes,
    *,
    filename: str = "video.mp4",
    mask_mode: str = "both",
    position_pct: int = 82,
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
        }
