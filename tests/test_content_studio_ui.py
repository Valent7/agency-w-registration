"""The Content entry keeps tools, archives and publishing directly reachable."""
import unittest
from streamlit.testing.v1 import AppTest
from test_content_factory_autopilot_ui import APP


SCRIPT = APP.replace(
    "    ui.render_content_autopilot(77, 'Партнёр')",
    """    import content_factory_ui as existing
    def never_generate(*a, **kw):
        raise AssertionError('Navigation must never generate or publish')
    with patch.object(existing, '_load_latest_package', return_value=None), patch('content_studio_catalog.list_manual_packages', return_value=[]):
        existing.render_content_factory(77, 'Партнёр', never_generate, never_generate, never_generate, never_generate)
""",
)


class StudioNavigationTests(unittest.TestCase):
    def app(self, script=SCRIPT):
        app = AppTest.from_string(script, default_timeout=20).run()
        self.assertFalse(app.exception, [item.message for item in app.exception])
        return app

    def choose(self, app, section):
        app.radio(key='content_studio_77_tool').set_value(section).run()
        self.assertFalse(app.exception, [item.message for item in app.exception])

    def test_content_entry_opens_one_form_and_keeps_tools_visible(self):
        app = self.app()
        self.assertEqual(app.radio(key='content_studio_77_tool').value, 'today')
        self.assertNotIn('cf2_start_77', [item.key for item in app.button])
        self.assertFalse(app.tabs)
        self.assertTrue(any(item.label == '✨ Создать пакет дня' for item in app.button))
        self.assertFalse(any(item.label == 'Сохранить проект' for item in app.button))
        self.assertIn('Сегодняшний контент', app.radio(key='content_studio_77_tool').options)
        self.assertIn('Мои генерации', app.radio(key='content_studio_77_tool').options)

    def test_all_sections_open_without_generating_or_hiding_neighbor_controls(self):
        app = self.app()
        for section in ('today', 'texts', 'images', 'carousel', 'video', 'reference', 'speech', 'music', 'library', 'editor',
                        'prompts', 'search', 'calendar', 'connections', 'project', 'help', 'ready'):
            with self.subTest(section=section):
                self.choose(app, section)
        self.assertFalse(app.session_state['_fake_store'].actions)

    def test_music_section_is_true_three_step_flow_without_external_editor(self):
        app = self.app()
        self.choose(app, 'music')
        values = [item.value for item in app.markdown]
        self.assertTrue(any('Музыка — 3 шага' in value for value in values))
        self.assertTrue(any('1. Опишите музыку' in value for value in values))
        self.assertTrue(any('2. Создайте' in value for value in values))
        self.assertTrue(any('3. Прослушайте и выберите' in value for value in values))
        self.assertTrue(any(item.key == 'studio_music_77_project-1_prompt' for item in app.text_area))
        self.assertFalse(app.get('link_button'))
        self.assertNotIn('studio_music_77_project-1_mode', [item.key for item in app.radio])
        self.assertFalse(app.session_state['_fake_store'].actions)

    def test_music_create_button_activates_after_plain_language_description(self):
        app = self.app()
        self.choose(app, 'music')
        key = 'studio_music_77_project-1_prompt'
        create = app.button(key='studio_music_77_project-1_create')
        self.assertTrue(create.disabled)
        app.text_area(key=key).input('Лёгкая спокойная музыка для ролика о времени').run()
        self.assertFalse(app.button(key='studio_music_77_project-1_create').disabled)
        self.assertFalse(app.session_state['_fake_store'].actions)

    def test_prompt_recipe_opens_and_prefills_its_real_tool_without_generating(self):
        app = self.app()
        self.choose(app, 'prompts')
        next(item for item in app.button if item.key == 'studio_templates_77_post-small-story_use').click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.radio(key='content_studio_77_tool').value, 'texts')
        self.assertIn('Агентство W', app.text_area(key='studio_tool_77_project-1_text_prompt').value)
        self.assertFalse(app.session_state['_fake_store'].actions)

    def test_favorites_filter_keeps_only_saved_favorites(self):
        app = self.app()
        app.session_state['_fake_store'].jobs[0]['favorite'] = True
        self.choose(app, 'library')
        app.checkbox(key='studio_library_77_favorites').check().run()
        self.assertFalse(app.exception)
        opens = [item.key for item in app.button if str(item.key).endswith('_open')]
        self.assertEqual(opens, ['studio_library_77_ready-1_open'])

    def test_gallery_shows_existing_ready_material_and_preserves_approval_editor(self):
        app = self.app()
        self.choose(app, 'library')
        self.assertTrue(any(item.label == 'Поиск по материалам' for item in app.text_input))
        self.assertTrue(any('Полезный материал' in item.value for item in app.markdown))
        app.button(key='studio_library_77_ready-1_open').click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.text_area(key='cf_auto_77_project-1_ready-1_text').value, 'Исходный текст')
        self.assertTrue(any('Утвердить и поставить' in item.label for item in app.button))
        self.assertTrue(any(item.label == 'Переработать материал' for item in app.button))
        self.assertTrue(any(item.label == 'Отменить материал' for item in app.button))
        self.assertTrue(any(item.proto.label == 'Скачать текст' for item in app.get('download_button')))

    def test_gallery_search_and_cancelled_filter(self):
        app = self.app()
        app.session_state['_fake_store'].jobs.append({
            'id': 'cancelled-1', 'project_id': 'project-1', 'owner_id': 77,
            'platform': 'vk', 'format': 'reel', 'state': 'cancelled',
            'scheduled_at': '2026-10-28T09:00:00+00:00',
            'content': {'title': 'Отклонённый ролик', 'text': 'Старый'}, 'assets': [],
        })
        self.choose(app, 'library')
        self.assertNotIn('studio_library_77_cancelled-1_open', [item.key for item in app.button])
        app.checkbox(key='studio_library_77_cancelled').check().run()
        self.assertIn('studio_library_77_cancelled-1_open', [item.key for item in app.button])
        app.text_input(key='studio_library_77_search').input('Полезный материал').run()
        opens = [item.key for item in app.button if str(item.key).endswith('_open')]
        self.assertEqual(opens, ['studio_library_77_ready-1_open'])

    def test_unsubmitted_music_description_survives_switching_tools(self):
        app = self.app()
        self.choose(app, 'music')
        key = 'studio_music_77_project-1_prompt'
        app.text_area(key=key).input('Мягкая музыка о ценности времени')
        self.choose(app, 'images')
        self.choose(app, 'music')
        self.assertEqual(app.text_area(key=key).value, 'Мягкая музыка о ценности времени')
        self.assertFalse(app.session_state['_fake_store'].actions)

    def test_unsubmitted_text_survives_switching_tools(self):
        app = self.app()
        self.choose(app, 'texts')
        key = 'studio_tool_77_project-1_text_prompt'
        app.text_area(key=key).input('Несохранённая идея владельца')
        self.choose(app, 'images')
        self.choose(app, 'library')
        self.choose(app, 'texts')
        self.assertEqual(app.text_area(key=key).value, 'Несохранённая идея владельца')
        self.assertFalse(app.session_state['_fake_store'].actions)

    def test_compact_mobile_selector_opens_the_same_tool(self):
        app = self.app()
        app.selectbox(key='content_studio_77_mobile_tool').select('texts').run()
        self.assertFalse(app.exception)
        self.assertEqual(app.radio(key='content_studio_77_tool').value, 'texts')
        self.assertTrue(any(item.label == 'Подготовить текст' for item in app.button))

    def test_archive_opens_previous_manual_result_without_recreating(self):
        package = {'package_id': 'manual-1', 'owner_telegram_id': 77,
                   'workflow_version': 'single_v2', 'items': [{'title': 'Прежний материал', 'id': 'old-1'},
                                                              {'title': 'Второй материал', 'id': 'old-2'}]}
        script = SCRIPT.replace("patch('content_studio_catalog.list_manual_packages', return_value=[])",
                                "patch('content_studio_catalog.list_manual_packages', return_value=" + repr([package]) + ")")
        # Rendering the archived result must hand the original package to the editor.
        script = script.replace("with patch.object(existing, '_load_latest_package',", "with patch.object(existing, '_render_item'), patch.object(existing, '_load_latest_package',")
        app = self.app(script)
        self.choose(app, 'library')
        app.button(key='studio_manual_77_manual-1').click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.radio(key='content_studio_77_tool').value, 'editor')
        self.assertEqual(app.session_state['content_factory_package_77']['package_id'], 'manual-1')
        self.assertEqual(app.session_state['content_factory_stage_77'], 'result')
        app.selectbox(key='cf2_archive_77_manual-1').select(1).run()
        self.assertFalse(app.exception)
        self.assertEqual(app.selectbox(key='cf2_archive_77_manual-1').value, 1)
        self.assertFalse(app.session_state['_fake_store'].actions)

    def test_old_editor_archive_is_available_before_creating_first_factory_project(self):
        package = {'package_id': 'legacy-only', 'owner_telegram_id': 77,
                   'items': [{'title': 'Сохранённый ранее материал'}]}
        script = SCRIPT.replace("return [copy.deepcopy(self.project)]", "return []", 1)
        script = script.replace("patch('content_studio_catalog.list_manual_packages', return_value=[])",
                                "patch('content_studio_catalog.list_manual_packages', return_value=" + repr([package]) + ")")
        app = self.app(script)
        self.choose(app, 'library')
        self.assertIn('studio_manual_77_legacy-only', [item.key for item in app.button])
        self.assertFalse(app.session_state['_fake_store'].actions)


if __name__ == '__main__':
    unittest.main()
