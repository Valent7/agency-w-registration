"""Optional stage-one editor inside Kadrian's authenticated project."""
from pathlib import Path
import subprocess

import streamlit as st

from content_factory_store import StoreError
from kadrian_reels import MAX_BYTES, STYLES, save_revision, save_sources


def _upload(file):
    return Path(file.name).suffix.lower().lstrip('.'), file.getvalue()


def render_reels(store, owner, project, key, *, live_draft=None):
    draft_id = live_draft['id'] if live_draft else None
    with st.container():
        st.caption('Монтаж готового клипа. Запись речи сохраняется как исходник.')
        clip = st.file_uploader('Готовый клип MP4', type=['mp4'], key=key + '_clip', max_upload_size=50)
        saved_clip = live_draft.get('live_preview_video') if live_draft else None
        use_saved = bool(saved_clip and not clip and st.checkbox('Использовать видео из предпросмотра',
                                                                 key=key + '_use_preview'))
        recorded = voice = None
        if live_draft is None:
            recorded = st.audio_input('Записать голос для ролика', key=key + '_record')
            voice = st.file_uploader('Или загрузить запись речи', type=['wav', 'mp3', 'm4a'],
                                     key=key + '_voice', max_upload_size=50)
        music = st.file_uploader('Фоновая музыка', type=['wav', 'mp3', 'm4a'],
                                 key=key + '_music', max_upload_size=50)
        saved_key = key + '_source'
        if st.button('Сохранить исходники монтажа', key=key + '_save',
                     disabled=not ((clip or use_saved) and (live_draft or recorded or voice))):
            try:
                if sum(item.size for item in (clip, recorded, voice, music) if item) > MAX_BYTES:
                    raise ValueError('Общий размер файлов должен быть до 50 МБ.')
                if live_draft:
                    original = live_draft['live_voice']
                    audio = (Path(original['filename']).suffix.lstrip('.'), store.read_asset(owner, original))
                else:
                    audio = _upload(voice) if voice else ('wav', recorded.getvalue())
                footage = ('mp4', store.read_asset(owner, saved_clip)) if use_saved else _upload(clip)
                source = save_sources(store, owner, project, clip=footage, voice=audio,
                                      music=_upload(music) if music else None, live_draft_id=draft_id)
                st.session_state[saved_key] = source['id']
            except (StoreError, ValueError) as error:
                st.error(str(error))

        try:
            jobs = store.list_jobs(owner, project, limit=1000)
        except StoreError:
            st.error('Не удалось открыть сохранённые ролики.')
            return
        sources = {job['id']: job for job in jobs if job.get('reels_sources')
                   and job.get('reels_live_draft_id') == draft_id}
        if not sources:
            return
        source_ids = list(sources)
        selected = st.session_state.get(saved_key)
        source_id = st.selectbox('Сохранённые исходники', source_ids,
                                 index=source_ids.index(selected) if selected in sources else 0,
                                 format_func=lambda job_id: f'{sources[job_id]["created_at"][:16]} · {job_id[:8]}',
                                 key=key + '_pick_' + str(selected))
        if live_draft and sources[source_id]['reels_sources']['voice']['sha256'] != live_draft['live_voice']['sha256']:
            st.caption('Этот монтаж использует предыдущую запись. Для монтажа с новой записью сохраните исходники снова.')
        revisions = [job for job in jobs if job.get('kadrian_reels_revision') == source_id and job.get('assets')]
        settings = revisions[0]['reels_settings'] if revisions else {}
        scope = key + '_' + source_id
        preview_key = scope + '_preview'
        with st.form(scope + '_edit'):
            title = st.text_input('Заголовок', value=settings.get('title', ''), max_chars=60, key=scope + '_title')
            caption = st.text_input('Подпись', value=settings.get('caption', ''), max_chars=100, key=scope + '_caption')
            styles = list(STYLES)
            style = st.selectbox('Стиль', styles, index=styles.index(settings.get('style', styles[0])),
                                 key=scope + '_style')
            music_level = st.slider('Громкость фона, %', 0, 15, settings.get('music_level', 8),
                                    key=scope + '_volume')
            submitted = st.form_submit_button('Собрать версию')
        if submitted:
            try:
                with st.spinner('Кадриан собирает ролик…'):
                    revision = save_revision(store, owner, project, source_id, title=title, caption=caption,
                                             style=style, music_level=music_level)
                revisions = [revision] + [job for job in revisions if job['id'] != revision['id']]
                st.session_state[preview_key] = revision['id']
                st.success('Версия сохранена.')
            except (StoreError, ValueError) as error:
                st.error(str(error))
            except (subprocess.SubprocessError, OSError):
                st.error('Монтаж не завершился. Проверьте исходники и попробуйте снова.')
        if revisions:
            versions = {job['id']: job for job in revisions}
            if st.session_state.get(preview_key) not in versions:
                st.session_state[preview_key] = next(iter(versions))
            version = st.selectbox('Версия для просмотра', list(versions),
                                   format_func=lambda job_id: versions[job_id]['reels_settings']['title'] + ' · ' + job_id[:8],
                                   key=preview_key)
            try:
                preview = store.read_asset(owner, versions[version]['assets'][0])
            except StoreError:
                st.error('Не удалось открыть MP4. Попробуйте снова.')
                return
            st.video(preview, format='video/mp4', width=360)
            st.download_button('Скачать MP4 9:16', preview, file_name='reel.mp4', mime='video/mp4',
                               key=scope + '_download', on_click='ignore')
