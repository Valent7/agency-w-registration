"""Exercise partner controls without credentials or social API calls."""
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from streamlit.testing.v1 import AppTest


APP = '''
import copy
import streamlit as st
from unittest.mock import patch
import content_factory_autopilot_ui as ui

class FakeStore:
    def __init__(self):
        self.project = {**ui._seed_project(), 'id': 'project-1', 'owner_id': 77}
        self.actions = []
        self.connections = []
        self.tokens = {}
        self.jobs = [
            {'id': 'ready-1', 'project_id': 'project-1', 'owner_id':77,
             'platform':'instagram', 'format':'post', 'state':'ready',
             'scheduled_at':'2026-10-28T09:00:00+00:00',
             'content':{'title':'Полезный материал', 'text':'Исходный текст'}, 'assets':[]},
            {'id': 'published-1', 'project_id':'project-1', 'owner_id':77,
             'platform':'threads', 'format':'post', 'state':'published',
             'scheduled_at':'2026-10-27T09:00:00+00:00',
             'content':{'title':'Готовая публикация', 'text':'Уже опубликовано'},
             'permalink':'https://www.threads.net/@test/post/example', 'assets':[]},
            {'id': 'uncertain-1', 'project_id':'project-1', 'owner_id':77,
             'platform':'vk', 'format':'reel', 'state':'reconcile',
             'scheduled_at':'2026-10-27T12:00:00+00:00',
             'content':{'title':'Проверить результат', 'text':'Видео'},
             'checkpoint':{'publish_started':True}, 'assets':[]},
        ]
    def list_projects(self, owner_id):
        assert owner_id == 77
        return [copy.deepcopy(self.project)]
    def list_jobs(self, owner_id, project_id, limit=100):
        assert (owner_id,project_id) == (77,'project-1')
        return copy.deepcopy(self.jobs)
    def list_connections(self, owner_id, project_id):
        assert (owner_id,project_id) == (77,'project-1')
        return copy.deepcopy(self.connections)
    def save_connection(self, owner_id, project_id, platform, account_id, display_name, token, extra=None):
        assert (owner_id,project_id) == (77,'project-1')
        self.tokens[platform] = token
        self.connections = [x for x in self.connections if x['platform'] != platform]
        self.connections.append({'platform':platform,'account_id':account_id,'display_name':display_name})
        self.actions.append(('connect',owner_id,project_id,platform))
    def delete_connection(self, owner_id, project_id, platform):
        self.actions.append(('disconnect', owner_id, project_id, platform))
        self.connections = [x for x in self.connections if x['platform'] != platform]
    def resolve_assets(self, owner_id, assets):
        assert owner_id == 77
        return []

class FakeService:
    def __init__(self, store): self.store = store
    def save_project(self, owner_id, data):
        assert owner_id == 77
        self.store.actions.append(('save_project', owner_id, copy.deepcopy(data)))
        self.store.project = {**self.store.project, **data}
        return copy.deepcopy(self.store.project)
    def plan(self, owner_id, project_id, days=7):
        self.store.actions.append(('plan', owner_id, project_id, days))
        return copy.deepcopy(self.store.jobs)
    def approve(self, owner_id, job_id, content=None):
        self.store.actions.append(('approve', owner_id, job_id, copy.deepcopy(content)))
        for job in self.store.jobs:
            if job['id'] == job_id:
                job['content'] = content
                job['state'] = 'scheduled'
    def cancel(self, owner_id, job_id):
        self.store.actions.append(('cancel', owner_id, job_id))
        for job in self.store.jobs:
            if job['id'] == job_id: job['state'] = 'cancelled'
    def retry(self, owner_id, job_id):
        self.store.actions.append(('retry', owner_id, job_id))

if '_fake_store' not in st.session_state:
    st.session_state['_fake_store'] = FakeStore()
store = st.session_state['_fake_store']
def process(store, owner_id, project_id):
    store.actions.append(('process', owner_id, project_id))
    return {'generated':1,'published':0,'pending':0}
with patch.object(ui, '_runtime', return_value=(store,FakeService(store),{'storage':'local','ai_configured':True,'encryption_configured':True})), patch.object(ui, '_process_queue', side_effect=process):
    ui.render_content_autopilot(77, 'Партнёр')
'''


class AutopilotUiTests(unittest.TestCase):
    def app(self):
        result = AppTest.from_string(APP, default_timeout=15).run()
        self.assertFalse(result.exception, [item.message for item in result.exception])
        return result

    def test_renders_real_states_and_no_retry_for_uncertain_publication(self):
        app = self.app()
        self.assertEqual([item.label for item in app.tabs], ["Проект", "Площадки", "Календарь", "Материалы"])
        self.assertEqual([item.value for item in app.metric], ["0", "1", "1"])
        self.assertTrue(any("Проверьте аккаунт" in item.value for item in app.info))
        self.assertFalse(any("uncertain-1_retry" in (item.key or "") for item in app.button))
        self.assertFalse(any("uncertain-1_cancel" in (item.key or "") for item in app.button))

    def test_edited_text_is_approved_only_for_current_owner(self):
        app = self.app()
        app.text_area(key="cf_auto_77_project-1_ready-1_text").input("Отредактированный текст")
        app.button(key="cf_auto_77_project-1_ready-1_approve").click().run()
        self.assertFalse(app.exception)
        action = app.session_state["_fake_store"].actions[-1]
        self.assertEqual(action[:3], ("approve", 77, "ready-1"))
        self.assertEqual(action[3]["text"], "Отредактированный текст")

    def test_connection_is_owner_scoped_and_password_cleared(self):
        app = self.app()
        app.text_input(key="cf_auto_77_project-1_instagram_account").input("12345")
        app.text_input(key="cf_auto_77_project-1_instagram_display").input("Мой Instagram")
        app.text_input(key="cf_auto_77_project-1_instagram_token").input("secret-access-token")
        submit = next(item for item in app.button if item.key == "FormSubmitter:cf_auto_77_project-1_instagram_connection-Сохранить подключение")
        submit.click().run()
        self.assertFalse(app.exception)
        store = app.session_state["_fake_store"]
        self.assertEqual(store.actions[-1], ("connect", 77, "project-1", "instagram"))
        self.assertEqual(store.tokens["instagram"], "secret-access-token")
        self.assertEqual(app.text_input(key="cf_auto_77_project-1_instagram_token").value, "")
        visible = " ".join(item.value for kind in (app.markdown, app.caption, app.info, app.success, app.error) for item in kind)
        self.assertNotIn("secret-access-token", visible)

    def test_plan_and_processing_are_scoped_to_selected_project(self):
        app = self.app()
        app.button(key="cf_auto_77_project-1_plan").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["_fake_store"].actions[-1], ("plan", 77, "project-1", 7))
        app.button(key="cf_auto_77_project-1_process").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["_fake_store"].actions[-1], ("process", 77, "project-1"))

    def test_invalid_account_does_not_save_secret(self):
        app = self.app()
        app.text_input(key="cf_auto_77_project-1_instagram_account").input("not-an-account")
        app.text_input(key="cf_auto_77_project-1_instagram_token").input("secret-access-token")
        submit = next(item for item in app.button if item.key == "FormSubmitter:cf_auto_77_project-1_instagram_connection-Сохранить подключение")
        submit.click().run()
        self.assertFalse(app.exception)
        self.assertFalse(app.session_state["_fake_store"].tokens)
        self.assertTrue(any("числовой ID" in item.value for item in app.error))

    def test_project_save_and_pause_keep_automation_state_consistent(self):
        app = self.app()
        app.text_input(key="cf_auto_77_project-1_name").input("Семейная пекарня")
        app.checkbox(key="cf_auto_77_project-1_enabled").check()
        submit = next(item for item in app.button if item.label == "Сохранить проект")
        submit.click().run()
        self.assertFalse(app.exception)
        store = app.session_state["_fake_store"]
        self.assertEqual(store.project["name"], "Семейная пекарня")
        self.assertTrue(store.project["automation_enabled"])
        app.button(key="cf_auto_77_project-1_pause").click().run()
        self.assertFalse(app.exception)
        self.assertFalse(store.project["automation_enabled"])
        self.assertFalse(app.checkbox(key="cf_auto_77_project-1_enabled").value)

    def test_real_store_project_and_calendar_persist_without_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            script = f'''\nimport os\nfrom unittest.mock import patch\nimport content_factory_autopilot_ui as ui\nwith patch.dict(os.environ, {{'CONTENT_FACTORY_DATA_DIR': {directory!r}, 'SUPABASE_URL':'', 'SUPABASE_SECRET_KEY':'', 'OPENAI_API_KEY':''}}):\n    ui.render_content_autopilot(77, 'Партнёр')\n'''
            app = AppTest.from_string(script, default_timeout=15).run()
            self.assertFalse(app.exception)
            self.assertEqual(app.selectbox(key="cf_auto_77_selected_project").value, "__new_project__")
            next(item for item in app.button if item.label == "Создать проект").click().run()
            self.assertFalse(app.exception)
            self.assertFalse(app.error, [item.value for item in app.error])
            project_id = app.selectbox(key="cf_auto_77_selected_project").value
            self.assertNotEqual(project_id, "__new_project__")
            app.button(key=f"cf_auto_77_{project_id}_plan").click().run()
            self.assertFalse(app.exception)
            self.assertFalse(app.error, [item.value for item in app.error])
            self.assertGreater(len(app.dataframe[0].value), 0)
            self.assertTrue(app.button(key=f"cf_auto_77_{project_id}_process").disabled)
            reloaded = AppTest.from_string(script, default_timeout=15).run()
            self.assertFalse(reloaded.exception)
            self.assertEqual(reloaded.selectbox(key="cf_auto_77_selected_project").value, project_id)
            self.assertEqual(len(app.dataframe[0].value), len(reloaded.dataframe[0].value))

    def test_oauth_connect_buttons_do_not_require_partner_credentials(self):
        script = APP.replace(
            "    ui.render_content_autopilot(77, 'Партнёр')",
            "    with patch.object(ui, '_connect_url', side_effect=lambda owner, project, platform: f'https://connect.example/{platform}?assertion=signed-connect-request'):\n        ui.render_content_autopilot(77, 'Партнёр')",
        )
        app = AppTest.from_string(script, default_timeout=15).run()
        self.assertFalse(app.exception)
        links = {item.proto.label: item.proto for item in app.get("link_button")}
        self.assertIn("Подключить Instagram", links)
        self.assertIn("Подключить Threads", links)
        self.assertEqual(links["Подключить Instagram"].type, "primary")
        self.assertEqual(links["Подключить Threads"].type, "primary")
        self.assertIn("/instagram?assertion=", links["Подключить Instagram"].url)
        self.assertEqual(len([item for item in app.expander if item.label == "Подключить вручную"]), 2)
        self.assertFalse(any(item.proto.expanded for item in app.expander if item.label == "Подключить вручную"))
        self.assertTrue(all(not item.value for item in app.text_input if item.label == "Ключ доступа к публикациям"))

    def test_open_cabinet_registers_link_refresh_and_mints_a_new_short_lived_handoff(self):
        from content_factory_oauth import HANDOFF_TTL, OAuthError, _verify
        from streamlit.testing.v1.local_script_runner import LocalScriptRunner

        script = '''
import streamlit as st
from unittest.mock import patch
import content_factory_autopilot_ui as ui
from content_factory_oauth import make_connect_url

if '_test_clock' not in st.session_state:
    st.session_state['_test_clock'] = 1800000000
config = {
    'CONTENT_FACTORY_SERVICE_URL': 'https://connect.example.com',
    'CONTENT_FACTORY_OAUTH_SIGNING_KEY': 'test-only-handoff-signing-key-' + 'x' * 32,
}
def fresh_link(owner, project, platform):
    with patch('content_factory_oauth.time.time', return_value=st.session_state['_test_clock']):
        return make_connect_url(owner, project, platform, config)
st.text_input('Черновик проекта', key='unsaved_project_note')
with patch.object(ui, '_connect_url', side_effect=fresh_link):
    ui._oauth_connect_button(77, '2788cb04-7bcb-4262-8bcc-68cdcc3207ee', 'instagram')
    ui._oauth_connect_button(77, '2788cb04-7bcb-4262-8bcc-68cdcc3207ee', 'threads')
'''
        timers = []
        forward_messages = LocalScriptRunner.forward_msgs

        def capture_timers(runner):
            messages = forward_messages(runner)
            timers.extend((message.auto_rerun.fragment_id, message.auto_rerun.interval)
                          for message in messages if message.WhichOneof('type') == 'auto_rerun')
            return messages

        with patch.object(LocalScriptRunner, 'forward_msgs', capture_timers):
            app = AppTest.from_string(script, default_timeout=15).run()
        self.assertFalse(app.exception)
        self.assertEqual(len({fragment_id for fragment_id, _ in timers}), 2)
        self.assertTrue(all(0 < interval < HANDOFF_TTL for _, interval in timers))
        old_link = next(item.proto.url for item in app.get('link_button')
                        if item.proto.label == 'Подключить Instagram')
        old_assertion = parse_qs(urlsplit(old_link).query)['assertion'][0]

        # AppTest does not advance browser timers. Advance its clock and rerun
        # the registered view, then verify real OAuth TTL and owner binding.
        app.session_state['_test_clock'] = 1800000000 + HANDOFF_TTL + 1
        app.text_input(key='unsaved_project_note').input('Несохранённое описание проекта')
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(app.text_input(key='unsaved_project_note').value,
                         'Несохранённое описание проекта')
        new_link = next(item.proto.url for item in app.get('link_button')
                        if item.proto.label == 'Подключить Instagram')
        self.assertNotEqual(old_link, new_link)
        key = 'test-only-handoff-signing-key-' + 'x' * 32
        with patch('content_factory_oauth.time.time', return_value=1800000000 + HANDOFF_TTL + 1):
            with self.assertRaises(OAuthError):
                _verify(old_assertion, key, purpose='content_factory_connect',
                        platform='instagram', ttl=HANDOFF_TTL)
            current = _verify(parse_qs(urlsplit(new_link).query)['assertion'][0], key,
                              purpose='content_factory_connect', platform='instagram', ttl=HANDOFF_TTL)
        self.assertEqual(current['owner_id'], 77)
        self.assertEqual(current['project_id'], '2788cb04-7bcb-4262-8bcc-68cdcc3207ee')

    def test_integrated_factory_preserves_existing_manual_creation(self):
        script = APP.replace(
            "    ui.render_content_autopilot(77, 'Партнёр')",
            "    import content_factory_ui as existing\n    with patch.object(existing, '_load_latest_package', return_value=None):\n        existing.render_content_factory(77, 'Партнёр', lambda *a, **k: '', lambda *a, **k: {}, lambda *a, **k: {}, lambda *a, **k: {})",
        )
        app = AppTest.from_string(script, default_timeout=15).run()
        self.assertFalse(app.exception)
        self.assertTrue(any(item.label == "🚀 Автопилот: контент и публикации" for item in app.expander))
        self.assertEqual(app.button(key="cf2_start_77").label, "✨ Создать контент")
        app.button(key="cf2_start_77").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.radio(key="cf2_format_77").value, "post")
        self.assertTrue(any(item.label == "🚀 Автопилот: контент и публикации" for item in app.expander))


if __name__ == "__main__":
    unittest.main()
