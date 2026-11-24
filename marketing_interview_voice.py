"""Decode microphone WAV files locally with the existing whisper.cpp engine."""
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import wave


def voice_engine():
    executable = Path(os.getenv("KADRIAN_WHISPER_CLI", "/nonexistent"))
    model = Path(os.getenv("KADRIAN_WHISPER_MODEL", "/nonexistent"))
    if executable.is_file() and os.access(executable, os.X_OK) and model.is_file():
        return executable.resolve(), model.resolve()
    return None


def transcribe_locally(recording, engine):
    if not 44 < len(recording) <= 4_000_000:
        raise ValueError("Запишите ответ продолжительностью до двух минут.")
    executable, model = engine
    with TemporaryDirectory(prefix="marketing-voice-") as directory:
        path = Path(directory) / "answer.wav"
        path.write_bytes(recording)
        try:
            with wave.open(str(path)) as audio:
                if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) != (16000, 1, 2):
                    raise ValueError("Запишите ответ встроенным микрофоном кабинета.")
                if not 0 < audio.getnframes() <= 120 * 16000:
                    raise ValueError("Запишите ответ продолжительностью до двух минут.")
            subprocess.run(
                [str(executable), "-m", str(model), "-f", str(path), "-l", "ru",
                 "-otxt", "-of", "answer", "-np", "-t", "2"],
                cwd=directory, check=True, timeout=150, capture_output=True,
            )
            result = (Path(directory) / "answer.txt").read_text(encoding="utf-8").strip()
        except (wave.Error, EOFError, subprocess.SubprocessError, OSError):
            raise ValueError("Не удалось распознать запись. Попробуйте записать её снова.") from None
        if not result or len(result) > 2000:
            raise ValueError("Проверьте запись. Текст ответа должен содержать до 2000 символов.")
        return result
