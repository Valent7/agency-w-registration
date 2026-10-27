"""The official Agency W corner mark, shared by local video exporters.

This module only processes local bytes. It neither orders media nor publishes
anything. Partner projects keep their own branding; a name containing "W" is
not sufficient to select the Agency's mark.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unicodedata

from PIL import Image


LOGO_PATH = Path(__file__).resolve().parent / "assets" / "agency_w_icon.png"
LOGO_VERSION = "agency-w-official-corner-v1"
MAX_VIDEO_BYTES = 100 * 1024 * 1024
MOV_FORMATS = "mov,mp4,m4a,3gp,3g2,mj2"


class VideoBrandingError(RuntimeError):
    """A local branding failure without media contents or provider URLs."""


def is_agency_w_brand(name):
    if not isinstance(name, str):
        return False
    name = " ".join(unicodedata.normalize("NFKC", name).casefold().split())
    return name in {"агентство w", "agency w", "агентствоw", "agencyw"}


def logo_layout(size):
    """A modest corner mark, outside the factory's lower subtitle region."""
    if (not isinstance(size, tuple) or len(size) != 2
            or any(type(value) is not int or not 120 <= value <= 4096 for value in size)
            or size[0] * size[1] > 9_000_000):
        raise VideoBrandingError("Неверный размер видео для фирменного знака.")
    short_side = min(size)
    edge = max(16, round(short_side * 144 / 1080))
    inset = max(6, round(short_side * 54 / 1080))
    return inset, inset, edge, edge


def _logo():
    try:
        with Image.open(LOGO_PATH) as source:
            if source.format != "PNG" or source.size != (512, 512):
                raise ValueError
            source.load()
            mark = source.convert("RGBA")
            if mark.getchannel("A").getextrema() != (0, 255):
                raise ValueError
            return mark
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        raise VideoBrandingError("Не найден оригинальный фирменный знак Агентства W.") from error


def check_video_logo(brand_name="Агентство W"):
    """Validate the official local PNG before any paid Agency W generation."""
    if not is_agency_w_brand(brand_name):
        return None
    with _logo():
        pass
    return LOGO_PATH


def prepare_video_logo(root, size, brand_name="Агентство W"):
    """Write a proportional transparent mark for an exporter's existing encode."""
    if not is_agency_w_brand(brand_name):
        return None
    _, _, width, height = logo_layout(size)
    path = Path(root) / "agency-w-official-mark.png"
    with _logo() as mark:
        mark.thumbnail((width, height), Image.Resampling.LANCZOS)
        mark.save(path, format="PNG")
    return path


def video_logo_filter(base_filters, after_filters, size, *, video_label="0:v", logo_label="1:v", output_label="branded"):
    """Overlay the static mark during a renderer's existing video encode.

    Filters and labels are supplied by local code, never interpolated owner
    text. Captions run afterwards and retain their original coordinates.
    The single PNG frame repeats as an overlay; the underlying video does not.
    """
    if any(not re.fullmatch(r"[A-Za-z0-9_:]+", label) for label in (video_label, logo_label, output_label)):
        raise VideoBrandingError("Неверная метка локального видеомонтажа.")
    x, y, _, _ = logo_layout(size)
    tail = "," + after_filters if after_filters else ""
    return (f"[{video_label}]{base_filters or 'null'}[agency_w_base];"
            f"[agency_w_base][{logo_label}]overlay=x={x}:y={y}:eof_action=repeat:"
            f"repeatlast=1:shortest=0:format=auto{tail}[{output_label}]")


def video_branding_proof(video_bytes, brand_name="Агентство W"):
    """Version metadata to bind a known branded export to its final bytes.

    Call only after applying the mark, or after a renderer integrated the same
    overlay. This metadata is not a detector for arbitrary uploaded videos.
    """
    if not is_agency_w_brand(brand_name):
        return None
    check_video_logo(brand_name)
    if not isinstance(video_bytes, bytes) or not 0 < len(video_bytes) <= MAX_VIDEO_BYTES:
        raise VideoBrandingError("Готовое видео пустое или превышает 100 МБ.")
    return {"version": LOGO_VERSION, "logo_filename": LOGO_PATH.name,
            "logo_sha256": hashlib.sha256(LOGO_PATH.read_bytes()).hexdigest(),
            "asset_sha256": hashlib.sha256(video_bytes).hexdigest()}


def branding_proof_matches(proof, asset_sha256):
    """Fail closed when an export's mark metadata is stale or detached."""
    required = {"version", "logo_filename", "logo_sha256", "asset_sha256"}
    if not isinstance(proof, dict) or set(proof) != required:
        return False
    if any(type(value) is not str for value in proof.values()):
        return False
    if (not isinstance(asset_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", asset_sha256)
            or not re.fullmatch(r"[0-9a-f]{64}", proof["logo_sha256"])
            or not re.fullmatch(r"[0-9a-f]{64}", proof["asset_sha256"])
            or proof["version"] != LOGO_VERSION or proof["logo_filename"] != LOGO_PATH.name
            or proof["asset_sha256"] != asset_sha256):
        return False
    try:
        check_video_logo()
        return proof["logo_sha256"] == hashlib.sha256(LOGO_PATH.read_bytes()).hexdigest()
    except (VideoBrandingError, OSError):
        return False


def _probe(path):
    try:
        result = subprocess.run([
            shutil.which("ffprobe"), "-v", "error", "-protocol_whitelist", "file,pipe",
            "-format_whitelist", MOV_FORMATS, "-show_streams", "-show_format", "-of", "json", path.name,
        ], cwd=path.parent, capture_output=True, check=True, timeout=30)
        data = json.loads(result.stdout)
        stream = next(item for item in data["streams"] if item.get("codec_type") == "video")
        size = int(stream["width"]), int(stream["height"])
        logo_layout(size)
        duration = float(data["format"]["duration"])
        if not math.isfinite(duration) or not 0 < duration <= 3600:
            raise ValueError
        return data, size, duration
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError, StopIteration):
        raise VideoBrandingError("Не удалось проверить исходное видео для фирменного знака.") from None


def apply_agency_w_logo(video_bytes, *, brand_name="Агентство W"):
    """Mark an existing MP4 without changing its size, pace, length or audio.

    Video alone is encoded; all existing audio packets are copied. There are
    no intro/outro frames, audio filters, time cutoffs, or remote media inputs.
    """
    if not is_agency_w_brand(brand_name):
        return video_bytes
    if not isinstance(video_bytes, bytes) or not 0 < len(video_bytes) <= MAX_VIDEO_BYTES:
        raise VideoBrandingError("Готовое видео пустое или превышает 100 МБ.")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not shutil.which("ffprobe"):
        raise VideoBrandingError("Для фирменного знака нужны FFmpeg и ffprobe.")
    with tempfile.TemporaryDirectory(prefix="agency_w_video_mark_") as folder:
        root = Path(folder)
        source = root / "original.mp4"
        source.write_bytes(video_bytes)
        details, size, duration = _probe(source)
        logo = prepare_video_logo(root, size, brand_name)
        target = root / "branded.mp4"
        try:
            subprocess.run([
                ffmpeg, "-y", "-loglevel", "error", "-filter_complex_threads", "1",
                "-protocol_whitelist", "file,pipe", "-format_whitelist", MOV_FORMATS,
                "-threads", "1", "-i", source.name,
                "-protocol_whitelist", "file,pipe", "-format_whitelist", "png_pipe", "-i", logo.name,
                "-filter_complex", video_logo_filter("null", "format=yuv420p", size),
                "-map", "[branded]", "-map", "0:a?", "-map_metadata", "0", "-map_chapters", "0",
                "-c:v", "libx264", "-threads", "1", "-preset", "veryfast", "-crf", "20",
                "-c:a", "copy", "-fps_mode", "passthrough", "-movflags", "+faststart", target.name,
            ], cwd=root, capture_output=True, check=True, timeout=max(120, math.ceil(duration * 10)))
            if not target.is_file() or not 0 < target.stat().st_size <= MAX_VIDEO_BYTES:
                raise VideoBrandingError("Видео с фирменным знаком отсутствует или превышает 100 МБ.")
            output_info, output_size, output_duration = _probe(target)
            if output_size != size or abs(output_duration - duration) > 0.05:
                raise VideoBrandingError("Фирменный знак изменил размер или длительность видео.")
            before = next(item for item in details["streams"] if item.get("codec_type") == "video")
            after = next(item for item in output_info["streams"] if item.get("codec_type") == "video")
            if before.get("nb_frames") and after.get("nb_frames") and before["nb_frames"] != after["nb_frames"]:
                raise VideoBrandingError("Фирменный знак изменил число кадров видео.")
            return target.read_bytes()
        except (OSError, subprocess.SubprocessError):
            raise VideoBrandingError("Не удалось добавить оригинальный фирменный знак без изменения звука.") from None
