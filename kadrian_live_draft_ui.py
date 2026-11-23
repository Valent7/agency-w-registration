"""Guided preparation inside the existing live-video option."""
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

from PIL import Image, ImageOps
import streamlit as st

from content_factory_store import StoreError
from kadrian_live_draft import (
    MARKER, create, editorial, local_editor_ready, local_speech_ready, transcribe, update,
)
from kadrian_reels_ui import render_reels
from kadrian_reels import _graphics


def _save(store, owner, project, job, action, value=None):
    update(store, owner, project, job, action, value)
    st.rerun()


def _text_stage(store, owner, project, draft, key):
    brief = draft.get('live_brief', '')
    with st.expander('Замысел ролика', expanded=not brief):
        with st.form(key + '_brief_form'):
            text = st.text_area('Какой ролик вы хотите и что хотите сказать зрителям?', brief,
                                max_chars=6000, key=key + '_brief')
            speech = st.audio_input('Или расскажите голосом', key=key + '_brief_audio',
                                   disabled=not local_speech_ready())
            prepare = st.form_submit_button('Подготовить сценарий')
        if not local_speech_ready():
            st.caption('Для голосовых указаний нужен локальный модуль распознавания. Здесь можно написать своими словами.')
        if not local_editor_ready():
            st.caption('Доступна базовая обработка текста: убирает явные повторы и слова-паразиты.')
        if prepare:
            text = (text.strip() + '\n' + transcribe(speech.getvalue())).strip() if speech else text.strip()
            if not text:
                raise ValueError('Расскажите, что хотите донести до зрителей.')
            script = editorial(text)
            updated = update(store, owner, project, draft, 'brief', text)
            _save(store, owner, project, updated, 'script', script)
    if not brief:
        return
    script = draft.get('live_script', '')
    if st.session_state.pop(key + '_script_saved', False):
        st.success('Новая версия сценария сохранена.')
    with st.form(key + '_script_form_' + sha256(script.encode()).hexdigest()[:12]):
        edited = st.text_area('Сценарий', script, max_chars=6000)
        save = st.form_submit_button('Сохранить правку')
        approve = st.form_submit_button('Утверждаю сценарий', disabled=not script)
    if save:
        _save(store, owner, project, draft, 'script', edited)
    if approve:
        if edited.strip() != script:
            draft = update(store, owner, project, draft, 'script', edited)
        _save(store, owner, project, draft, 'approve_script')
    if draft.get('live_script_ok'):
        st.caption('Сценарий утверждён. Новая редакция потребует повторного утверждения.')
    if draft.get('live_title_cues'):
        with st.expander('Акценты для титров'):
            for cue in draft['live_title_cues']:
                st.text(cue)
    with st.expander('Указания Кадриану по сценарию'):
        ready = local_editor_ready()
        with st.form(key + '_rewrite'):
            direction = st.text_area('Что изменить в сценарии?', max_chars=2000, disabled=not ready)
            voice = st.audio_input('Наговорить правки', key=key + '_rewrite_audio',
                                   disabled=not local_speech_ready())
            rewrite = st.form_submit_button('Переписать сценарий', disabled=not ready)
        if not ready:
            st.caption('Доступно ручное редактирование. Для правок по указаниям нужен локальный редактор.')
        if rewrite:
            note = (direction.strip() + '\n' + transcribe(voice.getvalue())).strip() if voice else direction.strip()
            if not note:
                raise ValueError('Укажите, что хотите изменить в сценарии.')
            with st.spinner('Кадриан переписывает сценарий…'):
                revised = editorial(brief, current=script, direction=note)
            update(store, owner, project, draft, 'script', revised)
            st.session_state[key + '_script_saved'] = True
            st.rerun()


def _visual_preview(store, owner, project, draft, key):
    st.subheader('Как будет выглядеть Reels')
    rendered_look = draft.get('live_rendered_look')
    look_current = (rendered_look and draft.get('live_rendered_look_for') ==
                    sha256(draft.get('live_look', '').encode()).hexdigest())
    photo = store.read_asset(owner, rendered_look if look_current else draft['live_photo'])
    if look_current:
        st.image(photo, caption='Подготовленный образ в полный рост', width=420)
        st.caption('Макеты кадров 9:16 на подготовленном образе.')
    else:
        st.caption('Макет кадров 9:16 на исходной фотографии. Одежда, фон и свет из описания появятся после подготовки образа.')
    cues = draft.get('live_title_cues') or []
    captions = cues
    opening = captions[0] if captions else 'Начало ролика'
    middle = captions[1] if len(captions) > 1 else 'Главная мысль'
    closing = captions[-1] if len(captions) > 2 else 'Финальный кадр'
    with Image.open(BytesIO(photo)) as original, TemporaryDirectory() as folder:
        portrait = ImageOps.exif_transpose(original).convert('RGB')
        artwork = Path(folder) / 'overlay.png'
        stills = []
        for line, caption in [(opening, ''), (middle, ''), (closing, '')]:
            frame = Image.new('RGBA', (1080, 1920), '#182329')
            fitted = ImageOps.contain(portrait, (1080, 1920))
            frame.paste(fitted, ((1080 - fitted.width) // 2, (1920 - fitted.height) // 2))
            _graphics(artwork, line, caption, 'Кинематографический')
            with Image.open(artwork) as overlay:
                frame.alpha_composite(overlay)
            stills.append(frame.resize((270, 480), Image.Resampling.LANCZOS).convert('RGB'))
        grid = Image.new('RGB', (830, 480), '#182329')
        for index, (column, still, label) in enumerate(zip(
            st.columns(3), stills, ('Вступление', 'Главная мысль', 'Финал')
        )):
            column.image(still, caption=label, width='stretch')
            grid.paste(still, (280 * index, 0))
        download = BytesIO()
        grid.save(download, 'PNG')
    st.download_button('Скачать макет кадров PNG', download.getvalue(), file_name='reels-frames.png',
                       mime='image/png', key=key + '_download_frames', on_click='ignore')
    with st.form(key + '_visual_upload'):
        video = st.file_uploader('Загрузить видео Reels (MP4)', type=['mp4'], max_upload_size=50)
        frames = st.file_uploader('Загрузить картинки или кадры Reels', type=['png', 'jpg', 'jpeg', 'webp'],
                                  accept_multiple_files=True, max_upload_size=15)
        save = st.form_submit_button('Сохранить видео и кадры')
    if save:
        _save(store, owner, project, draft, 'visuals',
              (video.getvalue() if video else None, [frame.getvalue() for frame in frames]))
    if draft.get('live_preview_video'):
        st.video(store.read_asset(owner, draft['live_preview_video']), format='video/mp4', width=360)
    if draft.get('live_preview_frames'):
        st.image([store.read_asset(owner, frame) for frame in draft['live_preview_frames']], width=160)

def _final_preview(store, owner, draft, key):
    if draft.get('live_final_video'):
        final_asset = draft['live_final_video']
        final_video = store.read_asset(owner, final_asset)
        st.subheader('Готовый Reels')
        st.video(final_video, format='video/mp4', width=360)
        st.download_button('Скачать готовый Reels', final_video, file_name=final_asset['filename'],
                           mime='video/mp4', key=key + '_download_final', on_click='ignore')
        scene = draft.get('live_final_scene_preview') or {}
        if scene.get('final_path') == final_asset.get('storage_path') and scene.get('video'):
            with st.expander('Предпросмотр третьей сцены'):
                st.video(store.read_asset(owner, scene['video']), format='video/mp4', width=360)
        if draft.get('live_final_storyboard'):
            st.image(store.read_asset(owner, draft['live_final_storyboard']), caption='Макеты готового ролика')


def _materials(store, owner, project, draft, key):
    if not draft.get('live_script_ok'):
        return
    look = draft.get('live_look', '')
    if not look:
        if st.button('Предложить образ', key=key + '_propose'):
            _save(store, owner, project, draft, 'look',
                  editorial(draft['live_brief'], current=draft['live_script'], look=True))
        return
    version = sha256(look.encode()).hexdigest()[:12]
    if st.session_state.pop(key + '_look_saved', False):
        st.success('Новая версия образа сохранена.')
    with st.form(key + '_look_form_' + version):
        edited = st.text_area('Образ, одежда, причёска, фон и свет', look, max_chars=6000)
        save = st.form_submit_button('Сохранить образ')
        approve = st.form_submit_button('Утверждаю образ')
    if save or approve:
        if edited.strip() != look:
            draft = update(store, owner, project, draft, 'look', edited)
        if approve:
            _save(store, owner, project, draft, 'approve_look')
        st.rerun()
    _visual_preview(store, owner, project, draft, key)
    _final_preview(store, owner, draft, key)
    ready = local_editor_ready()
    with st.form(key + '_revise_look_' + version):
        direction = st.text_area('Что изменить в образе или стиле?', max_chars=2000, disabled=not ready)
        spoken = st.audio_input('Наговорить изменения образа', disabled=not (ready and local_speech_ready()))
        revise = st.form_submit_button('Изменить образ', disabled=not ready)
    if revise:
        note = (direction.strip() + '\n' + transcribe(spoken.getvalue())).strip() if spoken else direction.strip()
        if not note:
            raise ValueError('Напишите или наговорите, что изменить в образе.')
        with st.spinner('Кадриан обновляет образ…'):
            revised = editorial(draft['live_brief'], current=draft['live_script'], look=True,
                                previous_look=look, direction=note)
        update(store, owner, project, draft, 'look', revised)
        st.session_state[key + '_look_saved'] = True
        st.rerun()
    if not draft.get('live_look_ok'):
        return
    st.caption('Образ утверждён. Запишите сценарий своим голосом или приложите готовую запись.')
    voice = st.audio_input('Записать голос для ролика', key=key + '_voice')
    upload = st.file_uploader('Загрузить голос', type=['wav', 'mp3', 'm4a'],
                              max_upload_size=25, key=key + '_voice_file')
    if st.button('Сохранить оригинал голоса', key=key + '_save_voice', disabled=not (voice or upload)):
        value = (Path(upload.name).suffix.lower().lstrip('.'), upload.getvalue()) if upload else ('wav', voice.getvalue())
        _save(store, owner, project, draft, 'voice', value)
    asset = draft.get('live_voice')
    if not asset:
        return
    st.audio(store.read_asset(owner, asset), format=asset['mime_type'])
    _artistic_edit(store, owner, project, draft, key)
    st.info('Материалы согласованы. Для оживления фото требуется отдельное разрешение на расходы. '
            'Готовый клип можно смонтировать здесь.')
    render_reels(store, owner, project, key + '_edit', live_draft=draft)


def _artistic_edit(store, owner, project, draft, key):
    from kadrian_art_edit import PALETTES, render_art_edit
    from kadrian_room_matte import room_matte_available

    with st.expander('Художественный монтаж Reels', expanded=True):
        st.caption('Используйте говорящий клип, уже синхронизированный с сохранённой озвучкой.')
        speech_ready = local_speech_ready()
        if not speech_ready:
            st.caption('Художественный монтаж требует локального распознавания речи. '
                       'Базовый монтаж готового клипа доступен ниже.')
        source = draft.get('live_art_source')
        with st.form(key + '_art_edit'):
            clip = st.file_uploader('Говорящий клип MP4', type=['mp4'], max_upload_size=50)
            if source:
                st.caption('В черновике уже есть говорящий кадр.')
            picture = st.file_uploader('Фотография для вступления и перебивок', type=['png','jpg','jpeg','webp'],
                                       max_upload_size=15)
            room = None
            if room_matte_available():
                room = st.file_uploader('Фон для живых сцен', type=['png','jpg','jpeg','webp'], max_upload_size=15)
                use_room = st.checkbox('Использовать фон из черновика', value=True) if draft.get('live_art_room') else False
            else:
                use_room = False
            palette = st.selectbox('Палитра Reels', list(PALETTES))
            music = st.checkbox('Тихая инструментальная музыка', value=True)
            run = st.form_submit_button('Собрать художественный Reels', disabled=not speech_ready)
        if not run:
            return
        if not (clip or source):
            raise ValueError('Загрузите говорящий клип.')
        if not (picture or draft.get('live_art_picture') or draft.get('live_rendered_look')):
            raise ValueError('Добавьте фотографию подготовленного образа или вступительного кадра.')
        clip_data = clip.getvalue() if clip else store.read_asset(owner, source)
        voice = store.read_asset(owner, draft['live_voice'])
        existing_pic = draft.get('live_art_picture') or draft.get('live_rendered_look')
        photo = picture.getvalue() if picture else store.read_asset(owner, existing_pic)
        room_data = room.getvalue() if room else store.read_asset(owner, draft['live_art_room']) if use_room else None
        with st.spinner('Кадриан выбирает сцены по речи, анимирует графику и собирает Reels…'):
            result = render_art_edit(clip_data, voice, photo, palette, draft.get('live_title_cues', []),
                                     with_music=music, approved_script=draft["live_script"], room=room_data)
        asset = store.put_asset(owner, project, draft['id'], 'reels-motion.mp4', result, 'video/mp4')
        history = list(draft.get('live_previous_final_videos') or [])
        if draft.get('live_final_video'):
            history.append(draft['live_final_video'])
        changes = {'live_final_video':asset, 'live_final_storyboard':None, 'live_previous_final_videos':history}
        if clip:
            changes['live_art_source'] = store.put_asset(owner, project, draft['id'], 'art-source.mp4',
                                                          clip_data, 'video/mp4')
        if room:
            from kadrian_live_draft import image_extension
            ext = image_extension(room_data)
            changes['live_art_room'] = store.put_asset(owner, project, draft['id'], f'art-room.{ext}',
                                                        room_data, f'image/{ext}')
        if picture:
            from kadrian_live_draft import image_extension
            ext = image_extension(photo)
            changes['live_art_picture'] = store.put_asset(owner, project, draft['id'], f'art-picture.{ext}',
                                                           photo, f'image/{ext}')
        store.update_job(owner, draft['id'], changes, expected_updated_at=draft['updated_at'])
        st.rerun()


def render_live_draft(owner, project, store):
    project_id = project['id']
    key = f'kadrian_live_{owner}_{project_id}'
    try:
        drafts = {job['id']: job for job in store.list_jobs(owner, project_id, limit=1000)
                  if job.get(MARKER) and job.get('live_photo')}
        with st.expander('Исходная фотография', expanded=not drafts):
            with st.form(key + '_photo_form'):
                photo = st.file_uploader('Фото для ролика', type=['jpg', 'jpeg', 'png', 'webp'], max_upload_size=15)
                permission = st.checkbox('Это моё фото или у меня есть разрешение человека на фото')
                start = st.form_submit_button('Сохранить фото и начать')
            if start:
                if not photo or not permission:
                    raise ValueError('Добавьте фотографию и подтвердите разрешение на её использование.')
                draft = create(store, owner, project_id, photo.getvalue())
                st.session_state[key + '_selected'] = draft['id']
                st.rerun()
        if not drafts:
            return
        selected = st.session_state.get(key + '_selected')
        ids = list(drafts)
        draft_id = st.selectbox('Черновик живого ролика', ids, index=ids.index(selected) if selected in ids else 0,
                                format_func=lambda item: f'{drafts[item]["created_at"][:16]} · {item[:8]}',
                                key=key + '_pick_' + str(selected))
        draft = drafts[draft_id]
        scope = key + '_' + draft_id
        st.image(store.read_asset(owner, draft['live_photo']), width=170)
        if draft.get('live_recovered') and not (draft.get('live_script_ok') and draft.get('live_look')):
            st.info('Материалы восстановлены из файлов Mac. История правок и отметки согласования требуют восстановления.')
            _final_preview(store, owner, draft, scope)
        _text_stage(store, owner, project_id, draft, scope)
        _materials(store, owner, project_id, draft, scope)
        if draft.get('live_history'):
            labels = {'brief': 'Замысел', 'script': 'Редакция сценария', 'approve_script': 'Сценарий утверждён',
                      'look': 'Редакция образа', 'approve_look': 'Образ утверждён', 'voice': 'Оригинал голоса сохранён',
                      'visuals': 'Видео или кадры сохранены'}
            with st.expander('История версий'):
                history = draft['live_history']
                index = st.selectbox('Запись истории', list(reversed(range(len(history)))),
                                     format_func=lambda i: f'{i + 1}. {labels[history[i]["action"]]}',
                                     key=scope + '_history')
                if isinstance(history[index]['value'], str):
                    st.text(history[index]['value'])
    except (ValueError, StoreError) as error:
        st.error(str(error))
    except (Image.UnidentifiedImageError, subprocess.SubprocessError, OSError):
        st.error('Не удалось обработать файл или выполнить локальную обработку. Проверьте исходник и попробуйте снова.')
