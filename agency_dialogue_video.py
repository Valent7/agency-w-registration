"""Two-speaker Agency W video-call Reels built from ONE HeyGen multi-scene job.

No paid API requests occur on import, preview, editing, or configuration checks.
Only create_dialogue_video submits a billable video, after explicit approval.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Any

import requests
from PIL import Image, ImageDraw, ImageFont

SPEAKER_LABELS = {"A": "Мужчина", "B": "Женщина"}
SPEAKER_ALIASES = {
    "а": "A", "a": "A", "мужчина": "A", "он": "A",
    "б": "B", "b": "B", "женщина": "B", "она": "B",
}
HEYGEN_GENERATE_URL = "https://api.heygen.com/v2/video/generate"
HEYGEN_AVATARS_URL = "https://api.heygen.com/v2/avatars"
HEYGEN_VOICES_URL = "https://api.heygen.com/v2/voices"
MAX_TURNS = 10


def _speaker(value: Any) -> str:
    raw = str(value or "").strip()
    return SPEAKER_ALIASES.get(raw.lower(), "")


def normalise_dialogue_turns(value: Any) -> list[dict[str, str]]:
    """Never silently truncate dialogue, which could cut the last spoken line."""
    if not isinstance(value, list):
        return []
    turns: list[dict[str, str]] = []
    for raw in value:
        if not isinstance(raw, dict):
            return []
        speaker = _speaker(raw.get("speaker") or raw.get("role"))
        text = str(raw.get("text") or "").strip()
        text = re.sub(r"\s+", " ", text)
        if not speaker or not text:
            return []
        turns.append({"speaker": speaker, "text": text})
    return turns


def dialogue_text(turns: list[dict[str, str]]) -> str:
    return "\n".join(
        f"{SPEAKER_LABELS.get(t.get('speaker'), 'Герой')}: {t.get('text') or ''}"
        for t in turns
    )


def parse_dialogue_editor(text: str) -> list[dict[str, str]]:
    result = []
    for line in str(text or "").splitlines():
        if not line.strip():
            continue
        match = re.match(r"^\s*([^:：]{1,35})\s*[:：]\s*(.+?)\s*$", line)
        if match is None:
            raise ValueError("Каждая реплика должна начинаться с «Мужчина:» или «Женщина:».")
        speaker = _speaker(match.group(1))
        if not speaker:
            raise ValueError("Используйте только героев «Мужчина» и «Женщина».")
        result.append({"speaker": speaker, "text": match.group(2).strip()})
    return result


def dialogue_errors(value: Any) -> list[str]:
    turns = normalise_dialogue_turns(value)
    if not turns:
        return ["Нужен диалог из отдельных реплик двух героев."]
    errors: list[str] = []
    if len(turns) < 4 or len(turns) > MAX_TURNS:
        errors.append("Для живого диалога нужно от 4 до 10 реплик.")
    if turns[0]["speaker"] != "A":
        errors.append("Начните с вопроса мужчины: он вводит зрителя в ситуацию.")
    if {turn["speaker"] for turn in turns} != {"A", "B"}:
        errors.append("Должны говорить оба героя.")
    for index, turn in enumerate(turns):
        if turn["speaker"] != ("A" if index % 2 == 0 else "B"):
            errors.append("Реплики должны чередоваться: мужчина, женщина, мужчина, женщина.")
            break
        if len(turn["text"]) > 280:
            errors.append(f"Реплика {index+1} слишком длинная: максимум 280 символов.")
    words = len(re.findall(r"\b[\wЁёА-Яа-я-]+\b", " ".join(t["text"] for t in turns)))
    if words < 38 or words > 140:
        errors.append(
            f"В диалоге {words} слов. Для Reels оставьте приблизительно 38–140 слов."
        )
    for turn in turns:
        if re.search(r"\b(сцена|камера|крупный план|кадр|переключение камеры)\s*[:—-]", turn["text"], re.I):
            errors.append("Режиссёрские команды нельзя произносить как реплики.")
            break
    return errors


def validate_cast(cast: dict) -> list[str]:
    errors: list[str] = []
    for speaker in ("A", "B"):
        item = cast.get(speaker)
        if not isinstance(item, dict) or not str(item.get("avatar_id") or "").strip():
            errors.append(f"Для героя «{SPEAKER_LABELS[speaker]}» не выбран аватар HeyGen.")
        if not isinstance(item, dict) or not str(item.get("voice_id") or "").strip():
            errors.append(f"Для героя «{SPEAKER_LABELS[speaker]}» не выбран голос HeyGen.")
    if not errors:
        if str(cast["A"]["avatar_id"]).strip() == str(cast["B"]["avatar_id"]).strip():
            errors.append("Для диалога нужны два разных аватара.")
        if str(cast["A"]["voice_id"]).strip() == str(cast["B"]["voice_id"]).strip():
            errors.append("Для диалога нужны два разных голоса.")
    return errors


def build_heygen_dialogue_payload(
    turns: list[dict[str, str]],
    cast: dict,
    *,
    is_test: bool = True,
    title: str = "Агентство W — ИИ-диалог",
) -> dict:
    errors = dialogue_errors(turns) + validate_cast(cast)
    if errors:
        raise ValueError("\n".join(errors))
    video_inputs = []
    for turn in turns:
        speaker = turn["speaker"]
        actor = cast[speaker]
        video_inputs.append({
            "character": {
                "type": "avatar",
                "avatar_id": str(actor["avatar_id"]).strip(),
                "avatar_style": "closeUp",
            },
            "voice": {
                "type": "text",
                "input_text": turn["text"],
                "voice_id": str(actor["voice_id"]).strip(),
                "speed": 1.0,
            },
            "background": {
                "type": "color",
                "value": "#142839" if speaker == "A" else "#E9E1D6",
            },
        })
    return {
        "video_inputs": video_inputs,
        "dimension": {"width": 720, "height": 1280},
        "title": str(title or "")[:115],
        "test": bool(is_test),
        "caption": False,
    }


def _api_error(response: requests.Response) -> str:
    try:
        data = response.json()
        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(error, dict):
            return str(error.get("message") or error.get("detail") or "Ошибка HeyGen.")[:400]
        return str((data or {}).get("message") or f"HeyGen HTTP {response.status_code}")[:400]
    except (ValueError, TypeError):
        return f"HeyGen HTTP {response.status_code}"


def create_dialogue_video(api_key: str, turns: list[dict], cast: dict, *, is_test: bool = True, title: str = "") -> dict:
    """Exactly one HeyGen generation request, NEVER per utterance."""
    if not str(api_key or "").strip():
        return {"ok": False, "error": "Не найден HEYGEN_API_KEY в Streamlit Secrets."}
    try:
        payload = build_heygen_dialogue_payload(turns, cast, is_test=is_test, title=title)
    except (ValueError, TypeError) as exc:
        return {"ok": False, "error": str(exc)}
    try:
        response = requests.post(
            HEYGEN_GENERATE_URL,
            headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
            json=payload,
            timeout=90,
        )
        if not response.ok:
            return {"ok": False, "error": _api_error(response)}
        body = response.json() if response.content else {}
        data = body.get("data") if isinstance(body, dict) else {}
        video_id = str((data or {}).get("video_id") or "").strip()
        if not video_id:
            return {"ok": False, "error": "HeyGen не вернул ID видео. Повторять платный запрос автоматически нельзя."}
        return {"ok": True, "video_id": video_id, "status": "pending", "test": bool(is_test)}
    except requests.RequestException as exc:
        return {"ok": False, "error": f"Ошибка связи с HeyGen: {type(exc).__name__}. Проверьте задачу в HeyGen перед повтором."}
    except (ValueError, TypeError):
        return {"ok": False, "error": "HeyGen вернул непонятный ответ. Проверьте статус в HeyGen перед повтором."}


def list_heygen_catalog(api_key: str) -> dict:
    """Read-only catalog lookup; may run only when user presses 'Load catalog'."""
    if not str(api_key or "").strip():
        return {"ok": False, "error": "HEYGEN_API_KEY не настроен."}
    try:
        avatars_response = requests.get(
            HEYGEN_AVATARS_URL, headers={"X-Api-Key": api_key}, timeout=30
        )
        voices_response = requests.get(
            HEYGEN_VOICES_URL, headers={"X-Api-Key": api_key}, timeout=30
        )
        if not avatars_response.ok:
            return {"ok": False, "error": "Аватары: " + _api_error(avatars_response)}
        if not voices_response.ok:
            return {"ok": False, "error": "Голоса: " + _api_error(voices_response)}
        avatars_data = avatars_response.json().get("data") or {}
        voices_data = voices_response.json().get("data") or {}
        avatars = avatars_data.get("avatars") or []
        voices = voices_data.get("voices") or []
        return {
            "ok": True,
            "avatars": [
                {"id": str(a.get("avatar_id") or "").strip(), "name": str(a.get("avatar_name") or a.get("name") or "Персонаж")[:90]}
                for a in avatars if isinstance(a, dict) and a.get("avatar_id")
            ],
            "voices": [
                {"id": str(v.get("voice_id") or "").strip(), "name": str(v.get("name") or "Голос")[:90],
                 "language": str(v.get("language") or v.get("language_code") or "")[:60],
                 "gender": str(v.get("gender") or "")[:32]}
                for v in voices if isinstance(v, dict) and v.get("voice_id")
            ],
        }
    except (ValueError, TypeError, requests.RequestException) as exc:
        return {"ok": False, "error": f"Не удалось получить каталог HeyGen: {type(exc).__name__}."}


def _font(px: int) -> ImageFont.ImageFont:
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ):
        if Path(path).exists():
            return ImageFont.truetype(path, px)
    return ImageFont.load_default()


def _call_overlay(logo_path: Path | None = None) -> bytes:
    """Transparent call controls; no fake microphone/video interactions."""
    image = Image.new("RGBA", (720, 1280), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((26, 26, 694, 110), radius=24, fill=(10, 25, 39, 213))
    draw.text((53, 53), "Видеовстреча  •  Агентство W", font=_font(28), fill=(250, 240, 215, 255))
    draw.rounded_rectangle((130, 1150, 590, 1251), radius=48, fill=(15, 26, 39, 230))
    for x, label, color in (
        (226, "●", (237, 240, 245, 235)),
        (360, "▣", (237, 240, 245, 235)),
        (494, "●", (233, 72, 74, 255)),
    ):
        draw.ellipse((x-34, 1163, x+34, 1230), fill=(55, 69, 85, 225) if x != 494 else (188, 48, 52, 255))
        draw.text((x-14, 1176), label, font=_font(29), fill=color)
    if logo_path and logo_path.is_file():
        try:
            mark = Image.open(logo_path).convert("RGBA")
            mark.thumbnail((92, 92))
            image.alpha_composite(mark, (590, 137))
        except (OSError, ValueError):
            pass
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def assemble_dialogue_mp4(video_bytes: bytes, *, logo_path: Path | None = None) -> bytes:
    """Draw video-call chrome on existing spoken HeyGen video, preserving its audio.

    Avoid duration trimming so the final syllable is not cut off.
    """
    if not video_bytes:
        raise ValueError("Видео HeyGen пустое.")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("На сервере нет ffmpeg. MP4 HeyGen можно скачать без оформления.")
    with tempfile.TemporaryDirectory(prefix="agency_w_dialogue_") as folder:
        tmp = Path(folder)
        raw = tmp / "heygen_dialogue.mp4"
        overlay = tmp / "ui.png"
        out = tmp / "agency_w_dialogue.mp4"
        raw.write_bytes(video_bytes)
        overlay.write_bytes(_call_overlay(logo_path))
        cmd = [
            ffmpeg, "-y", "-i", str(raw), "-loop", "1", "-i", str(overlay),
            "-filter_complex",
            "[0:v]scale=720:1280:force_original_aspect_ratio=increase,"
            "crop=720:1280,fps=30[base];[base][1:v]overlay=0:0:shortest=1[v]",
            "-map", "[v]", "-map", "0:a:0",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", str(out),
        ]
        result = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=360, check=False,
        )
        if result.returncode != 0 or not out.is_file() or out.stat().st_size < 2048:
            raise RuntimeError(
                "Не удалось наложить оформление видеозвонка на MP4. "
                + result.stderr.decode("utf-8", errors="ignore")[-600:]
            )
        return out.read_bytes()
