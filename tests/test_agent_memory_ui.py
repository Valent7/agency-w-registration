"""Real Streamlit reruns on synthetic local memory; no external services."""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from streamlit.testing.v1 import AppTest
from content_factory_store import SQLiteStore
from agent_memory_store import Repository


class MemoryUiTests(unittest.TestCase):
    def test_owner_check_and_separate_approval_are_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'synthetic.sqlite3';store=SQLiteStore(path)
            project=store.save_project(11,{'name':'Synthetic UI project'})
            Repository(store,initialize=True)
            selected = patch.dict('os.environ', {'AGENCY_MEMORY_V1':'1','AGENCY_MEMORY_V1_OWNERS':'11','AGENCY_MEMORY_V1_PROJECT_ID':project['id']})
            selected.start(); self.addCleanup(selected.stop)
            code=f'''
from content_factory_store import SQLiteStore
from agent_memory_ui import render_memory
store=SQLiteStore({str(path)!r})
render_memory(store,11,store.get_project(11,{project['id']!r}))
'''
            app=AppTest.from_string(code).run(timeout=20)
            def by_label(elements,label):return next(x for x in elements if x.label==label)
            self.assertFalse(app.exception)
            by_label(app.selectbox,'Новая запись').select('rule')
            by_label(app.text_area,'Текст записи').set_value('Verify the last frame')
            by_label(app.button,'Сохранить запись').click().run(timeout=20)
            self.assertFalse(app.exception);self.assertFalse(app.error)
            self.assertFalse(any(x.label=='Утвердить правило' for x in app.button))
            by_label(app.text_area,'Что проверено и где находится подтверждение').set_value('Owner checked synthetic last frame')
            by_label(app.checkbox,'Я проверила результат: правило прошло проверку').check()
            by_label(app.button,'Записать проверку').click().run(timeout=20)
            self.assertFalse(app.error)
            by_label(app.button,'Подготовить правило к утверждению').click().run(timeout=20)
            self.assertFalse(app.error)
            self.assertFalse(any(x.label=='Утвердить правило' for x in app.button))
            by_label(app.checkbox,'Утверждаю именно этот текст для выбранной области памяти').check().run(timeout=20)
            by_label(app.button,'Утвердить правило').click().run(timeout=20)
            self.assertFalse(app.error);self.assertFalse(app.exception)
            from agent_memory import Memory,Principal,Scope
            memory=Memory(Repository(store),Principal(11,'owner','owner'),Scope(11,'kadrian',project['id'],project['id']))
            self.assertEqual([e['text'] for e in memory.context()['rules']],['Verify the last frame'])
            self.assertEqual(store.get_project(11,project['id'])['name'],'Synthetic UI project')
