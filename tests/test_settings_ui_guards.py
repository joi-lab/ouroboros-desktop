import os
import pathlib
import sys
import unittest


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

REPO = pathlib.Path(__file__).resolve().parents[1]


class TestSettingsUiGuards(unittest.TestCase):
    def test_save_checks_http_status(self):
        source = (REPO / "web/modules/settings.js").read_text(encoding="utf-8")
        self.assertIn("if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);", source)

    def test_save_does_not_overwrite_masked_secrets(self):
        source = (REPO / "web/modules/settings.js").read_text(encoding="utf-8")
        self.assertIn("if (orKey && !orKey.includes('...')) body.OPENROUTER_API_KEY = orKey;", source)
        self.assertIn("if (oaiKey && !oaiKey.includes('...')) body.OPENAI_API_KEY = oaiKey;", source)
        self.assertIn("if (antKey && !antKey.includes('...')) body.ANTHROPIC_API_KEY = antKey;", source)
        self.assertIn("if (ghToken && !ghToken.includes('...')) body.GITHUB_TOKEN = ghToken;", source)

    def test_masked_secret_inputs_clear_on_focus(self):
        source = (REPO / "web/modules/settings.js").read_text(encoding="utf-8")
        self.assertIn("if (input.value.includes('...')) input.value = '';", source)

    def test_models_section_explains_local_switching(self):
        source = (REPO / "web/modules/settings.js").read_text(encoding="utf-8")
        self.assertIn("These fields are cloud model IDs.", source)
        self.assertIn("through the GGUF server configured above.", source)

    def test_save_reloads_settings_after_success(self):
        source = (REPO / "web/modules/settings.js").read_text(encoding="utf-8")
        self.assertIn("await loadSettings();", source)
