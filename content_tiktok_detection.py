"""Detect a *probable* animated TikTok mark, without modifying user's video.

Diagnostic only. Masking solid rectangles across a whole video is unsafe:
the floating mark can change sides and cover subtitles or faces.
"""
from __future__ import annotations
import subprocess
import tempfile
from pathlib import Path
import numpy as np
from content_video_cleanup import _probe_video, _binary, MAX_VIDEO_BYTES, VideoCleanupError


def _detect_from_frames(frames, info):
    """1 frame/sec, use TikTok's cyan-red contour; suppress scene color flashes."""
    if not len(frames):
        raise VideoCleanupError("Нет кадров для анализа.")
    h, w = frames.shape[1:3]
    observations = []
    for n, frame in enumerate(frames):
        arr = frame.astype(np.int16)
        red, green, blue = arr[..., 0], arr[..., 1], arr[..., 2]
        colored = (blue > red + 30) & (green > red + 12) & (
            blue > 105) & (green > 105) & (blue > green - 35)
        lm = np.zeros_like(colored)
        rm = np.zeros_like(colored)
        a, b = round(h*.35), round(h*.57)
        c, d = round(h*.66), round(h*.91)
        lm[a:b, :round(w*.42)] = colored[a:b, :round(w*.42)]
        rm[c:d, round(w*.62):] = colored[c:d, round(w*.62):]
        ls, rs = int(lm.sum()), int(rm.sum())
        side = ("слева" if ls >= 9 and ls > rs * 1.25
                else "справа" if rs >= 9 and rs > ls * 1.25
                else "неопределённо")
        observations.append({
            "side": side, "t": n + .5, "left": ls, "right": rs,
            "left_pixels": np.where(lm), "right_pixels": np.where(rm),
            "brightness": float(frame.mean()),
        })
    confident = sum(row["side"] != "неопределённо" for row in observations)
    if confident < max(3, round(len(frames)*.15)):
        return {"found": False, "segments": [], "outro_start": None}
    labels = []
    for i in range(len(observations)):
        votes = {"слева": 0, "справа": 0}
        for item in observations[max(0, i-2):i+3]:
            if item["side"] in votes:
                votes[item["side"]] += 1
        candidate = max(votes, key=votes.get)
        labels.append(candidate if votes[candidate] >= 2 else "неопределённо")
    for i, label in enumerate(labels):
        if label == "неопределённо":
            candidates = [
                (abs(i-k), v) for k, v in enumerate(labels)
                if v != "неопределённо"
            ]
            if candidates:
                labels[i] = min(candidates)[1]
    for _ in range(3):
        ranges, start = [], 0
        for j in range(1, len(labels)+1):
            if j == len(labels) or labels[j] != labels[start]:
                ranges.append((start, j, labels[start]))
                start = j
        for j, (start, end, kind) in enumerate(ranges):
            if (0 < j < len(ranges)-1 and end-start < 6 and
                    ranges[j-1][2] == ranges[j+1][2]):
                labels[start:end] = [ranges[j-1][2]]*(end-start)
    dark = [
        r["t"] for r in observations
        if r["t"] >= info["duration"]-13 and r["brightness"] < 45
    ]
    outro = None
    if len(dark) >= 2 and dark[-1] >= info["duration"]-1.7:
        tail = [dark[-1]]
        for time in reversed(dark[:-1]):
            if tail[0] - time <= 1.25:
                tail.insert(0, time)
            else:
                break
        if len(tail) >= 2:
            outro = max(0., tail[0]-.5)
    limit = min(outro, info["duration"]) if outro is not None else info["duration"]
    centers = {}
    for side, field in (("слева", "left_pixels"), ("справа", "right_pixels")):
        coords = []
        for item, label in zip(observations, labels):
            if label != side or item["t"] >= limit:
                continue
            yy, xx = item[field]
            other = item["right" if side == "слева" else "left"]
            if len(xx) >= 12 and len(xx) >= other * 1.1:
                coords.append((
                    float(np.median(xx))*info["width"]/w,
                    float(np.median(yy))*info["height"]/h,
                ))
        if coords:
            centers[side] = {
                "x": round(float(np.median([c[0] for c in coords]))),
                "y": round(float(np.median([c[1] for c in coords]))),
            }
    breaks = [(0.0, labels[0])]
    for i in range(1, len(labels)):
        if labels[i] != labels[i-1]:
            breaks.append((float(i), labels[i]))
    breaks.append((limit, None))
    segments = [
        {"from": round(a, 1), "to": round(min(b, limit), 1),
         "side": label, "center": centers[label]}
        for (a, label), (b, _) in zip(breaks, breaks[1:])
        if label in centers and a < limit and b > a
    ]
    return {"found": bool(segments), "segments": segments,
            "outro_start": round(outro, 1) if outro is not None else None}


def detect_tiktok_overlays(video_bytes: bytes, filename: str = "video.mp4"):
    """Return probable overlay positions; does NOT remove watermarks."""
    if not video_bytes or not isinstance(video_bytes, bytes):
        raise VideoCleanupError("Загрузите видео.")
    if len(video_bytes) > MAX_VIDEO_BYTES:
        raise VideoCleanupError("Видео больше 120 МБ.")
    suffix = Path(filename).suffix.lower()
    if suffix not in (".mp4", ".mov", ".m4v"):
        raise VideoCleanupError("Поддерживаются только MP4, MOV и M4V.")
    with tempfile.TemporaryDirectory(prefix="agency_w_tiktok_scan_") as folder:
        path = Path(folder)/("sample"+suffix)
        path.write_bytes(video_bytes)
        info = _probe_video(path)
        width = 240
        height = round((info["height"]/info["width"]*width)/2)*2
        args = [
            _binary("ffmpeg"), "-v", "error", "-nostdin", "-i", str(path),
            "-vf", f"fps=1,scale={width}:{height}:flags=bilinear",
            "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1",
        ]
        try:
            result = subprocess.run(args, check=True, capture_output=True, timeout=90)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise VideoCleanupError("Не удалось проверить видео.") from exc
        stride = width*height*3
        if not result.stdout or len(result.stdout)%stride:
            raise VideoCleanupError("Не удалось получить кадры видео.")
        frames = np.frombuffer(result.stdout, dtype=np.uint8).reshape(
            (-1, height, width, 3)
        )
        output = _detect_from_frames(frames, info)
        output["duration"] = round(info["duration"], 1)
        output["diagnostic_only"] = True
        return output
