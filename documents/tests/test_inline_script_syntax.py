import re
import shutil
import subprocess
import tempfile
import unittest

from django.template.loader import get_template
from django.test import TestCase


class InlineScriptSyntaxTests(TestCase):
    TEMPLATES = ["documents/upload_base.html", "billing/partials/pricing_cards.html"]

    def _parse_errors(self, js):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
            handle.write(js)
        result = subprocess.run(["node", "--check", handle.name], capture_output=True, text=True)
        return result.returncode, result.stderr

    def test_inline_scripts_parse(self):
        if shutil.which("node") is None:
            self.skipTest("node not installed")

        checked = 0
        for name in self.TEMPLATES:
            html = get_template(name).render()
            blocks = [
                b for b in re.findall(r"<script[^>]*>(.*?)</script>", html, re.S) if b.strip()
            ]
            for index, block in enumerate(blocks):
                js = re.sub(r"\{\{.*?\}\}|\{%.*?%\}", "", block, flags=re.S)
                code, stderr = self._parse_errors(js)
                self.assertEqual(code, 0, f"{name} script {index} does not parse:\n{stderr}")
                checked += 1

        self.assertGreaterEqual(checked, 5)

    def test_parser_rejects_the_bug_that_broke_dropzone(self):
        if shutil.which("node") is None:
            self.skipTest("node not installed")

        code, _ = self._parse_errors(
            "posthog.capture_exception(error, { event: 'x', props: { a: 1 });"
        )
        self.assertNotEqual(code, 0)

    def test_parser_accepts_valid_js(self):
        if shutil.which("node") is None:
            self.skipTest("node not installed")

        code, _ = self._parse_errors("posthog.capture_exception(error, { event: 'x' });")
        self.assertEqual(code, 0)
