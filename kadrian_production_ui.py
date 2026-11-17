"""Production choices and owner confirmation for Kadrian's connected services."""
from __future__ import annotations

from hashlib import sha256
import json
import math
import os
from uuid import uuid4

import streamlit as st

from content_factory_core import iso, utc_now
from kadrian_providers import HeyGenClient, heygen_payload, shotstack_payload

ROUTES = {
    'runway': 'Киносцены и мультфильм — Runway',
    'scene': 'Короткий ролик или мультфильм — HeyGen Video',
    'presenter': 'Один ведущий — HeyGen',
    'interview': 'Интервью двух ведущих — HeyGen',
    'assembly': 'Сборка готовых сцен — Shotstack',
    'music': 'Музыка — ElevenLabs',
}


def _config():
    from content_factory_runtime import _configuration
    return _configuration()


def connection_status():
    cfg = _config()
    return {
        'HeyGen': bool(cfg.get('HEYGEN_API_KEY') or cfg.get('HEYGEN_KEY')),
        'Runway': bool(cfg.get('RUNWAYML_API_SECRET')),
        'Shotstack': bool(cfg.get('SHOTSTACK_API_KEY')),
        'ElevenLabs': bool(cfg.get('ELEVENLABS_API_KEY')),
    }


def enqueue_provider(store, owner_id, project_id, provider, spec, style, request_id, quote_usd, quote_cents=None):
    project = store.get_project(owner_id, project_id)
    if not project:
        raise ValueError('Проект недоступен этому владельцу.')
    if isinstance(quote_usd, bool) or not isinstance(quote_usd, (int, float)) or not math.isfinite(quote_usd) or quote_usd <= 0:
        raise ValueError('Нужна оценка стоимости перед запуском.')
    if provider == 'heygen':
        heygen_payload(spec)
    elif provider != 'shotstack':
        raise ValueError('Неизвестный сервис производства.')
    from content_factory_billing import billing_enforced
    if billing_enforced() and (type(quote_cents) is not int or quote_cents <= 0):
        raise ValueError('Нужна согласованная стоимость в валюте баланса Агентства.')
    fingerprint = sha256(json.dumps(spec, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    now = iso(utc_now())
    job = store.enqueue_job(owner_id, project_id, {
        'platform': 'studio', 'format': 'reel', 'state': 'queued',
        'scheduled_at': now, 'available_at': now, 'requested_by_owner': True,
        'studio_master': True, 'video_style': style, 'review_required': True,
        'kadrian_provider': provider, 'provider_spec': spec,
        'provider_fingerprint': fingerprint, 'approved_quote_usd': quote_usd,
        'approved_quote_cents': quote_cents,
        'dedupe_key': 'kadrian-provider:' + request_id,
        'topic': str(spec.get('title') or spec.get('prompt') or 'Сборка Кадриана')[:150],
        'content': {}, 'assets': [], 'checkpoint': {}, 'error': '', 'attempts': 0,
    })
    if job.get('provider_fingerprint') != fingerprint or job.get('kadrian_provider') != provider:
        raise ValueError('Этот запрос уже сохранён с другими параметрами. Обновите подготовку.')
    return job


def _catalog(client, key):
    if st.button('Загрузить ведущих и голоса', key=key + '_catalog_load'):
        try:
            client.preflight()
            avatars, voices = client.avatars(), client.voices()
            st.session_state[key + '_catalog'] = {
                'avatars': avatars.get('items', avatars.get('looks', [])),
                'voices': voices.get('items', voices.get('voices', [])),
                'avatars_token': avatars.get('next_token'), 'voices_token': voices.get('next_token'),
            }
        except Exception:
            st.error('Не удалось получить каталог HeyGen. Проверьте API-ключ и разрешения на сервере.')
    catalog = st.session_state.get(key + '_catalog', {})
    for category, loader in (('avatars', client.avatars), ('voices', client.voices)):
        token = catalog.get(category + '_token')
        if token and st.button('Ещё ведущие' if category == 'avatars' else 'Ещё голоса', key=key + '_more_' + category):
            try:
                page = loader(token)
                catalog[category].extend(page.get('items', []))
                catalog[category + '_token'] = page.get('next_token')
            except Exception:
                st.error('Не удалось загрузить следующую страницу каталога.')
    return catalog


def _speakers(client, count, key):
    catalog = _catalog(client, key)
    avatars, voices = catalog.get('avatars') or [], catalog.get('voices') or []
    if not avatars or not voices:
        st.info('Загрузите доступных ведущих и голоса из вашего аккаунта HeyGen.')
        return None
    speakers = []
    for index in range(count):
        avatar = st.selectbox(f'Участник {index + 1}', avatars,
                              format_func=lambda item: item.get('name') or item.get('id', ''), key=f'{key}_avatar_{index}')
        voice = st.selectbox(f'Голос участника {index + 1}', voices,
                             format_func=lambda item: item.get('name') or item.get('id', ''), key=f'{key}_voice_{index}')
        supported = avatar.get('supported_api_engines') or []
        if not supported:
            detail_key = key + '_detail_' + str(avatar.get('id'))
            if detail_key not in st.session_state:
                try:
                    st.session_state[detail_key] = client.avatar(avatar.get('id'))
                except Exception:
                    st.error('Не удалось проверить возможности выбранного ведущего.')
                    return None
            supported = st.session_state[detail_key].get('supported_api_engines') or []
        engines = [engine for engine in ('heygen-avatar-6', 'heygen-avatar-6-turbo', 'avatar_v', 'avatar_iv') if engine in supported]
        if not engines:
            st.warning('Для этого образа HeyGen не подтвердил поддерживаемую модель. Выберите другой образ.')
            return None
        engine = st.selectbox(f'Модель участника {index + 1}', engines, key=f'{key}_engine_{index}')
        speakers.append({'avatar_id': avatar.get('id') or avatar.get('avatar_id'),
                         'voice_id': voice.get('id') or voice.get('voice_id'), 'engine': engine})
    return speakers


def _dialogue(ask_ai, brief, count):
    answer = ask_ai(
        'Подготовь только JSON {"dialogue":[{"speaker":0,"text":"реплика"}]}. '
        f'Участников {count}. Нумерация с нуля. Каждый участник должен говорить. '
        'Сохрани утверждённые реплики дословно. Убери описания камеры из речи. До 50 реплик.',
        brief, model=os.getenv('KADRIAN_CHAT_MODEL', 'gpt-6.1-terra'), reasoning_effort='medium',
    )
    return json.loads(answer)['dialogue']


def _quote(config, provider, kind, seconds, speakers=None):
    if provider == 'shotstack':
        rate = config.get('KADRIAN_SHOTSTACK_USD_PER_MINUTE')
    elif kind == 'scene':
        rate = config.get('KADRIAN_HEYGEN_VIDEO_USD_PER_MINUTE')
    else:
        rates = {'heygen-avatar-6': config.get('KADRIAN_HEYGEN_AVATAR6_USD_PER_MINUTE'),
                 'heygen-avatar-6-turbo': config.get('KADRIAN_HEYGEN_AVATAR6_TURBO_USD_PER_MINUTE'),
                 'avatar_v': config.get('KADRIAN_HEYGEN_AVATAR5_USD_PER_MINUTE'),
                 'avatar_iv': config.get('KADRIAN_HEYGEN_AVATAR4_USD_PER_MINUTE')}
        try:
            rate = max(float(rates[person.get('engine', 'heygen-avatar-6')]) for person in speakers or [])
        except (TypeError, ValueError):
            return None
    try:
        rate = float(rate)
        if not math.isfinite(rate) or rate <= 0:
            return None
    except (TypeError, ValueError):
        return None
    return round(math.ceil(seconds / 60) * rate, 2)


def render_production(owner_id, project, store, ask_ai, chat, route, style, key):
    cfg = _config()
    if route == 'music':
        from content_studio_music_ui import render_music_tool
        music_prompt_key = f"studio_music_{owner_id}_{project['id']}_prompt"
        if music_prompt_key not in st.session_state and chat.get('brief'):
            st.session_state[music_prompt_key] = chat['brief']
        render_music_tool(owner_id, project, store)
        return
    if route == 'assembly':
        _assembly(owner_id, project, store, cfg, style, key)
        return
    if not cfg.get('HEYGEN_API_KEY') and not cfg.get('HEYGEN_KEY'):
        st.info('Для этого формата нужен HEYGEN_API_KEY в настройках сервера студии.')
        return
    client = HeyGenClient(cfg.get('HEYGEN_API_KEY') or cfg.get('HEYGEN_KEY'))
    aspect = st.selectbox('Кадр', ['9:16', '16:9'], key=key + '_aspect')
    speakers = None
    seconds = 30
    if route == 'scene':
        seconds = st.slider('Длительность сцены, секунд', 5, 30, 15, key=key + '_seconds')
        prompt = st.text_area('Задание для сцены', value=chat.get('brief') or '', max_chars=32000, key=key + '_scene_prompt')
        if style == 'cartoon':
            prompt = 'Создай анимационный фильм в согласованной технике.\n' + prompt
        spec = {'kind': 'scene', 'title': 'Сцена Кадриана', 'prompt': prompt,
                'duration': seconds, 'aspect_ratio': aspect}
        references = [entry for role, entry in chat.get('messages', [])
                      if role == 'assistant' and isinstance(entry, dict) and entry.get('storage_path')]
        selected_image = st.selectbox('Опорный кадр', [None] + list(range(len(references))),
                                      format_func=lambda index: 'Создать сцену по тексту' if index is None else f'Эскиз {index + 1}',
                                      key=key + '_reference_image')
        if selected_image is not None:
            spec['reference_asset'] = references[selected_image]
            st.caption('HeyGen начнёт сцену с выбранного эскиза. Форма кадра будет взята из изображения.')
    else:
        count = 1 if route == 'presenter' else 2
        if count == 2:
            st.caption('Интервью со сменой планов двух ведущих. Каждый участник получает свой голос и реплики.')
        speakers = _speakers(client, count, key)
        if not speakers:
            return
        brief = chat.get('brief') or ''
        if st.session_state.get(key + '_dialogue_brief') != brief:
            st.session_state.pop(key + '_dialogue', None)
        if st.button('Подготовить реплики из обсуждения', key=key + '_dialogue_prepare', disabled=not brief):
            try:
                with st.spinner('Распределяю реплики…'):
                    dialogue = _dialogue(ask_ai, brief, count)
                heygen_payload({'kind': route, 'speakers': speakers, 'dialogue': dialogue})
                st.session_state[key + '_dialogue'] = dialogue
                st.session_state[key + '_dialogue_brief'] = brief
                st.session_state[key + '_dialogue_revision'] = uuid4().hex
            except Exception:
                st.error('Не удалось распределить реплики. Уточните их в чате и повторите подготовку.')
        dialogue = st.session_state.get(key + '_dialogue')
        if not dialogue:
            return
        edited = []
        revision = st.session_state.get(key + '_dialogue_revision', 'initial')
        for index, line in enumerate(dialogue):
            who = st.selectbox(f'Кто говорит реплику {index + 1}', list(range(count)),
                               index=line['speaker'], format_func=lambda value: f'Участник {value + 1}',
                               key=f'{key}_{revision}_line_speaker_{index}')
            text = st.text_area(f'Реплика {index + 1}', value=line['text'], max_chars=5000,
                                key=f'{key}_{revision}_line_text_{index}')
            edited.append({'speaker': who, 'text': text})
        spec = {'kind': route, 'title': 'Интервью Кадриана' if count == 2 else 'Ведущий Кадриана',
                'speakers': speakers, 'dialogue': edited, 'aspect_ratio': aspect}
        # A planning estimate. Provider billing uses the actual rendered duration.
        seconds = max(15, sum(len(line['text'].split()) for line in edited) / 1.8)
    quote = _quote(cfg, 'heygen', route, seconds, speakers)
    _confirm(store, owner_id, project['id'], 'heygen', spec, style, quote, key)


def _confirm(store, owner_id, project_id, provider, spec, style, quote, key):
    if quote is None:
        st.info('Перед запуском нужно записать актуальную стоимость выбранной модели в настройках студии. Смета пока недоступна.')
        return
    st.info(f'Оценка производства — {quote:.2f} $. Рассчитана по сохранённому тарифу с округлением длительности до минуты. '
            'Фактический расход зависит от длительности результата. Ответы ИИ оплачиваются отдельно.')
    from content_factory_billing import billing_enforced, usd_to_eur_cents, format_eur
    quote_cents = None
    if billing_enforced():
        try:
            quote_cents = usd_to_eur_cents(quote)
            st.caption('С баланса Агентства будет списано ' + format_eur(quote_cents) + '.')
        except Exception:
            st.error('Не удалось рассчитать стоимость в валюте баланса. Запуск временно недоступен.')
            return
    fingerprint = sha256(json.dumps(spec, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    token_key = key + '_submission_' + fingerprint
    request_id = st.session_state.setdefault(token_key, uuid4().hex)
    if st.button('Согласовать и запустить', type='primary', key=key + '_provider_submit'):
        try:
            job = enqueue_provider(store, owner_id, project_id, provider, spec, style, request_id, quote, quote_cents)
            st.success('Задание сохранено. Фоновый сервис выполнит его, готовый файл появится в библиотеке.')
            st.caption('Номер задания ' + job['id'])
        except Exception:
            st.error('Не удалось подтвердить сохранение задания. Обновите библиотеку перед повтором.')


def _assembly(owner_id, project, store, cfg, style, key):
    if not cfg.get('SHOTSTACK_API_KEY'):
        st.info('Для облачной сборки нужен SHOTSTACK_API_KEY в настройках сервера студии. Обычная сборка Runway продолжает использовать FFmpeg.')
        return
    jobs = store.list_jobs(owner_id, project['id'], limit=100)
    videos, music = [], []
    for job in jobs:
        for asset in job.get('assets') or []:
            item = {'label': str((job.get('content') or {}).get('title') or job.get('topic') or asset.get('filename')),
                    'asset': asset}
            if asset.get('mime_type') == 'video/mp4':
                videos.append(item)
            elif asset.get('mime_type') == 'audio/mpeg':
                music.append(item)
    if not videos:
        st.info('Готовые видео выбранного проекта появятся здесь для сборки.')
        return
    selected = st.multiselect('Фрагменты в порядке монтажа', range(len(videos)),
                              format_func=lambda index: videos[index]['label'], key=key + '_clips')
    clips = []
    for order, index in enumerate(selected):
        duration = st.number_input(f'Фрагмент {order + 1}, секунд', min_value=0.1, max_value=600.0,
                                   value=5.0, key=f'{key}_duration_{index}')
        clips.append({'asset': videos[index]['asset'], 'duration': duration})
    track = st.selectbox('Музыкальная дорожка', [None] + list(range(len(music))),
                         format_func=lambda index: 'Без дополнительной музыки' if index is None else music[index]['label'],
                         key=key + '_music_track')
    spec = {'kind': 'assembly', 'title': 'Монтаж Кадриана', 'clips': clips,
            'aspect_ratio': st.selectbox('Формат сборки', ['9:16', '16:9'], key=key + '_assembly_aspect')}
    if track is not None:
        spec['music_asset'] = music[track]['asset']
    if clips:
        seconds = sum(clip['duration'] for clip in clips)
        _confirm(store, owner_id, project['id'], 'shotstack', spec, style,
                 _quote(cfg, 'shotstack', 'assembly', seconds), key)


@st.fragment(run_every=30)
def render_provider_progress(store, owner_id, project_id):
    try:
        jobs = [job for job in store.list_jobs(owner_id, project_id, limit=30) if job.get('kadrian_provider')]
    except Exception:
        st.caption('Состояние последнего заказа временно недоступно.')
        return
    if not jobs:
        return
    job = jobs[0]
    state = job.get('state')
    if state in {'queued', 'generating'}:
        st.info((job.get('progress') or {}).get('message') or 'Заказ ожидает фонового сборщика.')
    elif job.get('error'):
        st.warning(str(job['error']))
    elif state == 'ready' and job.get('assets'):
        try:
            assets = store.resolve_assets(owner_id, job['assets'])
            for asset in assets:
                if asset.get('mime_type') == 'video/mp4':
                    st.video(asset['url'])
        except Exception:
            st.caption('Готовое видео временно недоступно для просмотра.')
