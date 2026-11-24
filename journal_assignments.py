"""Owner-scoped editorial work and verified media handoffs."""
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path, PurePosixPath
from shutil import which
import subprocess
from tempfile import TemporaryDirectory


VIDEO_FORMATS = {"Видеообзор", "Reels"}


def assignment_key(brief):
    fields = [brief[key] for key in ("passport_version", "date", "format", "topic", "segment")]
    return sha256(json.dumps(fields, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def assign_video(state, brief):
    if brief["format"] not in VIDEO_FORMATS:
        raise ValueError("Это задание не относится к видео.")
    key = assignment_key(brief)
    assignments = state.get("journal_assignments", {})
    if key in assignments:
        return state
    return {**state, "journal_assignments": {
        **assignments, key: {"brief": brief, "status": "assigned"},
    }}


def validate_video(data):
    executable = which("ffprobe")
    if executable is None:
        raise ValueError("Проверка видео требует FFprobe.")
    with TemporaryDirectory(prefix="journal-video-") as tmp:
        path = Path(tmp) / "media.mp4"
        path.write_bytes(data)
        try:
            result = subprocess.run(
                [executable, "-v", "error", "-protocol_whitelist", "file", "-f", "mov",
                 "-select_streams", "v:0", "-show_entries", "stream=width,height:format=duration",
                 "-of", "json", str(path)],
                check=True, capture_output=True, timeout=15,
            )
            metadata = json.loads(result.stdout)
            stream = metadata["streams"][0]
            duration = float(metadata["format"]["duration"])
            if not isfinite(duration) or min(int(stream["width"]), int(stream["height"]), duration) <= 0:
                raise ValueError
        except (subprocess.SubprocessError, KeyError, IndexError, ValueError):
            raise ValueError("Файл не прошёл проверку видеодорожки и длительности.") from None


def read_job_video(owner, project, job_id, store):
    job = store.get_job(owner, job_id)
    if (not job or str(job.get("project_id")) != str(project)
            or job.get("state") not in {"ready", "scheduled", "published"}):
        raise ValueError("Нужно завершённое задание Кадриана из этого проекта.")
    assets = [item for item in job.get("assets", []) if item.get("mime_type") == "video/mp4"]
    if len(assets) != 1:
        raise ValueError("Задание должно содержать один итоговый MP4.")
    asset = assets[0]
    parts = PurePosixPath(asset.get("storage_path", "")).parts
    if parts[:3] != (str(owner), str(project), str(job_id)):
        raise ValueError("Видеофайл не принадлежит выбранному заданию.")
    data = store.read_asset(owner, asset)
    digest = sha256(data).hexdigest()
    if asset.get("sha256") != digest:
        raise ValueError("Содержимое видео отличается от сохранённой версии.")
    return asset, data

def receive_video(state, key, owner, project, job_id, store, probe=validate_video):
    assignments = state["journal_assignments"]
    task = assignments[key]
    asset, data = read_job_video(owner, project, job_id, store)
    probe(data)
    result = {**task, "status": "media_received", "job_id": job_id,
              "filename": asset["filename"], "sha256": asset["sha256"]}
    return {**state, "journal_assignments": {**assignments, key: result}}
