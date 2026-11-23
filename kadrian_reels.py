"""Local Reels from existing footage, with immutable sources and versioned exports."""
from datetime import timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFont

from content_factory_core import iso, utc_now
from kadrian_editing import _probe

STYLES = {
    'Элегантный': ('#e8c88c', '#241c23', 62, 24, 270),
    'Кинематографический': ('#f5ae66', '#182329', 72, 8, 230),
    'Образовательный': ('#aee2ff', '#14233e', 58, 12, 220),
    'Эмоциональный': ('#ffc0bf', '#382139', 66, 34, 300),
    'Рекламный': ('#d6f891', '#252d21', 78, 4, 260),
}
MAX_BYTES = 50 * 1024 * 1024
AUDIO_TYPES = {'wav': 'audio/wav', 'mp3': 'audio/mpeg', 'm4a': 'audio/mp4'}


def _run(*args, cwd=None):
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise ValueError('Для монтажа требуется FFmpeg.')
    return subprocess.run([ffmpeg, '-nostdin', '-v', 'error', *args], cwd=cwd,
                          check=True, capture_output=True, timeout=180)


def _stream(path, kind, formats, max_seconds=60):
    info = _probe(path)
    stream = next((item for item in info.get('streams', [])
                   if item.get('codec_type') == kind and not item.get('disposition', {}).get('attached_pic')), None)
    detected = set(info.get('format', {}).get('format_name', '').split(','))
    if stream is None or not detected.intersection(formats):
        raise ValueError('Проверьте формат и содержимое загруженных файлов.')
    seconds = float(stream.get('duration') or info['format']['duration'])
    if not 0 < seconds <= max_seconds:
        raise ValueError(f'Длительность этого файла должна быть до {max_seconds} секунд.')
    return stream['index'], seconds


def _settings(title, caption, style, music_level):
    title, caption = title.strip(), caption.strip()
    if not title or len(title) > 60 or len(caption) > 100 or style not in STYLES:
        raise ValueError('Введите заголовок до 60 знаков и подпись до 100 знаков.')
    if not 0 <= music_level <= 15:
        raise ValueError('Уровень музыки должен быть от 0 до 15%.')
    return dict(title=title, caption=caption, style=style, music_level=music_level)


def _graphics(path, title, caption, style):
    fonts = [os.getenv('KADRIAN_REELS_FONT'), '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
             '/System/Library/Fonts/Supplemental/Arial.ttf']
    font_path = next((name for name in fonts if name and Path(name).is_file()), None)
    if font_path is None:
        raise ValueError('Для титров требуется шрифт с поддержкой русского языка.')
    accent, background, title_size, radius, title_y = STYLES[style]
    image = Image.new('RGBA', (1080, 1920))
    draw = ImageDraw.Draw(image)
    for content, position, size, color, panel in (
        (title, title_y, title_size, accent, background),
        (caption, 1465, 48, '#ffffff', background),
    ):
        if not content:
            continue
        for size_now in range(size, 23, -2):
            font = ImageFont.truetype(font_path, size_now)
            words = content.split()
            lines = ['']
            for word in words:
                candidate = (lines[-1] + ' ' + word).strip()
                if lines[-1] and font.getlength(candidate) > 740:
                    lines.append(word)
                else:
                    lines[-1] = candidate
            if len(lines) <= 3 and max(map(font.getlength, lines)) <= 740:
                break
        else:
            raise ValueError('Разбейте длинное слово в титрах.')
        height = len(lines) * (size_now + 12) + 55
        draw.rounded_rectangle((76, position, 905, position + height), radius, fill=panel)
        draw.rounded_rectangle((102, position + 26, 110, position + height - 26), 4, fill=accent)
        for index, line in enumerate(lines):
            draw.text((135, position + 29 + index * (size_now + 12)), line,
                      font=font, fill=color, anchor='lt')
    image.save(path)


def render(clip, voice, settings, music=None):
    """The original recording is the foreground audio and is played at 1×."""
    with tempfile.TemporaryDirectory(prefix='kadrian-reel-') as folder:
        root = Path(folder)
        for name, data in (('clip.mp4', clip), ('voice', voice), ('music', music)):
            if data is not None:
                (root / name).write_bytes(data)
        video_index, seconds = _stream(root / 'clip.mp4', 'video', {'mp4', 'mov'})
        audio_formats = {'mp3', 'wav', 'mp4', 'mov'}
        voice_index, speech = _stream(root / 'voice', 'audio', audio_formats)
        if speech > seconds + 1 / 30:
            raise ValueError('Запись речи длиннее клипа. Загрузите клип, покрывающий всю реплику.')
        duration = f'{max(seconds, speech):.6f}'
        fade = min(0.25, seconds / 4)
        _graphics(root / 'graphics.png', settings['title'], settings['caption'], settings['style'])
        args = ['-filter_complex_threads', '1', '-protocol_whitelist', 'file,pipe', '-i', 'clip.mp4',
                '-protocol_whitelist', 'file,pipe', '-i', 'voice',
                '-loop', '1', '-framerate', '30', '-i', 'graphics.png']
        graph = [
            f'[0:{video_index}]setpts=PTS-STARTPTS,scale=1080:1920:force_original_aspect_ratio=decrease:'
            'force_divisible_by=2,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=0x17171e,setsar=1,fps=30[base]',
            f'[2:v]format=rgba,fade=in:st=0:d={fade}:alpha=1[graphic]',
            '[base][graphic]overlay=shortest=1[v]',
        ]
        voice_graph = (f'[1:{voice_index}]asetpts=PTS-STARTPTS,aresample=48000,'
                       f'aformat=channel_layouts=stereo,apad=whole_dur={duration}')
        if music and settings['music_level']:
            music_index, music_seconds = _stream(root / 'music', 'audio', audio_formats, max_seconds=600)
            end = min(float(duration), music_seconds)
            tail = min(0.35, end / 3)
            args += ['-protocol_whitelist', 'file,pipe', '-i', 'music']
            graph += [
                voice_graph + ',asplit[voice][sidechain]',
                f'[3:{music_index}]asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,'
                f'volume={settings["music_level"] / 100},atrim=end={end:.6f},'
                f'afade=t=out:st={end-tail:.6f}:d={tail:.6f},apad=whole_dur={duration}[bed]',
                '[bed][sidechain]sidechaincompress=threshold=0.025:ratio=6:attack=15:release=250[ducked]',
                '[voice][ducked]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.89:level=0:latency=1[a]',
            ]
        else:
            graph.append(voice_graph + '[a]')
        _run(*args, '-filter_complex', ';'.join(graph), '-map', '[v]', '-map', '[a]',
             '-c:v', 'libx264', '-threads', '2', '-preset', 'veryfast', '-crf', '20', '-pix_fmt', 'yuv420p',
             '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', 'reel.mp4', cwd=root)
        output = root / 'reel.mp4'
        _run('-xerror', '-i', 'reel.mp4', '-f', 'null', '-', cwd=root)
        if not 0 < output.stat().st_size <= MAX_BYTES:
            raise ValueError('Готовый MP4 превышает 50 МБ.')
        _, rendered_seconds = _stream(output, 'video', {'mp4'}, max_seconds=61)
        _, rendered_audio = _stream(output, 'audio', {'mp4'}, max_seconds=61)
        if abs(rendered_seconds - seconds) > 1 / 30 + 0.001 or abs(rendered_audio - float(duration)) > 0.05:
            raise ValueError('Длительность экспорта отличается от исходников.')
        return output.read_bytes()


def _job(store, owner, project, job_id, marker):
    job = store.get_job(owner, job_id)
    if not job or job['project_id'] != project or not job.get(marker):
        raise ValueError('Этот материал недоступен в выбранном проекте.')
    return job


def save_sources(store, owner, project, *, clip, voice, music=None, live_draft_id=None):
    """Save byte-exact originals in the existing owner/project media storage."""
    uploads = {'clip': clip, 'voice': voice}
    if music:
        uploads['music'] = music
    if any(not data for _, data in uploads.values()) or sum(len(data) for _, data in uploads.values()) > MAX_BYTES:
        raise ValueError('Загрузите непустые исходники общим размером до 50 МБ.')
    manifest = {role: (extension, sha256(data).hexdigest()) for role, (extension, data) in uploads.items()}
    if clip[0] != 'mp4' or any(ext not in AUDIO_TYPES for role, (ext, _) in manifest.items() if role != 'clip'):
        raise ValueError('Нужен клип MP4 и аудио WAV, MP3 или M4A.')
    fingerprint = sha256(json.dumps([live_draft_id, manifest], sort_keys=True).encode()).hexdigest()
    job = store.enqueue_job(owner, project, {
        'dedupe_key': 'kadrian-reels-sources:' + fingerprint, 'state': 'paused',
        'platform': 'studio', 'format': 'reel', 'kadrian_reels_sources': True,
        'reels_live_draft_id': live_draft_id,
    })
    if job.get('reels_sources'):
        return job
    assets = {role: store.put_asset(owner, project, job['id'], f'{role}.{ext}', data,
                                   'video/mp4' if role == 'clip' else AUDIO_TYPES[ext])
              for role, (ext, data) in uploads.items()}
    return store.update_job(owner, job['id'], {'reels_sources': assets}, expected_updated_at=job['updated_at'])


def save_revision(store, owner, project, source_id, **options):
    settings = _settings(**options)
    source = _job(store, owner, project, source_id, 'kadrian_reels_sources')
    sources = source.get('reels_sources')
    if not sources:
        raise ValueError('Сначала сохраните исходники.')
    fingerprint = sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()
    job = store.enqueue_job(owner, project, {
        'dedupe_key': f'kadrian-reels-render:{source_id}:{fingerprint}', 'state': 'paused',
        'platform': 'studio', 'format': 'reel', 'kadrian_reels_revision': source_id, 'reels_settings': settings,
        'reels_live_draft_id': source.get('reels_live_draft_id'),
    })
    if job.get('assets'):
        return job
    lease = uuid4().hex
    job = store.update_job(owner, job['id'], {
        'lease_owner': lease, 'lease_until': iso(utc_now() + timedelta(minutes=12)),
    }, expected_updated_at=job['updated_at'])
    try:
        data = render(**{role: store.read_asset(owner, asset) for role, asset in sources.items()}, settings=settings)
        asset = store.put_asset(owner, project, job['id'], 'reel.mp4', data, 'video/mp4')
        return store.update_job(owner, job['id'], {'assets': [asset], 'lease_owner': None, 'lease_until': None},
                                lease_owner=lease)
    except Exception:
        store.update_job(owner, job['id'], {'lease_owner': None, 'lease_until': None}, lease_owner=lease)
        raise
