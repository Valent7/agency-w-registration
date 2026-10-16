"""Protect older content formats while enabling explicit two-person dialogue."""
import ast
from pathlib import Path
import unittest

CODE = Path("content_factory_ui.py").read_text(encoding="utf-8")
UI = Path("agency_dialogue_ui.py").read_text(encoding="utf-8")


class DialogueFactoryRouteTests(unittest.TestCase):
    def test_script_modules_compile(self):
        for path in (
            "content_factory_ui.py", "agency_dialogue_ui.py",
            "agency_dialogue_video.py",
        ):
            ast.parse(Path(path).read_text(encoding="utf-8"), filename=path)

    def test_new_format_is_available_in_factory(self):
        self.assertIn('options=["post", "carousel", "reel", "dialogue"]', CODE)
        self.assertIn('"dialogue": "🎭 ИИ-диалог"', CODE)
        self.assertIn('if content_format == "dialogue":', CODE)
        self.assertIn('normalise_dialogue_turns(item.get("dialogue"))', CODE)
        self.assertIn('render_dialogue_item(', CODE)
        self.assertIn('review_fn=_stagirite_review', CODE)

    def test_previous_paid_reels_path_unchanged(self):
        self.assertIn('🎬 Создать живой Reels', CODE)
        self.assertIn('_start_runway_scenes(', CODE)
        self.assertIn('_assemble_animated_reel(', CODE)
        self.assertIn('create_avatar_video_fn(item.get("script") or "")', CODE)

    def test_partner_ui_does_not_require_ids(self):
        self.assertIn("suggest_dialogue_cast(catalog, preferred=previous)", UI)
        self.assertIn("🎭 Изменить героев или голоса (необязательно)", UI)
        self.assertNotIn('st.text_input(\n                "Аватар HeyGen (ID)"', UI)
        self.assertIn("st.selectbox(", UI)
        self.assertIn("list_heygen_catalog(api_key)", UI)

    def test_approvals_separate_from_payment_and_publication(self):
        self.assertIn('if not approved:', UI)
        self.assertIn('review_fn(package, item, ask_ai_fn)', UI)
        self.assertIn('if not state.get("video_id"):', UI)
        self.assertIn('🎬 Создать ИИ-диалог в HeyGen', UI)
        self.assertIn('🔄 Проверить готовность диалога', UI)
        self.assertNotIn("publish_to_", UI)


if __name__ == "__main__":
    unittest.main()
