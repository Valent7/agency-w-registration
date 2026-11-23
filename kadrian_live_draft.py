"""Owner-scoped drafts and local-only preparation for the studio's live video."""
from io import BytesIO
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
from uuid import uuid4

from PIL import Image

from content_factory_core import iso, utc_now
from kadrian_reels import AUDIO_TYPES, _run, _stream

MARKER = 'kadrian_live_draft'
DEFAULT_LOOK = (
    'Образ: естественный, с привычной для вас манерой говорить. '
    'Одежда: однотонная, без мелкого рисунка. Причёска: сохранить привычную форму и цвет. '
    'Фон: спокойный, с небольшим размытием. Свет: мягкий спереди и чуть сбоку. '
    'Кадр: вертикальный, по грудь. Сохранить черты лица, возраст и оттенок кожи.'
)


def image_extension(photo):
    if not photo or len(photo) > 15 * 1024 * 1024:
        raise ValueError('Выберите фото до 15 МБ.')
    with Image.open(BytesIO(photo)) as image:
        if image.width * image.height > 24_000_000 or image.format not in {'JPEG', 'PNG', 'WEBP'}:
            raise ValueError('Нужен JPG, PNG или WebP размером до 24 мегапикселей.')
        ext = {'JPEG': 'jpeg', 'PNG': 'png', 'WEBP': 'webp'}[image.format]
        image.verify()
    return ext


def create(store, owner, project, photo):
    ext = image_extension(photo)
    job = store.enqueue_job(owner, project, {
        'dedupe_key': 'kadrian-live:' + uuid4().hex, 'platform': 'studio', 'format': 'reel',
        'state': 'paused', MARKER: True,
    })
    asset = store.put_asset(owner, project, job['id'], f'photo.{ext}', photo, f'image/{ext}')
    return store.update_job(owner, job['id'], {'live_photo': asset, 'live_history': []},
                            expected_updated_at=job['updated_at'])


def update(store, owner, project, job, action, value=None, *, title_cues=None):
    if job['owner_id'] != owner or job['project_id'] != project or not job.get(MARKER) or not job.get('live_photo'):
        raise ValueError('Черновик недоступен в этом проекте.')
    if action in {'brief', 'script', 'look'}:
        value = value.strip()
        if not 0 < len(value) <= 6000:
            raise ValueError('Введите текст длиной до 6000 знаков.')
        if action != 'brief' and value == job.get('live_' + action):
            return job
    if action == 'brief':
        changes = {'live_brief': value, 'live_script': '', 'live_script_ok': False,
                   'live_look': '', 'live_look_ok': False, 'live_voice': None, 'live_title_cues': []}
    elif action == 'script' and job.get('live_brief'):
        changes = {'live_script': value, 'live_script_ok': False, 'live_look': '',
                   'live_look_ok': False, 'live_voice': None, 'live_title_cues': title_cues or []}
    elif action == 'approve_script' and job.get('live_script'):
        changes = {'live_script_ok': True}
    elif action == 'look' and job.get('live_script_ok'):
        changes = {'live_look': value, 'live_look_ok': False}
    elif action == 'approve_look' and job.get('live_script_ok') and job.get('live_look'):
        changes = {'live_look_ok': True}
    elif action == 'visuals':
        video, frames = value
        if len(frames) > 6 or sum(map(len, frames)) > 30 * 1024 * 1024:
            raise ValueError('Можно сохранить до шести кадров общим размером до 30 МБ.')
        extensions = [image_extension(frame) for frame in frames]
        if video is not None:
            if not 0 < len(video) <= 50 * 1024 * 1024:
                raise ValueError('Выберите видео MP4 до 50 МБ.')
            with TemporaryDirectory() as folder:
                path = Path(folder) / 'clip.mp4'
                path.write_bytes(video)
                _stream(path, 'video', {'mp4', 'mov'})
        changes = {}
        if video is not None:
            changes['live_preview_video'] = store.put_asset(owner, project, job['id'], 'preview.mp4', video, 'video/mp4')
        if frames:
            changes['live_preview_frames'] = [
                store.put_asset(owner, project, job['id'], f'frame-{i}.{ext}', frame, f'image/{ext}')
                for i, (ext, frame) in enumerate(zip(extensions, frames), 1)
            ]
        if not changes:
            raise ValueError('Выберите видео или изображения.')
        value = changes
    elif action == 'voice' and job.get('live_script_ok') and job.get('live_look_ok'):
        ext, raw = value
        if ext not in AUDIO_TYPES or not 0 < len(raw) <= 25 * 1024 * 1024:
            raise ValueError('Нужна запись WAV, MP3 или M4A до 25 МБ.')
        with TemporaryDirectory() as folder:
            path = Path(folder) / f'voice.{ext}'
            path.write_bytes(raw)
            _stream(path, 'audio', {'wav', 'mp3', 'mp4', 'mov'})
        asset = store.put_asset(owner, project, job['id'], f'voice.{ext}', raw, AUDIO_TYPES[ext])
        changes = {'live_voice': asset}
        value = asset
    else:
        raise ValueError('Сначала завершите предыдущий шаг.')
    history = [*job.get('live_history', []), {'action': action, 'value': value, 'at': iso(utc_now())}]
    return store.update_job(owner, job['id'], {**changes, 'live_history': history},
                            expected_updated_at=job['updated_at'])


def _local_program(program_var, model_var):
    program, model = os.getenv(program_var), os.getenv(model_var)
    if program and model and Path(program).is_file() and Path(model).is_file():
        return os.path.abspath(program), os.path.abspath(model)
    return None


def local_editor_ready():
    return _local_program('KADRIAN_LLAMA_COMPLETION', 'KADRIAN_LLAMA_MODEL') is not None


def local_speech_ready():
    return _local_program('KADRIAN_WHISPER_CLI', 'KADRIAN_WHISPER_MODEL') is not None


def transcribe(raw):
    engine = _local_program('KADRIAN_WHISPER_CLI', 'KADRIAN_WHISPER_MODEL')
    if not engine:
        raise ValueError('Локальное распознавание речи пока не настроено. Введите указания текстом.')
    if not 0 < len(raw) <= 25 * 1024 * 1024:
        raise ValueError('Запишите короткое указание до 25 МБ.')
    binary, model = engine
    with TemporaryDirectory() as folder:
        root = Path(folder)
        (root / 'input.wav').write_bytes(raw)
        _stream(root / 'input.wav', 'audio', {'wav'}, max_seconds=120)
        _run('-protocol_whitelist', 'file,pipe', '-i', 'input.wav', '-t', '120', '-ar', '16000',
             '-ac', '1', 'speech.wav', cwd=root)
        subprocess.run([binary, '-m', model, '-f', 'speech.wav', '-l', 'ru', '-otxt', '-of', 'speech',
                        '-np'], cwd=root, capture_output=True, check=True, timeout=180)
        transcript = (root / 'speech.txt').read_text().strip()
    if not transcript:
        raise ValueError('Речь не распознана. Попробуйте снова или введите текст.')
    return transcript


def _clean_brief(text):
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r'(?<!\S)(?:эм|ээ+)\b[,.]?\s*|(?:^|(?<=,))\s*(?:ну|короче)\b[,.]?\s*', '', text, flags=re.I)
    text = re.sub(r'\b(\w+(?: \w+){0,3})(?:[, ]+\1\b)+', r'\1', text, flags=re.I)
    text = re.sub(r'\s+', ' ', text).strip(' ,–')
    text = re.sub(r'^(?:но,\s*)?(?:я )?(?:хочу|хотела бы|хотел бы) (?:в (?:этом )?ролике )?(?:сказать|рассказать|донести)[,: ]*(?:что\s+)?', '', text, flags=re.I)
    text = re.sub(r'([.!?])\s+(?=[а-яё])([а-яё])', lambda m: m[1] + ' ' + m[2].upper(), text)
    if not text:
        raise ValueError('Расскажите, какую мысль хотите донести до зрителя.')
    text = text[0].upper() + text[1:]
    return text if text.endswith(('.', '!', '?')) else text + '.'


def _delegated_look(direction):
    """Keep photo-based hair and explicit wardrobe when the rest is delegated."""
    delegation = re.search(r'\bвс[её]\s+остально\w*\b[^.!?]*(?:под твою ответственност\w*|на тво[её] усмотрение|выбери сам\w*)',
                           direction, flags=re.I)
    if not delegation:
        return None
    details = direction[:delegation.start()].strip(' ,.;\n')
    match = re.fullmatch(
        r'прич[её]ска\s+(?:точно\s+)?как на\s+(?:исходн\w+\s+)?(?:фотографии|фото)'
        r'\s*[,.;]\s*((?:рубашк\w*|блуз\w*|пиджак\w*|жакет\w*|плать\w*|одежда:)\s*.+)',
        details, flags=re.I | re.S,
    )
    if not match:
        return None
    wardrobe = match[1].strip()
    wardrobe = wardrobe[0].upper() + wardrobe[1:]
    return (
        'Причёска: сохранить причёску, пробор и укладку как на исходной фотографии. Сохранить черты лица.\n\n'
        f'Одежда: {wardrobe}.\n\n'
        'Фон и свет: спокойный серо-синий фон с лёгким градиентом, мягкий нейтральный свет на лице '
        'и лёгкая подсветка силуэта.\n\n'
        'Оформление Reels: вертикальный кадр 9:16. Средний план для речи и короткий поясной план для образа. '
        'Контрастные титры с акцентными стрелками, свободная зона вокруг лица.'
    )


def editorial(brief, *, current='', direction='', look=False, previous_look=''):
    if look and direction:
        specified = _delegated_look(direction)
        if specified:
            return specified
    engine = _local_program('KADRIAN_LLAMA_COMPLETION', 'KADRIAN_LLAMA_MODEL')
    if not engine:
        if direction:
            raise ValueError('Локальный редактор пока не настроен. Отредактируйте текст вручную.')
        if look:
            subject = (brief + ' ' + current).lower()
            if re.search(r'\b(?:обуч\w*|объяс\w*|урок\w*|покаж\w*)\b', subject):
                return DEFAULT_LOOK + ' Оставить сбоку место для коротких пояснений.'
            if re.search(r'\b(?:личн\w*|истори\w*|воспоминан\w*)\b', subject):
                return DEFAULT_LOOK + ' Для личной истории подойдёт тёплый фон из привычной обстановки.'
            return DEFAULT_LOOK
        return _clean_brief(brief)
    binary, model = engine
    task = (
        'Предложи конкретный узнаваемый образ, одежду, причёску, фон, свет и вертикальную композицию. '
        'Для одежды, причёски, фона и света дай по одному конкретному короткому пункту. '
        'Не утверждай, что видел фото, сохрани черты и индивидуальность.'
        if look else
        'Напиши естественный русский сценарий для речи в Reels. Начни с конкретной мысли автора, '
        'развей её и дай естественное завершение. Сохрани голос, смысл и позицию автора. '
        'Убери вступления вроде «я хочу сказать», повторы и слова-паразиты. '
        'Перечитай результат и исправь грамматику. Не добавляй новые факты, обещания или чужие идеи.'
    )
    parts = [f'Замысел автора:\n{_clean_brief(brief)}']
    if current and not (look and direction):
        parts.append(f'Текст ролика:\n{current}')
    if look and direction:
        parts = [f'Прежнее описание:\n{previous_look}', f'Пожелания автора:\n{direction}',
                 'Создай новый образ с учётом пожеланий. Начни сразу с «Причёска:». '
                 'Затем напиши отдельные пункты «Одежда:», «Фон:», «Свет:» и «Оформление Reels:». '
                 'Все явно заданные детали перенеси в подходящие пункты. Убери противоречащие детали прежнего образа.']
    elif direction:
        parts.append(f'Перепиши сценарий целиком по указанию автора. Верни только новую речь для озвучки. '
                     f'Не добавляй в неё формулировки указания и не приклеивай новый текст к старому.\n{direction}')
    elif look:
        parts.append('Опиши образ для этого ролика. Укажи одежду, причёску, фон и свет.')
    else:
        parts.append('Напиши текст, который автор будет говорить в камеру.')
    prompt = '\n\n'.join(parts)
    with TemporaryDirectory() as folder:
        prompt_file = Path(folder) / 'prompt.txt'
        prompt_file.write_text(prompt, encoding='utf-8')
        result = subprocess.run([binary, '-m', model, '-f', str(prompt_file), '-sys',
                                 task + ' Ответь по-русски. Верни только готовый текст.', '-n', '420', '-c', '8192',
                                 '-t', '4', '--temp', '0.25', '--conversation', '--single-turn', '--no-display-prompt',
                                 '--simple-io', '--color', 'off'],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                check=True, timeout=180).stdout.strip()
    result = result.removesuffix('[end of text]').strip()
    if look and re.search(r'[\u3400-\u9fff]', result):
        raise ValueError('Редактор вернул часть описания на другом языке. Предыдущий образ сохранён.')
    if direction:
        normalized = ' '.join(result.casefold().split())
        prior = ' '.join((previous_look if look else current).casefold().split())
        instruction = ' '.join(direction.casefold().split())
        if (prior and prior in normalized) or (len(instruction) >= 25 and instruction in normalized) or re.search(
            r'(?im)^\s*(?:текст ролика|замысел автора|указание автора|редакция сценария)\s*:', result
        ):
            raise ValueError('Редактор повторил прежний текст или указание. Предыдущая версия сохранена.')
    if not 0 < len(result) <= 6000:
        raise ValueError('Локальный редактор вернул пустой или слишком длинный текст.')
    return result
