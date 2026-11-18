"""Owner controls. Imported text/model output never invokes these approval actions."""
import json
from uuid import uuid4
import streamlit as st

from agent_memory import Memory, Principal, Scope, digest
from agent_memory_runtime import project_memory, migrate_legacy
from agent_memory_sync import import_local


def render_memory(store, owner_id, project):
    key = 'memory_v1_' + project['id']
    with st.expander('Версионная память и проверенные правила'):
        try:
            project_mem = project_memory(store, owner_id, project, kind='owner')
            scope_name = st.selectbox('Область памяти', ['Только этот проект', 'Кадриан: общие правила', 'Все агенты: общие правила'], key=key+'_scope')
            scope = (project_mem.scope if scope_name == 'Только этот проект' else
                     Scope(owner_id, 'kadrian' if scope_name.startswith('Кадриан:') else '*', 'owner'))
            mem = Memory(project_mem.repository, Principal(owner_id, 'owner:'+str(owner_id), 'owner'), scope)
            snap = mem.read(); st.caption('Версия '+str(snap['revision'])+'. Общие правила вступают в силу только после проверки и вашего утверждения.')
            st.caption('Не переносите клиентские сведения в общие правила. Статус сервиса «готово» не подтверждает качество результата.')
            entries = snap['document']['entries']
            for entry in entries.values():
                if entry['kind'] == 'rule':
                    st.write(entry['text']); st.caption(entry['status'])
            with st.form(key+'_record'):
                kind = st.selectbox('Новая запись', ['feedback', 'analysis', 'correction', 'fact', 'hypothesis', 'unknown', 'decision', 'rule'])
                text = st.text_area('Текст записи')
                related = st.multiselect('Связанные записи', list(entries), format_func=lambda i: entries[i]['text'][:65])
                if st.form_submit_button('Сохранить запись'):
                    mem.record(kind, text, source='owner-ui', key=uuid4().hex, expected=snap['revision'], related=related)
                    st.rerun()
            rules = {i:e for i,e in entries.items() if e['kind']=='rule' and e['status'] in {'proposed','validated'}}
            if rules:
                rid = st.selectbox('Проверяемое правило', list(rules), format_func=lambda i: rules[i]['text'], key=key+'_rule')
                with st.form(key+'_verify'):
                    evidence = st.text_area('Что проверено и где находится подтверждение')
                    passed = st.checkbox('Я проверила результат: правило прошло проверку')
                    if st.form_submit_button('Записать проверку'):
                        mem.record('check', evidence, source='owner-observation', key=uuid4().hex,
                                   expected=snap['revision'], related=[rid],
                                   evidence={'passed':passed, 'rule_hash':digest(rules[rid]['text']), 'method':'owner-observation'})
                        st.rerun()
                checks = [i for i,e in entries.items() if e['kind']=='check' and rid in e.get('related',[]) and e['evidence'].get('passed') is True]
                if rules[rid]['status']=='proposed' and checks and st.button('Подготовить правило к утверждению', key=key+'_validate'):
                    mem.validate_rule(rid, checks, expected=snap['revision']); st.rerun()
                if rules[rid]['status']=='validated':
                    approved = st.checkbox('Утверждаю именно этот текст для выбранной области памяти', key=key+'_approve_confirm')
                    if approved and st.button('Утвердить правило', key=key+'_approve'):
                        mem.approve_rule(rid, expected=snap['revision']); st.rerun()
            bundle = mem.export()
            from agent_memory_backup import encrypt_backup, decrypt_backup
            if store.fernet_key:
                st.download_button('Резервная копия памяти (зашифрована)', encrypt_backup(bundle, store.fernet_key),
                                   file_name='agent-memory-backup.enc', mime='application/octet-stream', key=key+'_backup')
            else:
                st.caption('Зашифрованная резервная копия требует уже настроенного серверного ключа шифрования.')
            uploaded = st.file_uploader('Контролируемый импорт: локальный JSON или зашифрованная резервная копия', type=['json','enc'], key=key+'_upload')
            if uploaded:
                if uploaded.size > 4_000_000:
                    st.error('Слишком большой пакет.'); return
                candidate = (decrypt_backup(uploaded.getvalue(), store.fernet_key) if uploaded.name.endswith('.enc')
                             else json.loads(uploaded.getvalue()))
                st.caption('Импорт не изменяет AGENTS.md и не активирует правила. Конфликт версий остановит импорт.')
                confirm = st.checkbox('Проверила область и разрешаю импорт в выбранную память', key=key+'_import_confirm')
                if confirm and st.button('Импортировать', key=key+'_import'):
                    if uploaded.name.endswith('.enc'):
                        mem.restore(candidate)
                    else:
                        import_local(mem, candidate)
                    st.rerun()
            if scope == project_mem.scope and st.button('Перенести старые заметки и переписку без удаления оригиналов', key=key+'_legacy'):
                migrate_legacy(store, owner_id, project); st.rerun()
        except Exception:
            st.error('Операция памяти не подтверждена. Обновите данные: возможен конфликт версии, недоступность хранилища или неверный пакет. Существующая история сохранена.')
