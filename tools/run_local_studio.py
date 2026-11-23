"""Start the existing Content Studio against private local SQLite storage."""
import os
from pathlib import Path
import sys
from unittest.mock import patch

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from content_factory_core import ContentFactoryService
from content_factory_store import SQLiteStore
from content_studio_ui import render_content_studio


def offline(*args, **kwargs):
    st.info('В локальном кабинете доступны операции с локальными материалами.')


def main():
    st.set_page_config(page_title='Студия «Контент» · локальный кабинет', layout='wide')
    st.caption('Локальный кабинет на этом Mac · 127.0.0.1:8765')
    data = Path(os.environ.get('KADRIAN_LOCAL_DATA', str(Path.home() / '.agency-w-content-local')))
    local_tools = {
        'KADRIAN_WHISPER_CLI': data / 'tools/whisper-build/bin/whisper-cli',
        'KADRIAN_WHISPER_MODEL': data / 'models/ggml-base.bin',
    }
    for setting, path in local_tools.items():
        if path.is_file():
            os.environ.setdefault(setting, str(path))
    store = SQLiteStore(data / 'preview.sqlite3')
    owner = 77
    if not store.list_projects(owner):
        store.save_project(owner, {'name': 'Агентство W · локальная проверка'})
    st.session_state.setdefault(f'content_studio_{owner}_tool', 'video')
    with patch('content_factory_autopilot_ui._runtime',
               return_value=(store, ContentFactoryService(store), {})), \
         patch('agency_access_control.is_agency_access_revoked', return_value=False):
        render_content_studio(owner, 'Локальная проверка', offline, offline, offline, offline,
                              render_manual=offline)


if __name__ == '__main__':
    main()
