"""Music edits and stop commands never cross a paid video-generation boundary."""
from __future__ import annotations
from datetime import timedelta
from fractions import Fraction
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from uuid import uuid4

from content_factory_core import iso, utc_now
from content_factory_store import StoreError

AUDIO_MIMES = {'.mp3': 'audio/mpeg', '.wav': 'audio/wav', '.m4a': 'audio/mp4'}
STOP_REPLY = ('Создание новых сцен остановлено. Готовые файлы сохранены. '
              'Уже принятый видеосервисом заказ может завершиться; его оплата не отменена.')


def local_command(prompt):
    """Inspect the actual latest user message, before consulting any model/history."""
    text = ' '.join(str(prompt).lower().replace('ё', 'е').split())
    cleaned = re.sub(r'не\s+(?:останавливай\w*|прекращай\w*)', '', text)
    stop = re.search(r'\b(?:стоп|останови\w*|прекрати\w*|перестань|хватит)\b', cleaned)
    stop = stop or re.search(r'\bне\s+(?:создавай|генерируй|запускай)\b', cleaned)
    stop = stop or re.search(r'не\s+(?:надо|нужно)\s+(?:создавать|генерировать)', cleaned)
    music = re.search(r'\b(?:музык\w*|трек\w*|саундтрек\w*|аудиофайл\w*|аудио|звук)\b', text)
    if stop and not re.search(r'\b(?:останови|прекрати)\s+(?:музыку|трек|воспроизведение)\b', text):
        return {'action': 'stop', 'edit_music': bool(music), 'reply': STOP_REPLY}
    new_picture = re.search(r'\b(?:создай|сгенерируй|нарисуй|оживи|анимируй)\b.{0,60}\b(?:нов\w*\s+)?(?:сцен\w*|ролик\w*|видео\w*|мультфильм\w*|картинк\w*)', text)
    if music and not new_picture:
        if re.search(r'\b(?:создай|сгенерируй|сочини)\s+(?:нов\w*\s+)?(?:музык\w*|трек\w*)', text):
            return {'action':'discuss','reply':'Для новой музыки выберите производство «Музыка — ElevenLabs». Наложение вашего готового трека доступно в разделе монтажа. Новые видеосцены не запускаются.'}
        return {'action': 'music_edit'}
    return None


def _project(store, owner, project_id):
    from agency_access_control import is_agency_access_revoked
    project = store.get_project(owner, project_id)
    if not project or project.get('owner_id') != owner or is_agency_access_revoked(owner):
        raise ValueError('Проект недоступен этому владельцу.')
    return project


def _merge(store, owner, project_id, mutate):
    for attempt in range(5):
        project = _project(store, owner, project_id)
        changes = mutate(project)
        try:
            return store.update_project_if_current(owner, project_id, changes, project['updated_at'])
        except StoreError:
            if attempt == 4:
                raise


def pause_production(store, owner, project_id):
    _merge(store, owner, project_id, lambda p: {'kadrian_production_paused': True})
    count = 0
    for job in store.list_jobs(owner, project_id, limit=1000):
        if not job.get('studio_master') or job.get('state') not in {'queued', 'generating'}:
            continue
        for attempt in range(5):
            current = store.get_job(owner, job['id'])
            if not current or current.get('state') not in {'queued', 'generating'}:
                break
            try:
                store.pause_studio_job(owner,project_id,job['id'],current['updated_at'],STOP_REPLY)
                count += 1
                break
            except StoreError:
                if attempt == 4:
                    raise
    return count


def normalize_upload(name, mime, data):
    # Browsers may provide an empty or generic MIME for a genuine MP3.
    audio_mime = AUDIO_MIMES.get(Path(name).suffix.lower())
    return name, audio_mime or mime, data


def remember_music_uploads(store, owner, project_id, files):
    from kadrian_music import save_uploaded_music
    normalized = [normalize_upload(*item) for item in files]
    for name, mime, data in normalized:
        if Path(name).suffix.lower() not in AUDIO_MIMES:
            if mime.startswith('audio/') or Path(name).suffix.lower() in {'.ogg','.aac','.flac'}:
                raise ValueError('Для музыки нужен MP3, WAV или M4A. Прежний трек не подставляется.')
            continue
        job = save_uploaded_music(store, owner, project_id, data, name)
        if not job.get('studio_complete'):
            raise ValueError('Музыка ещё не сохранена. Новые сцены не запускаются.')
        def remember(project):
            history = list(project.get('kadrian_music_history') or [])
            if not history or history[-1] != job['id']:
                history.append(job['id'])
            return {'kadrian_music_history': history, 'kadrian_music_selected': job['id']}
        _merge(store, owner, project_id, remember)
    return normalized


def edit_choices(store, owner, project_id):
    _project(store, owner, project_id)
    jobs = store.list_jobs(owner, project_id, limit=1000)
    clips = [j for j in jobs if j.get('studio_master') and j.get('state') in {'ready', 'published', 'scheduled'}
             and any(a.get('mime_type') == 'video/mp4' for a in j.get('assets') or []) and not j.get('error')]
    music = [j for j in jobs if j.get('studio_complete') and (j.get('content') or {}).get('uploaded_music')
             and any(a.get('mime_type') in AUDIO_MIMES.values() for a in j.get('assets') or [])]
    return clips, music


def default_sources(clips):
    if not clips:
        return []
    ids = (clips[0].get('production') or {}).get('source_job_ids') or []
    available = {j['id'] for j in clips}
    return ids if ids and all(i in available for i in ids) else [clips[0]['id']]


def choose_music(project, music, prompt='', selected=None):
    available = {j['id']: j for j in music}
    history = [i for i in project.get('kadrian_music_history') or [] if i in available]
    if not history:
        history = [j['id'] for j in sorted(music, key=lambda j: j['created_at'])]
    text = re.sub(r'не\s+(?:перв\w*|втор\w*)', '', str(prompt).lower())
    ordinal = 2 if re.search(r'втор\w*', text) else (1 if re.search(r'(?<!не )перв\w*', text) else None)
    if ordinal:
        if len(history) < ordinal:
            raise ValueError('Указанная музыка ещё не сохранена. Прикрепите нужный трек; новые сцены не запускаются.')
        return available[history[ordinal - 1]]
    if re.search(r'последн\w*', text) and history:
        return available[history[-1]]
    ident = selected or project.get('kadrian_music_selected') or (history[-1] if history else None)
    if ident not in available:
        raise ValueError('Для монтажа нужна ваша музыка. Прикрепите MP3, WAV или M4A; новые сцены не запускаются.')
    return available[ident]


def _probe(path):
    executable = shutil.which('ffprobe')
    if not executable:
        raise ValueError('Проверка готовых файлов временно недоступна. Новые сцены не запускаются.')
    result = subprocess.run([executable, '-v', 'error', '-protocol_whitelist', 'file,pipe',
                             '-show_streams', '-show_format', '-of', 'json', str(path)],
                            check=True, capture_output=True, timeout=30)
    return json.loads(result.stdout)


def assemble_files(videos, music, *, replace_audio=False):
    """Stream-copy conformed footage; preserve speed; music owns the soundtrack."""
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise ValueError('Монтаж временно недоступен: не найден FFmpeg. Новые сцены не запускаются.')
    with tempfile.TemporaryDirectory(prefix='kadrian_edit_') as folder:
        root = Path(folder)
        infos, paths = [], []
        for index, data in enumerate(videos):
            path = root / f'scene-{index}.mp4'
            path.write_bytes(data)
            info = _probe(path)
            if not replace_audio and any(s.get('codec_type') == 'audio' for s in info['streams']):
                raise ValueError('В сценах уже есть звук. Чтобы не удалить речь, замена остановлена. Для замены всего звука включите соответствующий параметр монтажа.')
            video = next((s for s in info['streams'] if s.get('codec_type') == 'video'), None)
            if not video or video.get('codec_name') != 'h264':
                raise ValueError('Нужны готовые H.264-сцены. Новая генерация не запускается.')
            infos.append(video)
            paths.append(path)
        if any(tuple(i.get(k) for k in ('width','height','avg_frame_rate','pix_fmt','time_base')) !=
               tuple(infos[0].get(k) for k in ('width','height','avg_frame_rate','pix_fmt','time_base')) for i in infos):
            raise ValueError('Выбранные сцены имеют разные форматы. Выберите совместимые готовые сцены; новые не создаются.')
        duration = sum(float(i['duration']) for i in infos)
        if not 0 < duration <= 600:
            raise ValueError('Монтаж поддерживает до 10 минут готовых сцен.')
        soundtrack = root / 'music'
        soundtrack.write_bytes(music)
        audio_info = _probe(soundtrack)
        if not any(s.get('codec_type') == 'audio' for s in audio_info['streams']):
            raise ValueError('Не удалось прочитать выбранную музыку.')
        audio_seconds = min(duration, float(audio_info['format']['duration']))
        (root/'scenes.txt').write_text(''.join(f"file '{p.name}'\n" for p in paths))
        audio_filter = (f'atrim=end={audio_seconds:.9f},asetpts=PTS-STARTPTS,aresample=48000,'
                        f'afade=t=out:st={max(0,audio_seconds-.2):.9f}:d={min(.2,audio_seconds):.9f},'
                        'alimiter=limit=0.89:level=false')
        output = root/'final.mp4'
        subprocess.run([ffmpeg,'-nostdin','-v','error','-f','concat','-safe','1','-i',str(root/'scenes.txt'),
                        '-i',str(soundtrack),'-map','0:v:0','-map','1:a:0','-c:v','copy','-af',audio_filter,
                        '-c:a','aac','-b:a','192k','-ar','48000','-movflags','+faststart',str(output)],
                       check=True,capture_output=True,timeout=300)
        if not 0 < output.stat().st_size <= 100*1024*1024:
            raise ValueError('Готовый файл превышает допустимый размер.')
        subprocess.run([ffmpeg,'-nostdin','-v','error','-i',str(output),'-f','null','-'],
                       check=True,capture_output=True,timeout=300)
        rendered = _probe(output)
        final_video = next(s for s in rendered['streams'] if s.get('codec_type') == 'video')
        if abs(float(final_video['duration']) - duration) > float(1/Fraction(infos[0]['avg_frame_rate'])):
            raise ValueError('Длительность монтажа не совпала со сценами. Повторная генерация не запускается.')
        return output.read_bytes(), duration


def assemble_music(store, owner, project_id, source_ids, music_job, *, replace_audio=False):
    project = _project(store, owner, project_id)
    if not 1 <= len(source_ids) <= 12 or len(set(source_ids)) != len(source_ids):
        raise ValueError('Выберите от 1 до 12 разных готовых фрагментов в порядке монтажа.')
    clips, uploaded = edit_choices(store, owner, project_id)
    available = {j['id']: j for j in clips}
    audio = {j['id']: j for j in uploaded}
    if any(i not in available for i in source_ids) or music_job['id'] not in audio:
        raise ValueError('Материалы недоступны в выбранном проекте. Новые сцены не запускаются.')
    selected = [next(a for a in available[i]['assets'] if a.get('mime_type') == 'video/mp4') for i in source_ids]
    track = next(a for a in audio[music_job['id']]['assets'] if a.get('mime_type') in AUDIO_MIMES.values())
    if sum(int(a.get('size') or 0) for a in selected + [track]) > 100*1024*1024:
        raise ValueError('Общий размер исходников монтажа превышает 100 МБ. Новые сцены не запускаются.')
    from kadrian_production import _check_asset
    for asset in selected + [track]:
        _check_asset({'owner_id':owner,'project_id':project_id}, asset)
    fingerprint = sha256(json.dumps([source_ids, [a.get('sha256') for a in selected], track.get('sha256'), bool(replace_audio)], sort_keys=True).encode()).hexdigest()
    job = store.enqueue_job(owner, project_id, {
        'dedupe_key':'kadrian-edit:'+fingerprint,'state':'paused','platform':'studio','format':'reel',
        'studio_master':True,'video_style':available[source_ids[0]].get('video_style','cartoon'),
        'review_required':True,'content':{'title':'Монтаж готовых сцен с вашей музыкой'},
        'assets':[],'checkpoint':{},'error':'','kadrian_local_edit':True})
    if job.get('state') == 'ready' and job.get('assets'):
        return job
    if job.get('lease_owner') and job.get('lease_until','') > iso(utc_now()):
        raise ValueError('Этот монтаж уже выполняется. Новые сцены не запускаются.')
    writer = 'kadrian-edit-'+uuid4().hex
    job = store.update_job(owner, job['id'], {'lease_owner':writer,'lease_until':iso(utc_now()+timedelta(minutes=10))}, expected_updated_at=job['updated_at'])
    try:
        render_options = {'replace_audio':True} if replace_audio else {}
        data, duration = assemble_files([store.read_asset(owner,a) for a in selected], store.read_asset(owner,track), **render_options)
        asset = store.put_asset(owner,project_id,job['id'],'kadrian-music-edit.mp4',data,'video/mp4')
        return store.update_job(owner,job['id'],{
            'state':'ready','assets':[asset],'error':'','lease_owner':None,'lease_until':None,
            'checkpoint':{'generation_complete':True},
            'content':{'title':'Монтаж готовых сцен с вашей музыкой',
                       'text':str((music_job.get('content') or {}).get('title') or ''),'duration_seconds':duration},
            'production':{'manual_edit':True,'source_job_ids':source_ids,'music_job_id':music_job['id'],
                          'music_sha256':track.get('sha256'),'replace_audio':bool(replace_audio),'duration_seconds':duration,'generation_calls':0},
            'progress':{'stage':'ready','message':'Монтаж готов. Использована выбранная музыка, новые сцены не создавались.'}}, lease_owner=writer)
    except Exception:
        store.update_job(owner,job['id'],{'state':'paused','lease_owner':None,'lease_until':None,
                         'error':'Монтаж не завершён. Исходники сохранены. Новая генерация не запускается.'},lease_owner=writer)
        raise
