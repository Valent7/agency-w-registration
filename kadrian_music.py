"""Validate and store owner-uploaded music without calling a generation API."""
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

MAX_MUSIC_BYTES = 30 * 1024 * 1024


def inspect_music(data, filename):
    suffix = Path(filename).suffix.lower()
    mime = {'.mp3': 'audio/mpeg', '.wav': 'audio/wav', '.m4a': 'audio/mp4'}.get(suffix)
    if not mime or not isinstance(data, bytes) or not 0 < len(data) <= MAX_MUSIC_BYTES:
        raise ValueError('Выберите MP3, WAV или M4A размером до 30 МБ.')
    probe = shutil.which('ffprobe')
    if not probe:
        raise ValueError('Проверка аудиофайла временно недоступна.')
    try:
        with tempfile.TemporaryDirectory(prefix='kadrian_music_') as folder:
            path = Path(folder) / ('music' + suffix)
            path.write_bytes(data)
            result = subprocess.run([probe, '-v', 'error', '-protocol_whitelist', 'file,pipe',
                                     '-show_format', '-show_streams', '-of', 'json', str(path)],
                                    capture_output=True, timeout=20, check=True)
            info = json.loads(result.stdout)
        streams = info.get('streams') or []
        duration = float(info['format']['duration'])
        formats = set(str(info['format']['format_name']).split(','))
        expected = {'.mp3': {'mp3'}, '.wav': {'wav'}, '.m4a': {'mov', 'mp4', 'm4a'}}[suffix]
        # Attached cover pictures are harmless; moving video is not a soundtrack.
        if not formats.intersection(expected) or not any(s.get('codec_type') == 'audio' for s in streams):
            raise ValueError()
        if any(s.get('codec_type') == 'video' and not s.get('disposition', {}).get('attached_pic') for s in streams):
            raise ValueError()
        if not 0 < duration <= 1800:
            raise ValueError()
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError):
        raise ValueError('Не удалось прочитать музыку. Нужен аудиофайл длительностью до 30 минут.') from None
    return mime, duration


def save_uploaded_music(store, owner_id, project_id, data, filename):
    from content_studio_catalog import save_studio_result
    mime, duration = inspect_music(data, filename)
    name = Path(filename).name[:120]
    request_id = 'music_upload_' + sha256(data + name.encode()).hexdigest()
    job = save_studio_result(store, owner_id, project_id, 'music', 'Своя музыка: ' + name,
                             data=data, mime_type=mime, request_id=request_id)
    if job.get('studio_complete'):
        content = {**job.get('content', {}), 'audio_duration': duration, 'uploaded_music': True}
        job = store.update_job(owner_id, job['id'], {'content': content}, expected_updated_at=job.get('updated_at'))
    return job
