"""Agency W restricted pilot: reproduce user-approved 'Русские немцы' cleanup.

Two temporal zones, unlike old stationary, two-sided FFmpeg delogo:
 - first seconds: borrow clean scenery from same video's 8.5-second frame
   using local SIFT geometric alignment;
 - rest: narrowly inpaint the fixed marks near the right edge.
This is a calibrated profile, NOT a general TikTok-logo detector.
Never upload personal footage to any external API.
"""
from __future__ import annotations
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

RUSSIAN_GERMANS_SHA256 = "dda5836364c461d232269b35e21e24458fd6bfa59a0d129067769b2d94e2fed1"
MAX_UPLOAD_BYTES = 95 * 1024 * 1024
MAX_PREVIEW_SECONDS = 12.0

class PilotError(ValueError):
    pass

def recognized_reference(payload: bytes) -> bool:
    return isinstance(payload, bytes) and hashlib.sha256(payload).hexdigest() == RUSSIAN_GERMANS_SHA256

def _dependencies():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise PilotError("Для обработки требуется FFmpeg.")
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise PilotError("Тестовый модуль требует OpenCV.") from exc
    if not hasattr(cv2, "SIFT_create"):
        raise PilotError("Нужна сборка OpenCV с SIFT.")
    return cv2, np

def _probe(path: Path):
    r = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,avg_frame_rate:format=duration",
        "-of", "json", str(path),
    ], capture_output=True, text=True, check=True, timeout=15)
    result = json.loads(r.stdout)
    video = result["streams"][0]
    n, d = video["avg_frame_rate"].split("/")
    return int(video["width"]), int(video["height"]), float(n) / float(d), float(result["format"]["duration"])

def make_test_preview(
    payload: bytes, *, start_second: float = 0, duration_seconds: float = 8,
) -> tuple[bytes, dict]:
    """Return short, audio-preserving sample before any full-length service."""
    if not isinstance(payload, bytes) or not payload or len(payload) > MAX_UPLOAD_BYTES:
        raise PilotError("Загрузите MP4 до 95 МБ.")
    if not recognized_reference(payload):
        raise PilotError("Этот тестовый режим пока проверен только на исходном ролике «Русские немцы». Другие видео автоматически не обрабатываем.")
    start, duration = float(start_second), float(duration_seconds)
    if start not in (0.0, 60.0, 180.0):
        raise PilotError("Выберите предложенный участок видео.")
    if not 2 <= duration <= MAX_PREVIEW_SECONDS:
        raise PilotError("Продолжительность пробы — 2–12 секунд.")
    cv2, np = _dependencies()
    with tempfile.TemporaryDirectory(prefix="w_video_pilot_") as temp:
        src = Path(temp) / "source.mp4"
        dst = Path(temp) / "preview.mp4"
        logfile = Path(temp) / "ffmpeg.log"
        src.write_bytes(payload)
        try:
            width, height, fps, total = _probe(src)
        except Exception as exc:
            raise PilotError("Не удалось прочитать MP4.") from exc
        if (width, height) != (464, 848) or abs(fps - 30) > 0.1 or total < 305:
            raise PilotError("Файл отличается от контрольного оригинала.")
        cap = cv2.VideoCapture(str(src))
        cap.set(cv2.CAP_PROP_POS_MSEC, 8500)
        ok, reference = cap.read()
        if not ok:
            cap.release()
            raise PilotError("Эталонный кадр недоступен.")
        keymask = np.zeros((height, width), np.uint8)
        keymask[315:490, 0:320] = 255
        keymask[385:475, 0:132] = 0
        sift = cv2.SIFT_create(nfeatures=1500)
        keypoints, desc = sift.detectAndCompute(
            cv2.cvtColor(reference, cv2.COLOR_BGR2GRAY), keymask,
        )
        bf = cv2.BFMatcher()
        left = np.zeros((height, width), np.uint8)
        left[389:469, 0:126] = 255
        alpha = cv2.GaussianBlur(left, (19, 19), 4).astype("float32") / 255
        right = np.zeros((height, width), np.uint8)
        for x1, y1, x2, y2 in [
            (413, 643, 453, 687), (400, 685, 463, 709), (342, 708, 463, 726),
        ]:
            right[y1:y2, x1:x2] = 255
        cap.set(cv2.CAP_PROP_POS_MSEC, start * 1000)
        command = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{width}x{height}",
            "-r", str(fps), "-i", "pipe:0",
            "-ss", str(start), "-i", str(src),
            "-map", "0:v:0", "-map", "1:a:0?", "-t", str(duration),
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", "-threads", "2", str(dst),
        ]
        homography = None
        warped = None
        prev_key = -1
        written = 0
        with logfile.open("wb") as log:
            process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log,
            )
            try:
                for i in range(round(duration * fps)):
                    ok, frame = cap.read()
                    if not ok:
                        break
                    second = start + i / fps
                    if second < 3.9:
                        key = int(second * fps) // 15
                        if key != prev_key:
                            kp, dt = sift.detectAndCompute(
                                cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), keymask,
                            )
                            if desc is not None and dt is not None:
                                pairs = bf.knnMatch(desc, dt, k=2)
                                matches = [
                                    a for a, b in pairs if a.distance < .73 * b.distance
                                ]
                                if len(matches) >= 7:
                                    a = np.float32([
                                        keypoints[m.queryIdx].pt for m in matches
                                    ]).reshape(-1, 1, 2)
                                    b = np.float32([
                                        kp[m.trainIdx].pt for m in matches
                                    ]).reshape(-1, 1, 2)
                                    matrix, valid = cv2.findHomography(
                                        a, b, cv2.RANSAC, 4,
                                    )
                                    if (matrix is not None and int(valid.sum()) >= 7
                                            and abs(np.linalg.det(matrix[:2, :2])) > .15):
                                        homography = matrix
                            if homography is not None:
                                warped = cv2.warpPerspective(
                                    reference, homography, (width, height),
                                )
                            prev_key = key
                        if warped is not None:
                            frame = np.uint8(
                                frame * (1 - alpha[:, :, None])
                                + warped * alpha[:, :, None]
                            )
                    else:
                        frame = cv2.inpaint(frame, right, 3, cv2.INPAINT_TELEA)
                    process.stdin.write(frame.tobytes())
                    written += 1
            except (BrokenPipeError, OSError) as exc:
                process.kill()
                raise PilotError("Ошибка обработки видеокадров.") from exc
            finally:
                cap.release()
                if process.stdin is not None:
                    process.stdin.close()
            status = process.wait(timeout=70)
        if status or not dst.is_file() or dst.stat().st_size < 1000:
            raise PilotError("Пробный MP4 не создан.")
        if written < round(duration * fps) - 3:
            raise PilotError("Видеопроба неполная.")
        return dst.read_bytes(), {
            "start": start, "duration": written / fps,
            "width": width, "height": height, "frames": written,
        }
