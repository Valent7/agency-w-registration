"""Visible, durable selection of already-finished clips and owner-uploaded music."""
import streamlit as st
from kadrian_editing import (_merge, assemble_music, choose_music, default_sources,
                             edit_choices, pause_production, STOP_REPLY)


def render_edit_controls(store, owner, project, key, chat):
    project_id = project['id']
    if st.button('⏹ Остановить создание сцен', key=key+'_stop'):
        try:
            pause_production(store, owner, project_id)
            st.success(STOP_REPLY)
        except Exception:
            st.error('Остановку пока не удалось подтвердить. Не отправляйте новые заказы до проверки.')
            return [],None,False
        st.rerun()
    current = store.get_project(owner, project_id)
    if current.get('kadrian_production_paused'):
        st.info('Новые сцены остановлены. Монтаж готовых сцен с вашей музыкой остаётся доступен.')
    sources, music = edit_choices(store, owner, project_id)
    source_map = {j['id']:j for j in sources}
    music_map = {j['id']:j for j in music}
    source_key, music_key = key+'_edit_sources', key+'_edit_music'
    if chat.pop('refresh_music_selection', False):
        st.session_state.pop(music_key, None)
    if source_key in st.session_state:
        st.session_state[source_key] = [i for i in st.session_state[source_key] if i in source_map]
    if music_key in st.session_state and st.session_state[music_key] not in music_map:
        st.session_state.pop(music_key, None)
    def title(job):
        return str((job.get('content') or {}).get('title') or job.get('topic') or job['id'])[:100] + ' · ' + str(job.get('created_at',''))[:16]
    defaults = [i for i in current.get('kadrian_edit_source_ids') or [] if i in source_map] or default_sources(sources)
    selected_track = current.get('kadrian_music_selected')
    with st.expander('Монтаж готовых сцен и ваша музыка', expanded=bool(music)):
        st.caption('Соединяет выбранные готовые фрагменты и накладывает ваш трек. Runway, HeyGen и генератор музыки не запускаются.')
        chosen = st.multiselect('Готовые фрагменты в порядке монтажа', list(source_map),
                                default=defaults, format_func=lambda i:title(source_map[i]),key=source_key)
        music_ids = list(music_map)
        index = music_ids.index(selected_track) if selected_track in music_ids else 0
        track = st.selectbox('Ваша музыка', music_ids, index=index if music_ids else None,
                             format_func=lambda i:title(music_map[i]),key=music_key)
        if track:
            try:
                audio_asset = next(a for a in music_map[track]['assets'] if str(a.get('mime_type','')).startswith('audio/'))
                audio_preview = store.resolve_assets(owner,[audio_asset])[0]
                st.audio(audio_preview.get('url') or audio_preview.get('path'))
            except Exception:
                st.caption('Прослушивание временно недоступно, выбранный трек сохранён.')
            st.caption('Выбран именно этот сохранённый трек; первая музыка автоматически не подставляется.')
        replace_audio = st.checkbox('Заменить весь прежний звук, включая речь', key=key+'_edit_replace_audio')
        if st.button('Смонтировать выбранные сцены с этой музыкой', key=key+'_edit_submit', disabled=not chosen or not track):
            try:
                with st.spinner('Соединяю готовые сцены и накладываю вашу музыку…'):
                    job = assemble_music(store,owner,project_id,chosen,music_map[track],replace_audio=replace_audio)
                st.success('Монтаж готов. Файл сохранён в готовых работах.')
                for asset in store.resolve_assets(owner,job['assets']):
                    st.video(asset.get('url') or asset.get('path'))
            except Exception:
                st.error('Монтаж не завершён. Исходники сохранены, новые сцены не создавались.')
    changes = {}
    if chosen != current.get('kadrian_edit_source_ids'):
        changes['kadrian_edit_source_ids'] = chosen
    if track and track != current.get('kadrian_music_selected'):
        changes['kadrian_music_selected'] = track
    if changes:
        _merge(store,owner,project_id,lambda p: changes)
    return chosen, track, replace_audio


def execute_edit_command(store, owner, project_id, prompt, source_ids, selected_music, replace_audio=False):
    clips, music = edit_choices(store,owner,project_id)
    if not source_ids:
        raise ValueError('Нет выбранных готовых сцен. Укажите их в разделе монтажа; новые сцены не запускаются.')
    project = store.get_project(owner,project_id)
    track = choose_music(project,music,prompt,selected_music)
    job = assemble_music(store,owner,project_id,source_ids,track,replace_audio=replace_audio)
    return 'Монтаж готов. Использована музыка «' + str((track.get('content') or {}).get('title','')) + '». Новые сцены не создавались. Файл — в готовых работах.'
