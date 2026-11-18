"""Offline film lifecycle regression: local files, SQLite and mocked providers only."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import uuid4

from content_factory_core import iso, utc_now
from content_factory_store import SQLiteStore, SupabaseStore, StoreError
from kadrian_films import FilmStore, create_film, change_film, films, production_status
from kadrian_memory import save_turn, restore_chat, project_context


class FilmTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'films.sqlite'
        self.raw = SQLiteStore(self.path)
        self.project = self.raw.save_project(77, {'name': 'Test', 'kadrian_notes': 'Проверенное общее правило',
            'kadrian_chat': {'turns': [{'id': 'legacy', 'prompt': 'Кот', 'answer': 'Создаю сцену.'}],
                             'brief': 'Прежний сценарий', 'request': 'legacy-request'}})
        self.pid = self.project['id']
        self.legacy = films(self.project)['legacy_id']
        self.one = FilmStore(self.raw, 77, self.pid, self.legacy)

    def new(self):
        item = create_film(self.raw, 77, self.pid)
        return FilmStore(self.raw, 77, self.pid, item['id'])

    def save(self, scope, text):
        save_turn(scope, 77, self.pid, str(uuid4()), text, 'Ответ ' + text, 'Сценарий ' + text, str(uuid4()))

    def job(self, scope=None, **extra):
        data = {'dedupe_key': 'paid-' + uuid4().hex, 'state': 'generating', 'platform': 'studio',
                'format': 'reel', 'scheduled_at': iso(utc_now()), 'studio_master': True,
                'production': {'task_id': 'already-paid'}, 'assets': [], 'progress': {'message': 'Создаю сцену.'}, **extra}
        return (scope or self.raw).enqueue_job(77, self.pid, data)

    def test_three_films_restore_after_store_restart_including_original_and_assets(self):
        legacy_before = deepcopy(self.project['kadrian_chat'])
        old_job = self.job()
        second, third = self.new(), self.new()
        for scope, text in ((second, 'Лес'), (third, 'Море')):
            self.save(scope, text)
            scope.save_attachments([(text + '.png', 'image/png', text.encode())])
            self.job(scope)
        restarted = SQLiteStore(self.path)
        for scope, text in ((second, 'Лес'), (third, 'Море')):
            fresh = FilmStore(restarted, 77, self.pid, scope.film_id)
            self.assertEqual(restore_chat(fresh.get_project(77, self.pid))['brief'], 'Сценарий ' + text)
            self.assertEqual(fresh.restore_attachments()[0][2], text.encode())
            self.assertEqual(len(fresh.list_jobs(77, self.pid)), 1)
        original = FilmStore(restarted, 77, self.pid, self.legacy)
        self.assertEqual(original.get_project(77, self.pid)['kadrian_chat'], legacy_before)
        self.assertEqual(original.list_jobs(77, self.pid)[0]['id'], old_job['id'])
        self.assertEqual(restarted.get_project(77, self.pid)['kadrian_notes'], 'Проверенное общее правило')

    def test_new_film_is_empty_and_keeps_general_rules(self):
        second = self.new()
        project = second.get_project(77, self.pid)
        self.assertEqual(restore_chat(project), {'messages': [], 'brief': '', 'request': None})
        self.assertEqual(second.restore_attachments(), [])
        self.assertEqual(second.list_jobs(77, self.pid), [])
        self.assertIn('Проверенное общее правило', project_context(project))

    def test_navigation_does_not_modify_paid_job_or_enqueue(self):
        paid = self.job()
        with patch.object(self.raw, 'enqueue_job', wraps=self.raw.enqueue_job) as enqueue, \
             patch.object(self.raw, 'update_job', wraps=self.raw.update_job) as update:
            for _ in range(3):
                second = self.new()
                second.get_project(77, self.pid)
                self.one.list_jobs(77, self.pid)
            enqueue.assert_not_called()
            update.assert_not_called()
        self.assertEqual(self.raw.get_job(77, paid['id']), paid)

    def test_uploads_and_generated_assets_cannot_cross_films(self):
        second, third = self.new(), self.new()
        asset = second.put_asset(77, self.pid, None, 'cat.png', b'cat', 'image/png')
        job = self.job(second)
        generated = self.raw.put_asset(77, self.pid, job['id'], 'out.mp4', b'video', 'video/mp4')
        for value in (asset, generated):
            with self.assertRaises(StoreError):
                third.read_asset(77, value)
            with self.assertRaises(StoreError):
                self.job(third, provider_spec={'reference_asset': value})
        self.assertEqual(second.read_asset(77, generated), b'video')

    def test_foreign_owner_project_and_unknown_film_fail_closed(self):
        second = self.new()
        other = self.raw.save_project(77, {'name': 'Other'})
        for owner, pid, fid in ((88, self.pid, second.film_id), (77, other['id'], second.film_id),
                                (77, self.pid, str(uuid4()))):
            with self.assertRaises(StoreError):
                FilmStore(self.raw, owner, pid, fid)
        with self.assertRaises(StoreError):
            second.list_jobs(77, other['id'])

    def test_create_retry_is_idempotent(self):
        fid = str(uuid4())
        first = create_film(self.raw, 77, self.pid, fid)
        self.assertEqual(create_film(self.raw, 77, self.pid, fid), first)
        self.assertEqual(len(films(self.raw.get_project(77, self.pid))['items']), 2)

    def test_concurrent_film_and_memory_change_are_merged_on_conflict(self):
        original = self.raw.update_project_if_current
        first = True
        def racing(owner, pid, changes, expected):
            nonlocal first
            if first:
                first = False
                project = self.raw.get_project(owner, pid)
                original(owner, pid, {'kadrian_notes': 'Новое правило'}, project['updated_at'])
                create_film(self.raw, owner, pid)
                raise StoreError('conflict')
            return original(owner, pid, changes, expected)
        with patch.object(self.raw, 'update_project_if_current', side_effect=racing):
            self.new()
        saved = self.raw.get_project(77, self.pid)
        self.assertEqual(len(films(saved)['items']), 3)
        self.assertEqual(saved['kadrian_notes'], 'Новое правило')

    def test_two_tabs_append_same_film_without_erasing_turns(self):
        second = self.new()
        other_tab = FilmStore(self.raw, 77, self.pid, second.film_id)
        self.save(second, 'Первый')
        self.save(other_tab, 'Второй')
        self.assertEqual(len(second.get_project(77, self.pid)['kadrian_chat']['turns']), 2)
        self.assertEqual(self.one.get_project(77, self.pid)['kadrian_chat']['brief'], 'Прежний сценарий')

    def test_legacy_turns_remain_available_to_rollback_code(self):
        self.new()
        self.save(self.one, 'Поправка')
        self.assertEqual(len(self.raw.get_project(77, self.pid)['kadrian_chat']['turns']), 2)

    def test_terminal_status_overrides_stale_progress(self):
        for state, text in [('ready', 'Видео готово.'), ('failed', 'Производство завершилось ошибкой.'),
                            ('paused', 'Производство приостановлено.'), ('queued', 'Задание в очереди.')]:
            self.assertEqual(production_status({'state': state, 'progress': {'message': 'Создаю сцену.'}}), text)
        self.assertEqual(production_status({'state': 'queued', 'progress': {'stage': 'credits', 'message': 'Недостаточно кредитов'}}), 'Недостаточно кредитов')

    def test_database_filter_precedes_limit_and_keeps_old_films_visible(self):
        second, third = self.new(), self.new()
        older = self.job(second)
        for _ in range(35):
            self.job(third)
        self.assertEqual(second.list_jobs(77, self.pid, limit=1)[0]['id'], older['id'])

    def test_supabase_filter_scopes_owner_project_film_before_limit(self):
        # Pure request contract, never connects to a database.
        store = SupabaseStore('https://example.supabase.co', 'not-a-real-key')
        with patch.object(store, '_rows', return_value=[]) as rows:
            store.list_film_jobs(77, self.pid, self.legacy, limit=30)
            args = rows.call_args.args[1]
            self.assertEqual(args['data->>film_id'], 'eq.' + self.legacy)
            self.assertEqual(args['owner_id'], 'eq.77')
            self.assertEqual(args['project_id'], 'eq.' + self.pid)
            store.list_film_jobs(77, self.pid, self.legacy, legacy=True)
            self.assertIn('data->>film_id.is.null', rows.call_args.args[1]['or'])

    def test_streamlit_three_films_switch_restart_and_real_job_status(self):
        from streamlit.testing.v1 import AppTest
        paid = self.job(state='ready')
        script = 'import streamlit as st\nfrom kadrian_studio_ui import render_kadrian\nrender_kadrian(77, st.session_state["ask"])'
        ask = Mock(return_value='Обсуждаем новый сценарий')
        def selector(app):
            return next(x for x in app.selectbox if x.label == 'Сохранённые фильмы')
        def button(app, label):
            return next(x for x in app.button if x.label == label)
        with patch('kadrian_studio_ui._runtime', return_value=(self.raw, None, None)), \
             patch('kadrian_studio_ui.connection_status', return_value={}), \
             patch('content_factory_runtime.resolve_live_video_client') as provider:
            app = AppTest.from_string(script)
            app.session_state['ask'] = ask
            app.run()
            self.assertFalse(app.exception)
            self.assertTrue(any(x.value == 'Видео готово.' for x in app.info))
            self.assertFalse(any(x.value == 'Создаю сцену.' for x in app.markdown))
            created = []
            for text in ('Лес', 'Море'):
                button(app, '🎬 Новый фильм / мультфильм').click().run()
                fid = selector(app).value
                created.append(fid)
                self.assertEqual(len(app.chat_message), 0)
                self.save(FilmStore(self.raw, 77, self.pid, fid), text)
                app.run()
                self.assertTrue(any(x.value == text for x in app.markdown))
                self.assertFalse(app.exception)
            selector(app).set_value(created[0]).run()
            self.assertTrue(any(x.value == 'Лес' for x in app.markdown))
            self.assertFalse(any(x.value == 'Море' for x in app.markdown))
            restarted = AppTest.from_string(script)
            restarted.session_state['ask'] = ask
            restarted.run()
            self.assertEqual(len(selector(restarted).options), 3)
            selector(restarted).set_value(created[1]).run()
            self.assertTrue(any(x.value == 'Море' for x in restarted.markdown))
            self.assertFalse(restarted.exception)
            provider.assert_not_called()
            ask.assert_not_called()
        self.assertEqual(self.raw.get_job(77, paid['id']), paid)

    def test_attachment_replacements_and_script_revisions_preserve_history(self):
        second = self.new()
        second.save_attachments([('old.png', 'image/png', b'old')])
        second.save_attachments([('new.png', 'image/png', b'new')])
        second.save_attachments([])
        self.save(second, 'Старый сценарий')
        self.save(second, 'Новый сценарий')
        item = films(self.raw.get_project(77, self.pid))['items'][second.film_id]
        self.assertEqual(second.read_asset(77, item['attachment_history'][1][0]), b'old')
        self.assertEqual(second.read_asset(77, item['attachment_history'][2][0]), b'new')
        self.assertEqual(item['chat']['turns'][0]['brief'], 'Сценарий Старый сценарий')

    def test_multiple_providers_are_scoped_and_repeated_requests_remain_idempotent(self):
        from kadrian_production_ui import enqueue_provider
        spec = {'kind': 'scene', 'prompt': 'Friendly cat', 'duration': 5, 'aspect_ratio': '9:16'}
        second, third = self.new(), self.new()
        with patch('agency_access_control.is_agency_access_revoked', return_value=False), \
             patch('content_factory_billing.billing_enforced', return_value=False):
            first = enqueue_provider(second, 77, self.pid, 'heygen', spec, 'cartoon', 'same-request', 1)
            again = enqueue_provider(second, 77, self.pid, 'heygen', spec, 'cartoon', 'same-request', 1)
            other = enqueue_provider(third, 77, self.pid, 'heygen', spec, 'cartoon', 'same-request', 1)
        self.assertEqual(first['id'], again['id'])
        self.assertNotEqual(first['id'], other['id'])
        self.assertEqual([x['id'] for x in second.list_jobs(77, self.pid)], [first['id']])

    def test_runway_active_job_in_other_film_does_not_replace_new_request(self):
        from kadrian_actions import enqueue_runway
        second, third = self.new(), self.new()
        spec = {'scope': 'scene', 'duration': 5, 'brief': 'A friendly cat sits down', 'aspect': '9:16'}
        client = Mock(model='gen4.5')
        client.estimate_credits.return_value = 60
        with patch('kadrian_actions.runway_balance', return_value=(client, 200)), \
             patch('agency_access_control.is_agency_access_revoked', return_value=False):
            one, _ = enqueue_runway(second, 77, second.get_project(77, self.pid), spec, 'cartoon', 'same')
            two, created = enqueue_runway(third, 77, third.get_project(77, self.pid), spec, 'cartoon', 'same')
            same, repeated = enqueue_runway(third, 77, third.get_project(77, self.pid), spec, 'cartoon', 'other')
        self.assertTrue(created)
        self.assertFalse(repeated)
        self.assertEqual(same['id'], two['id'])
        self.assertNotEqual(one['id'], two['id'])
        client.create_scene.assert_not_called()

    def test_music_upload_and_assembly_do_not_offer_other_film_assets(self):
        from kadrian_music import save_uploaded_music
        from kadrian_production_ui import _assembly
        import streamlit as st
        from streamlit.testing.v1 import AppTest
        second, third = self.new(), self.new()
        with patch('kadrian_music.inspect_music', return_value=('audio/mpeg', 10)), \
             patch('agency_access_control.is_agency_access_revoked', return_value=False):
            one = save_uploaded_music(second, 77, self.pid, b'music', 'theme.mp3')
            two = save_uploaded_music(third, 77, self.pid, b'music', 'theme.mp3')
        self.assertNotEqual(one['id'], two['id'])
        self.assertEqual(len(third.list_jobs(77, self.pid)), 1)
        video = self.job(second, state='ready')
        asset = second.put_asset(77, self.pid, video['id'], 'scene.mp4', b'video', 'video/mp4')
        second.update_job(77, video['id'], {'assets': [asset]})
        app = AppTest.from_string('import streamlit as st\nfrom kadrian_production_ui import _assembly\n_assembly(77, st.session_state["project"], st.session_state["store"], {"SHOTSTACK_API_KEY":"test"}, "cartoon", "test")')
        app.session_state['project'] = third.get_project(77, self.pid)
        app.session_state['store'] = third
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(len(app.multiselect), 0)
        self.assertTrue(any('Готовые видео выбранного проекта' in x.value for x in app.info))

    def test_create_ack_loss_preserves_one_film_and_does_not_retry_a_job(self):
        original = self.raw.update_project_if_current
        first = True
        def ack_loss(*args):
            nonlocal first
            result = original(*args)
            if first:
                first = False
                raise StoreError('ack lost')
            return result
        with patch.object(self.raw, 'update_project_if_current', side_effect=ack_loss):
            self.new()
        self.assertEqual(len(films(self.raw.get_project(77, self.pid))['items']), 2)
        self.assertEqual(self.raw.list_jobs(77, self.pid), [])

    def test_forged_film_marker_does_not_make_foreign_asset_available(self):
        second, third = self.new(), self.new()
        asset = second.put_asset(77, self.pid, None, 'cat.png', b'cat', 'image/png')
        with self.assertRaises(StoreError):
            third.read_asset(77, {**asset, 'film_id': third.film_id})

    def test_attachment_original_name_and_settings_survive_restart(self):
        second = self.new()
        second.save_attachments([('Кот.png', 'image/png', b'cat')])
        change_film(self.raw, 77, self.pid, second.film_id,
                    lambda item: item.update(settings={'_style': 'Мультфильм', '_route': 'assembly'}))
        fresh = FilmStore(SQLiteStore(self.path), 77, self.pid, second.film_id)
        self.assertEqual(fresh.restore_attachments()[0][0], 'Кот.png')
        self.assertEqual(films(fresh.get_project(77, self.pid))['items'][second.film_id]['settings']['_route'], 'assembly')

    def test_paid_provider_task_finishes_in_its_original_film_after_new_film_created(self):
        from datetime import datetime, timezone
        from kadrian_actions import process_auto_job
        original = self.new()
        job = self.job(original, kadrian_auto=True, kadrian_provider='runway_auto',
                       provider_spec={'scope': 'scene', 'duration': 5, 'brief': 'Cat'},
                       production={'task_id': 'already-paid', 'image_asset': {'unused': True}})
        self.new()
        client = Mock(model='gen4.5')
        client.task_status.return_value = {'status': 'succeeded', 'url': 'https://example.test/result.mp4'}
        client.download.return_value = b'video'
        worker = Mock(store=self.raw)
        worker.clock.return_value = datetime.now(timezone.utc)
        worker._finish.side_effect = lambda old, **changes: self.raw.update_job(77, old['id'], changes)
        with patch('content_factory_runtime.resolve_live_video_client', return_value=client), \
             patch('content_factory_billing.billing_enforced', return_value=False):
            self.assertEqual(process_auto_job(worker, job, self.project), 'generated')
        client.create_scene.assert_not_called()
        worker.generator._illustration.assert_not_called()
        ready = original.get_job(77, job['id'])
        self.assertEqual(ready['state'], 'ready')
        self.assertEqual(ready['film_id'], original.film_id)
        self.assertEqual(original.read_asset(77, ready['assets'][0]), b'video')
