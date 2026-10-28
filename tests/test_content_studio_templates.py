"""Recipes transfer into existing tools without buying or publishing output."""
import copy
import unittest

from streamlit.testing.v1 import AppTest

from content_studio_templates import apply_template_handoff, list_templates, template_handoff


PROJECT = {"id": "project-1", "owner_id": 77, "name": "Мой проект", "audience": "Руководители",
           "product": "Подготовка материалов", "cta": "Посмотрите примеры", "visual_style": "Естественный свет"}

APP = '''
from unittest.mock import patch
import streamlit as st
import content_studio_templates as ui
project = PROJECT
with patch('agency_access_control.is_agency_access_revoked',return_value=False), patch('requests.post',side_effect=AssertionError('Catalog must never buy anything')):
    ui.render_templates(77, project)
'''


class TemplateDataTests(unittest.TestCase):
    def test_catalog_has_distinct_supported_destinations_and_independent_copies(self):
        templates = list_templates()
        self.assertGreaterEqual(len(templates), 10)
        self.assertEqual(len({item['id'] for item in templates}), len(templates))
        self.assertEqual({item['tool'] for item in templates}, {'texts', 'images', 'carousel', 'video', 'speech'})
        videos = [template_handoff(item['id'], PROJECT) for item in templates if item['tool'] == 'video']
        self.assertEqual({item['video_style'] for item in videos}, {'live', 'cartoon'})
        templates[0]['title'] = 'Changed by caller'
        self.assertNotEqual(list_templates()[0]['title'], 'Changed by caller')

    def test_search_and_category_use_actual_template_text(self):
        self.assertEqual([item['id'] for item in list_templates('video', 'мультфильм')], ['video-cartoon-action'])
        self.assertEqual(list_templates('speech', 'мультфильм'), [])
        for category, query in [('unknown', ''), ('all', None), ('all', 'x' * 501)]:
            with self.subTest(category=category, query=query), self.assertRaises(ValueError):
                list_templates(category, query)

    def test_project_context_is_inserted_without_executing_or_expanding_project_braces(self):
        project = {**PROJECT, 'name': '{name} $(do-not-execute)', 'product': 'x' * 6000,
                   'audience': 'y' * 6000, 'cta': 'z' * 6000, 'visual_style': 'v' * 6000}
        for item in list_templates():
            handoff = template_handoff(item['id'], project)
            self.assertIn('{name} $(do-not-execute)', handoff['prompt'])
            self.assertLessEqual(len(handoff['prompt']), 6000)
        self.assertEqual(template_handoff('speech-invitation')['project_id'], None)
        with self.assertRaises(ValueError):
            template_handoff('not-in-catalog')

    def test_each_handoff_prefills_its_real_tool_and_does_not_submit(self):
        for item in list_templates():
            with self.subTest(template=item['id']):
                pending = {**template_handoff(item['id'], PROJECT), 'owner_id': 77}
                state = {'content_studio_77_template_handoff': copy.deepcopy(pending), 'unrelated': 'preserved'}
                self.assertTrue(apply_template_handoff(77, PROJECT, state))
                self.assertNotIn('content_studio_77_template_handoff', state)
                self.assertEqual(state['unrelated'], 'preserved')
                self.assertFalse(any('submit' in key or 'approved' in key or 'queued' in key for key in state))
                if item['tool'] in {'texts', 'images', 'speech'}:
                    kind = {'texts': 'text', 'images': 'image', 'speech': 'audio'}[item['tool']]
                    self.assertEqual(state[f'studio_tool_77_project-1_{kind}_prompt'], pending['prompt'])
                else:
                    self.assertEqual(state['cf_auto_77_project-1_brief_topic'], pending['prompt'])
                    self.assertEqual(state['cf_auto_77_project-1_brief_format'], item['content_format'])
                if item.get('video_style'):
                    self.assertEqual(state['content_studio_77_video_style'], item['video_style'])

    def test_owner_or_project_mismatch_does_not_overwrite_draft(self):
        pending = {**template_handoff('post-checklist', PROJECT), 'owner_id': 77}
        original = {'content_studio_77_template_handoff': pending,
                    'studio_tool_77_project-1_text_prompt': 'Свой незавершённый текст'}
        for project, owner in [({**PROJECT, 'owner_id': 88}, 77),
                               ({**PROJECT, 'id': 'project-2'}, 77), (PROJECT, 88)]:
            with self.subTest(project=project, owner=owner):
                state = copy.deepcopy(original)
                self.assertFalse(apply_template_handoff(owner, project, state))
                self.assertEqual(state, original)
        for invalid in (0, True, None, 'not-an-owner'):
            with self.assertRaises(ValueError):
                apply_template_handoff(invalid, PROJECT, {})


class TemplateUiTests(unittest.TestCase):
    def app(self, project=PROJECT):
        app = AppTest.from_string(APP.replace('PROJECT', repr(project)), default_timeout=20).run()
        self.assertFalse(app.exception, [item.message for item in app.exception])
        return app

    def test_gallery_renders_search_category_copy_and_use_without_generation(self):
        app = self.app()
        self.assertEqual(len(app.button), len(list_templates()))
        self.assertEqual(len(app.code), len(list_templates()))
        self.assertTrue(all(item.label == 'Использовать' for item in app.button))
        app.selectbox(key='studio_templates_77_category').select('video').run()
        self.assertFalse(app.exception)
        self.assertEqual(len(app.button), 2)
        app.text_input(key='studio_templates_77_query').input('мультфильм').run()
        self.assertFalse(app.exception)
        self.assertEqual(len(app.button), 1)
        self.assertEqual(app.button[0].key, 'studio_templates_77_video-cartoon-action_use')

    def test_use_handoff_is_owner_project_scoped_and_waits_for_real_submit(self):
        app = self.app()
        app.button(key='studio_templates_77_video-cartoon-action_use').click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state['content_studio_77_next'], 'video')
        result = app.session_state['content_studio_77_template_handoff']
        self.assertEqual((result['owner_id'], result['project_id'], result['video_style']), (77, 'project-1', 'cartoon'))
        self.assertIn('Мой проект', result['prompt'])
        self.assertNotIn('owner_approved', result)
        self.assertNotIn('generation_started', result)

    def test_browsing_without_project_is_possible_but_foreign_project_is_blocked(self):
        app = self.app(None)
        self.assertEqual(len(app.button), len(list_templates()))
        foreign = self.app({**PROJECT, 'owner_id': 88})
        self.assertFalse(foreign.button)
        self.assertTrue(foreign.error)


if __name__ == '__main__':
    unittest.main()
