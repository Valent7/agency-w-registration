"""Static checks of the actual Streamlit-generated Neola audio player JavaScript."""
import ast
import re
import shutil
import subprocess
import unittest
from pathlib import Path


def _player_template():
    source = Path("neola_partner_center.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_render_neola_voice_clip"
    )
    template_assignment = next(
        node for node in function.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "template" for t in node.targets)
    )
    return template_assignment.value.value, source


class NeolaVoicePlayerTests(unittest.TestCase):
    def test_browser_retry_and_manual_replay(self):
        template, source = _player_template()
        self.assertIn("if (!playedToday()) tryPlay(false)", template)
        self.assertIn("button.addEventListener(\"click\", onClick)", template)
        self.assertIn("document.addEventListener(\"pointerdown\", onFirstGesture, true)", template)
        self.assertIn("window.localStorage.setItem(storeKey, stamp)", template)
        self.assertIn("unsafe_allow_javascript=True", source)
        self.assertIn("__agencyWNeolaActiveAudio", template)

    @unittest.skipUnless(shutil.which("node"), "Node.js unavailable")
    def test_inline_javascript_parses(self):
        template, _ = _player_template()
        js = re.search(r"<script>(.*?)</script>", template, re.S)
        self.assertIsNotNone(js)
        script = (
            js.group(1)
            .replace("__ROOT__", "neola-test-player")
            .replace("__STORAGE_KEY__", '"storage-key"')
            .replace("__STAMP__", '"2026-10-13"')
            .replace("__AUDIO__", "ZmFrZQ==")
        )
        result = subprocess.run(
            ["node", "--check", "-"],
            input=script, text=True, capture_output=True, timeout=10, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
