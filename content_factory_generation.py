"""Headless AI copy, illustrations and narrated social video generation.

This module never publishes, imports Streamlit, or retries paid requests.
The caller persists the returned bytes before scheduling publication.
"""
from __future__ import annotations

import base64
import binascii
import copy
import io
import json
import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps


class GenerationError(RuntimeError):
    """A generation step failed; the error is safe to show without credentials."""

    def __init__(self, message, *, draft=None, provider_details=None):
        super().__init__(message)
        self.draft = draft
        self.provider_details = provider_details


TEXT_LIMITS = {"instagram": 2200, "vk": 16000, "threads": 500, "telegram": 1024}
FORMATS = {"post", "carousel", "reel"}
_FIELDS = ("name", "product", "audience", "language", "tone", "goal", "website", "cta",
           "facts", "examples", "visual_style", "avoid", "video_mode")


def can_resume_video(production):
    """Recognize durable inputs without authorizing another creation request."""
    if not isinstance(production, dict) or type(production.get("version")) is not int or production["version"] != 1 or not isinstance(production.get("phase"), str) or production["phase"] not in {"inputs_ready", "clips", "complete"}:
        return False
    sources = production.get("raw_sources")
    count = len(sources) if isinstance(sources, list) else 0
    if not 4 <= count <= 6 or any(not isinstance(value, dict) or not value for value in sources):
        return False
    if not isinstance(production.get("speech_source"), dict) or not production["speech_source"]:
        return False
    if not isinstance(production.get("content"), dict):
        return False
    review = production["content"].get("quality_review")
    if not isinstance(review, dict) or review.get("approved") is not True:
        return False
    try:
        validate_content(production["content"], "reel", "vk")
    except GenerationError:
        return False
    try:
        if not math.isfinite(float(production["duration"])) or not 0.3 <= float(production["duration"]) <= 59.5:
            return False
    except (TypeError, ValueError, KeyError):
        return False
    seconds, tasks, clips = (production.get(key) for key in ("scene_seconds", "tasks", "clips"))
    if any(not isinstance(value, list) or len(value) != count for value in (seconds, tasks, clips)):
        return False
    if any(type(value) is not int or not 2 <= value <= 10 for value in seconds):
        return False
    minimum_seconds = math.ceil((float(production["duration"]) + 0.35) / count)
    if any(value < minimum_seconds for value in seconds):
        return False
    if any(value is not None and (not isinstance(value, str) or not value or len(value) > 200) for value in tasks):
        return False
    if any(value is not None and (not isinstance(value, dict) or not value) for value in clips):
        return False
    starting = production.get("starting_index")
    if starting is not None and (type(starting) is not int or not 0 <= starting < count):
        return False
    failed = production.get("failed_indices", [])
    return isinstance(failed, list) and all(type(index) is int and 0 <= index < count for index in failed)


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for path in (Path("/usr/share/fonts/truetype/dejavu") / name, Path(name)):
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            continue
    raise GenerationError("Не найден шрифт DejaVu Sans для оформления материалов.")


def _color(value: Any) -> str:
    text = str(value or "#D4AF37")
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", text):
        raise GenerationError("Фирменный цвет должен иметь формат #RRGGBB.")
    return text


def _text(value: Any, field: str, maximum: int, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise GenerationError(f"ИИ вернул неверное поле «{field}».")
    value = value.strip()
    if (required and not value) or len(value) > maximum or "\x00" in value:
        raise GenerationError(f"ИИ вернул пустое или слишком длинное поле «{field}».")
    return value


def validate_content(data: Any, content_format: str, platform: str) -> dict:
    """Validate the whole copy before any paid image or speech request."""
    if not isinstance(data, dict):
        raise GenerationError("ИИ вернул материал в неверном формате.")
    if content_format not in FORMATS or platform not in TEXT_LIMITS:
        raise GenerationError("Неизвестный формат или площадка.")
    result = {
        "format": content_format,
        "title": _text(data.get("title"), "заголовок", 150),
        "text": _text(data.get("text"), "текст публикации", TEXT_LIMITS[platform]),
    }
    if content_format == "carousel":
        slides = data.get("slides")
        if not isinstance(slides, list) or not 3 <= len(slides) <= 6:
            raise GenerationError("Для карусели нужны от трёх до шести слайдов.")
        result["slides"] = [_text(slide, "текст слайда", 400) for slide in slides]
        if len(set(result["slides"])) != len(result["slides"]):
            raise GenerationError("ИИ повторил одинаковые слайды карусели.")
    if content_format == "reel":
        narration = _text(data.get("narration"), "речь диктора", 1200)
        if not 45 <= len(narration.split()) <= 85:
            raise GenerationError("Для короткого видео нужна речь длиной от 45 до 85 слов.")
        if re.search(r"(?im)^\s*(сцена|кадр|камера|scene|camera)\s*\d*\s*[:—-]", narration):
            raise GenerationError("В речь диктора попали технические указания.")
        result["narration"] = narration
    return result


def _image(data: bytes) -> Image.Image:
    if not data or len(data) > 20 * 1024 * 1024:
        raise GenerationError("ИИ вернул пустое или слишком большое изображение.")
    try:
        with Image.open(io.BytesIO(data)) as source:
            if source.width * source.height > 25_000_000:
                raise GenerationError("Разрешение изображения превышает допустимое.")
            source.load()
            return source.convert("RGB")
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise GenerationError("Не удалось прочитать изображение ИИ.") from exc


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.splitlines() or [text]:
        line = ""
        for word in paragraph.split():
            candidate = f"{line} {word}".strip()
            if draw.textlength(candidate, font=font) <= width:
                line = candidate
                continue
            if line:
                lines.append(line)
                line = ""
            # Long URLs or unbroken words still need to fit without cropping.
            for char in word:
                if line and draw.textlength(line + char, font=font) > width:
                    lines.append(line)
                    line = ""
                line += char
        lines.append(line)
    return lines


def render_branded_image(
    data: bytes, text: str, brand_name: str, brand_color: str,
    *, size: tuple[int, int] = (1080, 1350), slide_number: str = "",
) -> bytes:
    """Readable deterministic typography over the original generated artwork."""
    color = _color(brand_color)
    width, height = size
    image = ImageOps.fit(_image(data), size, method=Image.Resampling.LANCZOS).convert("RGBA")
    overlay = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    top_height = 170
    box_top = max(top_height + 20, int(height * 0.53))
    draw.rectangle((0, 0, width, top_height), fill=(8, 12, 20, 210))
    draw.rectangle((0, box_top, width, height), fill=(8, 12, 20, 225))
    draw.rectangle((64, top_height - 12, width - 64, top_height - 4), fill=color)
    brand_font = _font(36, bold=True)
    brand_lines = _wrap(draw, brand_name, brand_font, width - 128)
    if len(brand_lines) > 2:
        raise GenerationError("Название проекта слишком длинное для фирменной подписи.")
    for index, line in enumerate(brand_lines):
        draw.text((64, 34 + index * 45), line, font=brand_font, fill="white")
    available = height - box_top - 120
    chosen = None
    for font_size in range(64, 27, -2):
        font = _font(font_size, bold=True)
        lines = _wrap(draw, text, font, width - 128)
        line_height = int(font_size * 1.30)
        if len(lines) * line_height <= available:
            chosen = (font, lines, line_height)
            break
    if chosen is None:
        raise GenerationError("Текст не помещается на слайде; сократите его перед производством.")
    font, lines, line_height = chosen
    for index, line in enumerate(lines):
        draw.text((64, box_top + 45 + index * line_height), line, font=font, fill="white")
    if slide_number:
        draw.text((width - 170, height - 60), slide_number, font=_font(28), fill=color)
    result = Image.alpha_composite(image, overlay).convert("RGB")
    output = io.BytesIO()
    result.save(output, format="JPEG", quality=94)
    return output.getvalue()


def _ass_time(seconds: float) -> str:
    centiseconds = int(round(seconds * 100))
    hours, rest = divmod(centiseconds, 360000)
    minutes, rest = divmod(rest, 6000)
    whole, fraction = divmod(rest, 100)
    return f"{hours}:{minutes:02d}:{whole:02d}.{fraction:02d}"


def _write_captions(path: Path, narration: str, duration: float) -> None:
    words = narration.split()
    chunks = [" ".join(words[index:index + 7]) for index in range(0, len(words), 7)]
    total = sum(len(chunk) for chunk in chunks)
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,54,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,3,1,2,72,72,220,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    elapsed = 0.0
    for index, chunk in enumerate(chunks):
        end = duration if index == len(chunks) - 1 else elapsed + duration * len(chunk) / max(1, total)
        safe = chunk.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", " ")
        events.append(f"Dialogue: 0,{_ass_time(elapsed)},{_ass_time(end)},Default,,0,0,0,,{safe}")
        elapsed = end
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")


def render_reel(image_bytes: bytes, speech_bytes: bytes, narration: str, title: str,
                brand_name: str, brand_color: str) -> bytes:
    """Render a real vertical H.264/AAC video; no remote services are called."""
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise GenerationError("Для производства видео нужны FFmpeg и ffprobe.")
    if not speech_bytes or len(speech_bytes) > 16 * 1024 * 1024:
        raise GenerationError("Озвучка отсутствует или превышает допустимый размер.")
    with tempfile.TemporaryDirectory(prefix="content_factory_render_") as directory:
        path = Path(directory)
        (path / "cover.jpg").write_bytes(render_branded_image(
            image_bytes, title, brand_name, brand_color, size=(1080, 1920)
        ))
        (path / "speech.mp3").write_bytes(speech_bytes)
        try:
            probe = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", "speech.mp3"],
                cwd=path, capture_output=True, timeout=15, check=True,
            )
            duration = float(json.loads(probe.stdout)["format"]["duration"])
        except (subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError) as exc:
            raise GenerationError("Не удалось определить длительность озвучки.") from exc
        if not math.isfinite(duration) or not 0.3 <= duration <= 59.5:
            raise GenerationError("Озвучка видео должна длиться от 0,3 до 59,5 секунды.")
        _write_captions(path / "captions.ass", narration, duration)
        video_filter = (
            "zoompan=z='min(1.0+0.00012*on,1.04)':"
            "x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=1:s=1080x1920:fps=24,"
            "subtitles=captions.ass,format=yuv420p"
        )
        try:
            subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error", "-filter_threads", "2",
                 "-loop", "1", "-framerate", "24", "-i", "cover.jpg", "-i", "speech.mp3",
                 "-vf", video_filter, "-map", "0:v:0", "-map", "1:a:0",
                 "-c:v", "libx264", "-threads", "2", "-preset", "veryfast", "-crf", "23",
                 "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                 "-af", "apad=pad_dur=0.5", "-t", f"{duration + 0.5:.3f}",
                 "-movflags", "+faststart", "reel.mp4"],
                cwd=path, capture_output=True, timeout=240, check=True,
            )
            result = (path / "reel.mp4").read_bytes()
        except (subprocess.SubprocessError, OSError) as exc:
            raise GenerationError("FFmpeg не смог собрать видео с озвучкой и субтитрами.") from exc
        if not result:
            raise GenerationError("Собранный видеофайл пуст.")
        return result


class Generator:
    """Generate a single platform-specific deliverable with bounded paid calls."""

    supports_progress = True
    supports_live_video_resume = True

    def __init__(self, api_key: str, text_model: str = "gpt-5", image_model: str = "gpt-image-2.5-sunburst",
                 speech_model: str = "gpt-4o-mini-tts", voice: str = "alloy", session=None,
                 image_quality: str = "high", review_model: str | None = None,
                 live_video_client_resolver=None):
        if image_quality not in {"low", "medium", "high", "auto"}:
            raise GenerationError("Качество изображения должно быть low, medium, high или auto.")
        self.api_key = str(api_key or "").strip()
        self.text_model = text_model
        self.review_model = review_model or text_model
        self.image_model = image_model
        self.speech_model = speech_model
        self.voice = voice
        self.image_quality = image_quality
        self.live_video_client_resolver = live_video_client_resolver
        self.session = session if session is not None else requests.Session()

    def _request(self, path: str, payload: dict):
        try:
            response = self.session.post(
                "https://api.openai.com/v1/" + path,
                headers={"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"},
                json=payload, timeout=(10, 180),
            )
        except requests.RequestException as exc:
            raise GenerationError("Связь с OpenAI прервалась; автоматический повтор платного запроса не выполнялся.") from exc
        if not 200 <= response.status_code < 300:
            # Keep bounded configuration diagnostics for the operator, never the
            # provider's message (which can repeat private input or credentials).
            details = {"http_status": response.status_code, "endpoint": path}
            try:
                error = response.json().get("error", {})
                known_codes = {"unsupported_parameter", "unsupported_value", "invalid_value", "model_not_found",
                               "insufficient_quota", "rate_limit_exceeded", "invalid_api_key", "invalid_type",
                               "invalid_request_error", "invalid_json_schema", "missing_required_parameter"}
                code = error.get("code")
                details["code"] = code if isinstance(code, str) and code in known_codes else "other"
                param = error.get("param")
                known_params = {"model", "reasoning", "reasoning.effort", "text.format", "text.format.type",
                                "max_output_tokens", "input", "instructions", "quality", "size", "n"}
                details["parameter"] = param if isinstance(param, str) and param in known_params else "other"
                message = str(error.get("message") or "").lower()
                markers = {"verification_required": ("must be verified", "verify your organization"),
                           "model_access": ("does not exist", "do not have access"),
                           "json_mode": ("json",), "reasoning_option": ("reasoning",),
                           "retention_policy": ("zero data retention", "must be set to false", "store=false")}
                details["categories"] = [name for name, words in markers.items() if any(word in message for word in words)]
            except (ValueError, TypeError, AttributeError):
                pass
            raise GenerationError(f"OpenAI отклонил запрос: HTTP {response.status_code}.", provider_details=details)
        return response

    def _json(self, path: str, payload: dict) -> dict:
        try:
            data = self._request(path, payload).json()
        except (ValueError, TypeError) as exc:
            raise GenerationError("OpenAI вернул непонятный ответ.") from exc
        if not isinstance(data, dict):
            raise GenerationError("OpenAI вернул ответ в неверном формате.")
        return data

    def _structured(self, instructions, input_data, *, model=None, max_tokens=6000):
        # The provider's JSON-mode check examines user input as well as system
        # instructions. Keep the required literal explicit in both text/vision
        # requests while preserving the structured project data.
        def json_input(value):
            data = json.loads(value)
            return json.dumps({**data, "response_format": "JSON"}, ensure_ascii=False)

        if isinstance(input_data, str):
            input_data = json_input(input_data)
        else:
            input_data = [{**item, "content": [
                {**part, "text": json_input(part["text"])} if part.get("type") == "input_text" else dict(part)
                for part in item["content"]
            ]} for item in input_data]
        payload = {
            "model": model or self.text_model, "instructions": instructions,
            "input": input_data, "max_output_tokens": max_tokens,
            "text": {"format": {"type": "json_object"}},
        }
        if payload["model"].startswith("gpt-5"):
            payload["reasoning"] = {"effort": "low"}
        response = self._json("responses", payload)
        if response.get("status") in {"incomplete", "failed", "cancelled"}:
            raise GenerationError("ИИ не завершил текущий этап. Материал не передан на публикацию.")
        parts = []
        for item in response.get("output") or []:
            if isinstance(item, dict):
                for part in item.get("content") or []:
                    if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str):
                        parts.append(part["text"])
        try:
            return json.loads("".join(parts))
        except (ValueError, TypeError) as exc:
            raise GenerationError("ИИ вернул повреждённый JSON; следующий этап не запускался.") from exc

    def _copy(self, project, job, *, previous=None, review=None):
        from content_factory_editorial import build_copy_prompt
        instructions, input_data = build_copy_prompt(project, job, previous=previous, review=review)
        return validate_content(self._structured(instructions, input_data), job["format"], job["platform"])

    def _review_copy(self, project, job, content):
        from content_factory_editorial import build_review_prompt, validate_review
        instructions, input_data = build_review_prompt(project, job, content)
        return validate_review(self._structured(instructions, input_data, model=self.review_model, max_tokens=4000))

    def _proofread(self, project, job, content):
        """Correct objective language defects before semantic review or paid media."""
        from content_factory_editorial import build_proofread_prompt
        instructions, input_data = build_proofread_prompt(project, job, content)
        result = self._structured(instructions, input_data, model=self.review_model)
        if isinstance(result, dict) and result.get("format", job["format"]) != job["format"]:
            raise GenerationError("Корректор изменил формат материала; производство остановлено.")
        corrected = validate_content(result, job["format"], job["platform"])
        if job["format"] == "carousel":
            original_slides = content.get("slides") or []
            if len(corrected["slides"]) != len(original_slides):
                raise GenerationError("Корректор изменил число слайдов; производство остановлено.")
            if any(slide in original_slides and original_slides.index(slide) != index
                   for index, slide in enumerate(corrected["slides"])):
                raise GenerationError("Корректор переставил слайды; производство остановлено.")
        return corrected

    def _illustration(self, prompt):
        data = self._json("images/generations", {
            "model": self.image_model, "n": 1, "size": "1024x1536", "quality": self.image_quality,
            "prompt": prompt,
        })
        try:
            encoded = data["data"][0]["b64_json"]
            if not isinstance(encoded, str) or len(encoded) > 28 * 1024 * 1024:
                raise ValueError("Invalid image data")
            result = base64.b64decode(encoded, validate=True)
        except (KeyError, IndexError, TypeError, ValueError, binascii.Error) as exc:
            raise GenerationError("OpenAI не вернул готовое изображение.") from exc
        _image(result).close()
        return result

    def _review_artwork(self, project, job, content, frame, art_direction, artwork):
        from content_factory_editorial import build_image_review_prompt, validate_review
        instructions, input_data = build_image_review_prompt(project, job, content, frame, art_direction)
        with _image(artwork) as source:
            source.thumbnail((768, 1152))
            preview = io.BytesIO()
            source.save(preview, format="JPEG", quality=85)
        visual_input = [{"role": "user", "content": [
            {"type": "input_text", "text": input_data},
            {"type": "input_image", "detail": "high", "image_url":
             "data:image/jpeg;base64," + base64.b64encode(preview.getvalue()).decode()},
        ]}]
        return validate_review(self._structured(instructions, visual_input, model=self.review_model, max_tokens=4000))

    def _continue_live_video(self, project, state, client, *, report, save_production, save_source, load_source):
        from content_factory_editorial import build_motion_prompt, validate_visual_plan
        from content_factory_live_media import render_live_reel
        from content_factory_live_video import LiveVideoError, LiveVideoPending, LiveVideoUncertain

        if not can_resume_video(state):
            raise GenerationError("Не удалось проверить сохранённые исходники живого видео.")
        state = copy.deepcopy(state)
        content = state["content"]
        count = len(state["raw_sources"])
        plan = validate_visual_plan(content.get("visual_plan"), frame_count=count)
        custom_prompts = state.get("motion_prompts")
        if custom_prompts is not None and (
            not isinstance(custom_prompts, list) or len(custom_prompts) != count or any(
                not isinstance(value, str) or not value.strip() or len(value.encode("utf-16-le")) // 2 > 1000
                for value in custom_prompts
            )
        ):
            raise GenerationError("Неверно сохранено описание движения живых сцен.")

        def persist():
            save_production(copy.deepcopy(state))

        starting = state.get("starting_index")
        if state.get("creation_uncertain") or (starting is not None and not state["tasks"][starting]):
            raise LiveVideoUncertain("Видеосервис мог принять прошлую сцену. Проверьте её результат перед новым платным запуском.")
        if state.get("failed_indices"):
            raise LiveVideoError("Одна из живых сцен завершилась ошибкой. Повтор возможен после проверки материала.")
        state["phase"] = "clips"
        state["starting_index"] = None
        for index in range(count):
            if state["tasks"][index] or state["clips"][index]:
                continue
            report("video", f"Запускаем живую сцену {index + 1} из {count}.")
            prompt = custom_prompts[index] if custom_prompts is not None else build_motion_prompt(
                project, plan["frames"][index], plan["art_direction"], index=index, total=count,
            )
            source = load_source(state["raw_sources"][index])
            state["starting_index"] = index
            persist()  # A crash after this marker must never blindly repeat POST.
            try:
                task_id = client.create_scene(source, prompt, state["scene_seconds"][index])
            except LiveVideoUncertain:
                state["creation_uncertain"] = True
                persist()
                raise
            except LiveVideoError:
                state["starting_index"] = None
                state["failed_indices"] = [index]
                persist()
                raise
            if not isinstance(task_id, str) or not task_id.strip() or len(task_id) > 200:
                state["creation_uncertain"] = True
                persist()
                raise LiveVideoUncertain("Видеосервис не подтвердил номер созданной сцены. Повторный платный запуск остановлен.")
            state["tasks"][index] = task_id.strip()
            state["starting_index"] = None
            persist()  # Persist this ID before attempting the next paid scene.

        pending, failed = 0, []
        for index, task_id in enumerate(state["tasks"]):
            if state["clips"][index]:
                continue
            try:
                status = client.task_status(task_id)
                if status.get("status") == "succeeded":
                    clip = client.download(status["url"])
                    state["clips"][index] = save_source(f"live_scene_{index + 1:02d}.mp4", clip, "video/mp4")
                    persist()
                elif status.get("status") == "failed":
                    failed.append(index)
                else:
                    pending += 1
            except LiveVideoError as error:
                if not getattr(error, "retryable", False):
                    raise
                pending += 1
        if failed:
            state["failed_indices"] = failed
            persist()
            raise LiveVideoError("Одна из живых сцен завершилась ошибкой. Готовые сцены сохранены; повторите только после проверки.")
        if pending:
            ready = sum(value is not None for value in state["clips"])
            report("video", f"Видеосервис создаёт живые сцены. Готово {ready} из {count}.")
            persist()
            raise LiveVideoPending("Живые сцены ещё создаются. Завод проверит их автоматически.", retry_after=30)

        report("video", "Собираем живые сцены с голосом и субтитрами.")
        clips = [load_source(reference) for reference in state["clips"]]
        speech = load_source(state["speech_source"])
        video = render_live_reel(clips, speech, content["narration"], content["title"],
                                 project["name"], project["brand_color"])
        content["video_kind"] = "live"
        state["phase"] = "complete"
        state["content"] = content
        persist()
        report("saving", "Сохраняем готовое живое видео.")
        return content, [{"data": video, "mime_type": "video/mp4", "filename": "live_reel.mp4"}]

    def generate(self, project, job, *, progress=None, production=None,
                 save_production=None, save_source=None, load_source=None):
        from content_factory_editorial import (
            PROJECT_LIMITS, EditorialError, build_image_prompt, build_visual_prompt, validate_visual_plan,
        )
        from content_factory_media import MediaError, render_slide, render_story_reel
        from content_factory_live_media import LiveMediaError, speech_duration, scene_durations
        if not self.api_key:
            raise GenerationError("Ключ OpenAI не настроен.")
        if not isinstance(job.get("format"), str) or not isinstance(job.get("platform"), str) or job["format"] not in FORMATS or job["platform"] not in TEXT_LIMITS:
            raise GenerationError("Неизвестный формат или площадка задания.")
        clean_project = {name: str(project.get(name) or "").strip() for name in _FIELDS}
        if any(len(value) > PROJECT_LIMITS[name] for name, value in clean_project.items()):
            raise GenerationError("Сведения о проекте превышают допустимый объём.")
        if any(not clean_project[name] for name in ("name", "product", "audience")):
            raise GenerationError("Заполните название, продукт и аудиторию проекта.")
        clean_project["brand_color"] = _color(project.get("brand_color"))
        mode = "live" if production else job.get("video_mode", project.get("video_mode", "illustrated"))
        if mode not in {"live", "illustrated"}:
            raise GenerationError("Выберите поддерживаемый режим создания видео.")
        clean_project["video_mode"] = mode
        _font(36)
        if job["format"] == "reel" and not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
            raise GenerationError("Для производства видео нужны FFmpeg и ffprobe.")

        def report(stage, message):
            if progress is not None:
                progress({"stage": stage, "message": message})

        content = None
        try:
            live_client = None
            if job["format"] == "reel" and mode == "live":
                if not callable(self.live_video_client_resolver):
                    raise GenerationError("Живое видео пока не подключено. Завершите подключение видеосервиса в проекте.")
                if not all(callable(callback) for callback in (save_production, save_source, load_source)):
                    raise GenerationError("Для живого видео нужно постоянное защищённое хранилище исходников.")
                live_client = self.live_video_client_resolver(project)
                if live_client is None:
                    raise GenerationError("Живое видео пока не подключено. Завершите подключение видеосервиса в проекте.")
                if production:
                    if not can_resume_video(production):
                        raise GenerationError("Сохранённое создание живого видео требует проверки; новый платный запуск остановлен.")
                    content = production["content"]
                    snapshot = production.get("project") or clean_project
                    return self._continue_live_video(snapshot, production, live_client, report=report,
                                                     save_production=save_production, save_source=save_source, load_source=load_source)
                preflight = live_client.preflight()
                balance = preflight.get("credit_balance") if isinstance(preflight, dict) else None
                if isinstance(balance, (int, float)) and balance <= 0:
                    raise GenerationError("В видеосервисе недостаточно кредитов для создания живых сцен.")
            report("copy", "Автор готовит текст по вашей задаче.")
            content = self._copy(clean_project, job)
            report("proofread", "Корректор проверяет грамматику и точность формулировок.")
            content = self._proofread(clean_project, job, content)
            report("review", "Стагирит проверяет смысл, факты и пользу текста.")
            review = self._review_copy(clean_project, job, content)
            if not review["approved"]:
                report("copy", "Автор дорабатывает текст по замечаниям Стагирита.")
                content = self._copy(clean_project, job, previous=content, review=review)
                report("proofread", "Корректор проверяет исправленный текст.")
                content = self._proofread(clean_project, job, content)
                report("review", "Стагирит проверяет исправленный текст.")
                review = self._review_copy(clean_project, job, content)
            content["quality_review"] = review
            if not review["approved"]:
                raise GenerationError("Стагирит остановил производство: текст требует доработки. "
                                      + "; ".join(review["issues"])[:600], draft=content)

            speech, duration = None, None
            if live_client is not None:
                report("video", "Записываем голос, чтобы точно рассчитать длительность живых сцен.")
                speech = self._request("audio/speech", {
                    "model": self.speech_model, "voice": self.voice,
                    "input": content["narration"], "response_format": "mp3",
                }).content
                duration = speech_duration(speech)
            frame_count = len(content["slides"]) if job["format"] == "carousel" else (
                max(4, math.ceil((duration + 0.35) / 10)) if live_client is not None else 4
            ) if job["format"] == "reel" else 1
            report("visual", "Арт-директор разрабатывает сюжет и единый стиль иллюстраций.")
            instructions, input_data = build_visual_prompt(clean_project, job, content, frame_count=frame_count)
            visual_plan = validate_visual_plan(self._structured(instructions, input_data), frame_count=frame_count)
            content["visual_plan"] = visual_plan
            content["visual_reviews"] = []
            artwork = []
            # One correction for the whole deliverable bounds image spending.
            corrections_left = 1
            for index, frame in enumerate(visual_plan["frames"]):
                report("image", f"Создаём и проверяем иллюстрацию {index + 1} из {frame_count}.")
                prompt = build_image_prompt(clean_project, frame, visual_plan["art_direction"], index=index, total=frame_count)
                image = self._illustration(prompt)
                image_review = self._review_artwork(clean_project, job, content, frame, visual_plan["art_direction"], image)
                if not image_review["approved"] and corrections_left:
                    corrections_left -= 1
                    correction = json.dumps(image_review, ensure_ascii=False)
                    image = self._illustration(prompt + "\nCorrect these concrete visual defects while preserving the scene and style: " + correction)
                    image_review = self._review_artwork(clean_project, job, content, frame, visual_plan["art_direction"], image)
                content["visual_reviews"].append(image_review)
                if not image_review["approved"]:
                    content["quality_review"] = image_review
                    raise GenerationError("Проверка иллюстрации выявила ошибки. Уточните оформление в материале; публикация остановлена.", draft=content)
                artwork.append(image)

            title, name, color = content["title"], clean_project["name"], clean_project["brand_color"]
            assets = []
            if job["format"] == "carousel":
                for index, (image, slide) in enumerate(zip(artwork, content["slides"])):
                    layout = "cover" if index == 0 else "clean" if index == len(artwork) - 1 else "editorial"
                    image = render_slide(image, slide, name, color, layout=layout,
                                         slide_number=f"{index + 1}/{len(artwork)}")
                    assets.append({"data": image, "mime_type": "image/jpeg", "filename": f"slide_{index + 1:02d}.jpg"})
            elif job["format"] == "reel":
                if live_client is not None:
                    seconds = scene_durations(duration, frame_count)
                    if getattr(live_client, "model", "gen4.5") == "gen4_turbo":
                        seconds = [5 if value <= 5 else 10 for value in seconds]
                    raw_sources = []
                    for index, image in enumerate(artwork):
                        with Image.open(io.BytesIO(image)) as source:
                            mime = Image.MIME.get(source.format, "image/png")
                        extension = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}.get(mime)
                        if extension is None:
                            raise GenerationError("Не удалось сохранить исходник живой сцены.")
                        raw_sources.append(save_source(f"live_input_{index + 1:02d}.{extension}", image, mime))
                    speech_source = save_source("live_narration.mp3", speech, "audio/mpeg")
                    content["video_kind"] = "live"
                    state = {"version": 1, "phase": "inputs_ready", "project": clean_project,
                             "content": content, "raw_sources": raw_sources, "speech_source": speech_source,
                             "duration": duration, "scene_seconds": seconds, "tasks": [None] * frame_count,
                             "clips": [None] * frame_count, "failed_indices": [], "starting_index": None}
                    save_production(copy.deepcopy(state))
                    return self._continue_live_video(clean_project, state, live_client, report=report,
                                                     save_production=save_production, save_source=save_source, load_source=load_source)
                report("video", "Записываем озвучку и собираем сцены, движение и субтитры.")
                speech = self._request("audio/speech", {
                    "model": self.speech_model, "voice": self.voice,
                    "input": content["narration"], "response_format": "mp3",
                }).content
                video = render_story_reel(artwork, speech, content["narration"], title, name, color)
                content["video_kind"] = "illustrated"
                assets.append({"data": video, "mime_type": "video/mp4", "filename": "reel.mp4"})
            else:
                image = render_slide(artwork[0], title, name, color, layout="cover")
                assets.append({"data": image, "mime_type": "image/jpeg", "filename": "post.jpg"})
            report("saving", "Сохраняем готовый материал.")
            return content, assets
        except GenerationError as error:
            if error.draft is None:
                error.draft = content
            raise
        except (EditorialError, MediaError, LiveMediaError) as error:
            raise GenerationError(str(error), draft=content) from error
