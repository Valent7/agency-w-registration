"""Assemble generated moving footage at its original playback speed.

This renderer accepts real MP4 clips, never illustrations. It trims each clip
without stretching, looping or padding its video. Encoding is sequential and
single-threaded to fit the factory's small background worker.
"""
from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

from agency_video_branding import (
    VideoBrandingError, check_video_logo, is_agency_w_brand, prepare_video_logo, video_logo_filter,
)
from agency_outro import append_agency_outro
from content_factory_media import _ass_time, _caption_events, _fit, _write_scene_captions, MediaError


class LiveMediaError(RuntimeError):
    """A bounded error that never includes file contents or provider URLs."""


FPS = 24
END_PADDING = 0.35
MAX_CLIP_BYTES = 25 * 1024 * 1024
MAX_TOTAL_CLIP_BYTES = 100 * 1024 * 1024
MOV_FORMATS = "mov,mp4,m4a,3gp,3g2,mj2"
AUDIO_FORMATS = "mp3,wav," + MOV_FORMATS


def _tools():
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise LiveMediaError("Для монтажа живого видео нужны FFmpeg и ffprobe.")
    return ffmpeg, ffprobe


def _text(value, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise LiveMediaError("Текст оформления живого видео имеет неверный формат.")
    return value.strip()


def _limits(target_seconds):
    # This is an explicit production contract, not a reason to relax limits
    # for existing short-Reel callers or malformed saved state.
    if type(target_seconds) is not int or target_seconds not in {45, 120}:
        raise LiveMediaError("Выберите короткий ролик или видео до двух минут.")
    return (119.5, 16) if target_seconds == 120 else (59.5, 6)


def _duration(value, *, target_seconds=45):
    maximum, _ = _limits(target_seconds)
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not 0.3 <= value <= maximum:
        limit = "119,5" if target_seconds == 120 else "59,5"
        raise LiveMediaError(f"Озвучка должна длиться от 0,3 до {limit} секунды.")
    return float(value)


def _speech(value):
    if not isinstance(value, bytes) or not value or len(value) > 16 * 1024 * 1024:
        raise LiveMediaError("Озвучка отсутствует или превышает допустимый размер.")
    return value


def _probe(ffprobe, path, formats):
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-protocol_whitelist", "file,pipe",
             "-format_whitelist", formats, "-show_streams", "-show_format", "-of", "json", path.name],
            cwd=path.parent, capture_output=True, timeout=20, check=True,
        )
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
        if not math.isfinite(duration) or duration <= 0 or not isinstance(data.get("streams"), list):
            raise ValueError
        return data, duration
    except (subprocess.SubprocessError, OSError, ValueError, TypeError, KeyError):
        raise LiveMediaError("Не удалось проверить исходный медиафайл живого видео.") from None


def _audio_info(ffprobe, path, *, target_seconds=45):
    data, duration = _probe(ffprobe, path, AUDIO_FORMATS)
    audio = next((stream for stream in data["streams"] if stream.get("codec_type") == "audio"), None)
    if audio is None:
        raise LiveMediaError("В файле озвучки нет звуковой дорожки.")
    return _duration(duration, target_seconds=target_seconds), audio.get("codec_name")


def _audio_duration(ffprobe, path, *, target_seconds=45):
    return _audio_info(ffprobe, path, target_seconds=target_seconds)[0]


def speech_duration(speech_bytes, *, target_seconds=45):
    """Measure local MP3/WAV or M4A/AAC before requesting moving footage."""
    _limits(target_seconds)
    _speech(speech_bytes)
    _, ffprobe = _tools()
    with tempfile.TemporaryDirectory(prefix="factory_voice_probe_") as folder:
        path = Path(folder) / "speech.audio"
        path.write_bytes(speech_bytes)
        return _audio_duration(ffprobe, path, target_seconds=target_seconds)


def scene_durations(duration, frame_count, *, target_seconds=45):
    """Return bounded Runway Gen-4.5 requests covering voice plus its tail."""
    duration = _duration(duration, target_seconds=target_seconds)
    _, maximum = _limits(target_seconds)
    if type(frame_count) is not int or not 3 <= frame_count <= maximum:
        limit = "шестнадцати" if target_seconds == 120 else "шести"
        raise LiveMediaError(f"Для живого ролика нужны от трёх до {limit} сцен.")
    seconds = max(2, math.ceil((duration + END_PADDING) / frame_count))
    if seconds > 10:
        raise LiveMediaError("Для этой озвучки нужно больше живых сцен длительностью до 10 секунд.")
    return [seconds] * frame_count


def _ass_text(value):
    return value.replace("\\", "＼").replace("{", "｛").replace("}", "｝").replace("\r", "").replace("\n", "\\N")


def _scene_captions(path, events, start, end, title, brand_name, brand_color):
    _write_scene_captions(path, events, start, end)
    text = path.read_text(encoding="utf-8")
    color = "&H00" + brand_color[5:7] + brand_color[3:5] + brand_color[1:3]
    title_margin = 218 if is_agency_w_brand(brand_name) else 150
    styles = (
        f"Style: Brand,DejaVu Sans,28,{color},{color},&H00121212,&H60000000,-1,0,0,0,100,100,0,0,1,2,1,7,54,54,48,1\n"
        f"Style: Title,DejaVu Sans,56,&H00FFFFFF,&H00FFFFFF,&H00121212,&H60000000,-1,0,0,0,100,100,0,0,1,3,1,8,90,90,{title_margin},1\n"
    )
    text = text.replace("[Events]\n", styles + "[Events]\n", 1)
    last = _ass_time(end - start)
    if not is_agency_w_brand(brand_name):
        text += f"Dialogue: 1,0:00:00.00,{last},Brand,,0,0,0,,{_ass_text(brand_name)}\n"
    if title:
        measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        try:
            font, lines, _ = _fit(measure, title, 900, 260, start=56, minimum=32)
        except MediaError:
            raise LiveMediaError("Заголовок не помещается в оформлении живого видео.") from None
        # The override contains a numeric font size generated locally. Owner
        # text is escaped separately, so it cannot inject ASS instructions.
        wrapped = "\\N".join(_ass_text(line) for line in lines)
        text += f"Dialogue: 1,0:00:00.00,{last},Title,,0,0,0,,{{\\fs{font.size}}}{wrapped}\n"
    path.write_text(text, encoding="utf-8")


def render_live_reel(
    scene_clips, speech_bytes, narration, title, brand_name, brand_color, *,
    target_seconds=45, intro_image_bytes=None, outro_image_bytes=None, music_bytes=None,
    voice_volume=1.0, music_volume=0.12, apply_finishing=False,
):
    """Make a live reel and finish Agency W films with selected branding/audio."""
    _, maximum = _limits(target_seconds)
    if not isinstance(scene_clips, list) or not 3 <= len(scene_clips) <= maximum:
        limit = "шестнадцати" if target_seconds == 120 else "шести"
        raise LiveMediaError(f"Для живого видео нужны от трёх до {limit} готовых видеосцен.")
    if any(not isinstance(clip, bytes) or not clip or len(clip) > MAX_CLIP_BYTES for clip in scene_clips):
        raise LiveMediaError("Видеосцена отсутствует или превышает размер 25 МБ.")
    if sum(map(len, scene_clips)) > MAX_TOTAL_CLIP_BYTES:
        raise LiveMediaError("Общий размер видеосцен превышает 100 МБ.")
    _speech(speech_bytes)
    narration, title, brand_name = _text(narration, 4000 if target_seconds == 120 else 3000), _text(title, 150), _text(brand_name, 120)
    if not isinstance(brand_color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", brand_color):
        raise LiveMediaError("Цвет оформления должен иметь формат #RRGGBB.")
    ffmpeg, ffprobe = _tools()
    with tempfile.TemporaryDirectory(prefix="factory_live_reel_") as folder:
        root = Path(folder)
        speech = root / "speech.audio"
        speech.write_bytes(speech_bytes)
        duration, audio_codec = _audio_info(ffprobe, speech, target_seconds=target_seconds)
        total_frames = math.ceil((duration + END_PADDING) * FPS)
        base, extra = divmod(total_frames, len(scene_clips))
        counts = [base + (index < extra) for index in range(len(scene_clips))]
        events = _caption_events(narration, duration)
        try:
            logo = prepare_video_logo(root, (1080, 1920), brand_name)
        except VideoBrandingError as error:
            raise LiveMediaError(str(error)) from None
        # Probe every clip before encoding any output. A short clip is a failed
        # input, never permission to repeat its last frame or slow its movement.
        for index, (clip, count) in enumerate(zip(scene_clips, counts)):
            source = root / f"source-{index}.mp4"
            source.write_bytes(clip)
            data, clip_duration = _probe(ffprobe, source, MOV_FORMATS)
            video = next((s for s in data["streams"] if s.get("codec_type") == "video"), None)
            try:
                if not video or not 0 < int(video["width"]) * int(video["height"]) <= 9_000_000:
                    raise ValueError
                if video.get("nb_frames") not in (None, "N/A") and int(video["nb_frames"]) < 2:
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                raise LiveMediaError("Исходная сцена не содержит подходящую движущуюся видеодорожку.") from None
            if clip_duration + 0.002 < count / FPS:
                raise LiveMediaError("Одна из живых сцен короче нужного фрагмента. Зацикливание и заморозка отключены.")

        cursor = 0.0
        for index, count in enumerate(counts):
            seconds = count / FPS
            subtitle = root / f"captions-{index}.ass"
            _scene_captions(subtitle, events, cursor, cursor + seconds, title if index == 0 else "", brand_name, brand_color)
            base_filters = (
                "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
                f"fps={FPS},setpts=PTS-STARTPTS"
            )
            lettering_filters = f"subtitles={subtitle.name},format=yuv420p"
            inputs = (["-protocol_whitelist", "file,pipe", "-format_whitelist", "png_pipe", "-i", logo.name]
                      if logo else [])
            filters = (["-filter_complex", video_logo_filter(base_filters, lettering_filters, (1080, 1920)),
                        "-map", "[branded]"] if logo else ["-vf", base_filters + "," + lettering_filters])
            try:
                subprocess.run(
                    [ffmpeg, "-y", "-loglevel", "error", "-filter_threads", "1", "-filter_complex_threads", "1",
                     "-protocol_whitelist", "file,pipe", "-format_whitelist", MOV_FORMATS,
                     "-i", f"source-{index}.mp4", *inputs, *filters, "-frames:v", str(count),
                     "-an", "-c:v", "libx264", "-threads", "1", "-preset", "veryfast",
                     "-crf", "21", "-pix_fmt", "yuv420p", f"scene-{index}.mp4"],
                    cwd=root, capture_output=True, timeout=240, check=True,
                )
                encoded, _ = _probe(ffprobe, root / f"scene-{index}.mp4", MOV_FORMATS)
                stream = next(s for s in encoded["streams"] if s.get("codec_type") == "video")
                if int(stream.get("nb_frames", 0)) != count:
                    raise LiveMediaError("Живая сцена закончилась раньше нужного момента; подмена статичным кадром отключена.")
            except (subprocess.SubprocessError, OSError, ValueError, KeyError, StopIteration):
                raise LiveMediaError("Не удалось подготовить живую сцену без изменения её скорости.") from None
            cursor += seconds
        # Some MP4 headers omit the last frame interval from their duration.
        # Explicit frame-derived offsets prevent adjacent clips from overlapping.
        (root / "scenes.txt").write_text("".join(
            f"file 'scene-{index}.mp4'\nduration {count / FPS:.9f}\n"
            for index, count in enumerate(counts)), encoding="utf-8")
        try:
            # Imported AAC is already the original soundtrack. Copy its encoded
            # packets unchanged; the video may outlast it by END_PADDING. MP3
            # and WAV speech retain the existing AAC encoding and audio pad.
            audio_options = ["-c:a", "copy"] if audio_codec == "aac" else [
                "-c:a", "aac", "-b:a", "128k", "-af", "apad",
            ]
            # AAC is finite and needs no output time cutoff. Let the copied
            # video reach its natural end, including reordered final frames.
            # Only the apad branch needs -t to bound its synthetic audio tail.
            length_options = [] if audio_codec == "aac" else ["-t", f"{total_frames / FPS:.6f}"]
            subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "1",
                 "-protocol_whitelist", "file,pipe", "-i", "scenes.txt",
                 "-protocol_whitelist", "file,pipe", "-format_whitelist", AUDIO_FORMATS, "-i", speech.name,
                 "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", *audio_options,
                 "-threads", "1", *length_options,
                 "-movflags", "+faststart", "live-reel.mp4"],
                cwd=root, capture_output=True, timeout=120, check=True,
            )
            output = root / "live-reel.mp4"
            if not output.is_file() or not 0 < output.stat().st_size <= 100 * 1024 * 1024:
                raise LiveMediaError("Готовое живое видео отсутствует или превышает 100 МБ.")
            result = output.read_bytes()
            if apply_finishing and is_agency_w_brand(brand_name):
                try:
                    result = append_agency_outro(
                        result, logo_path=check_video_logo(brand_name), size=(1080, 1920), title=title,
                        intro_image_bytes=intro_image_bytes, outro_image_bytes=outro_image_bytes,
                        music_bytes=music_bytes, voice_volume=voice_volume, music_volume=music_volume,
                    )
                except (RuntimeError, ValueError, VideoBrandingError) as exc:
                    raise LiveMediaError(str(exc)) from None
            return result
        except (subprocess.SubprocessError, OSError):
            raise LiveMediaError("Не удалось соединить живые сцены и озвучку.") from None
