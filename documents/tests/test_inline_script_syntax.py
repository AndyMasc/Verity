"""Inline scripts in templates must actually parse.

A syntax error anywhere in an inline <script type="module"> kills the whole module,
so every listener in it silently stops being attached. That is invisible to the view
tests and only shows up as a dead control in the browser: an unreferenced identifier
or a malformed object literal makes "click the upload dropzone" do nothing at all.
"""

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.template.loader import render_to_string
from django.test import SimpleTestCase

TEMPLATE_ROOT = Path(__file__).resolve().parents[2]

# Rendered with the smallest context each one accepts; anything missing is simply
# empty, which is harmless for a syntax check.
TEMPLATES = {
    "documents/upload_file.html": {
        "api_url": "/billing/x/",
        "redirect_url_template": "/records/add/0/",
        "is_supporting_flow": False,
    },
    # upload_supporting_files.html extends upload_base.html and inherits the same
    # module script, so upload_file.html already covers it.
    "billing/partials/pricing_cards.html": {"request": None, "base_plans": []},
}


def _inline_scripts(html: str) -> list[str]:
    scripts = []
    for match in re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.DOTALL):
        body = match.group(1).strip()
        if body:
            scripts.append(body)
    return scripts


def _strip_template_tags(source: str) -> str:
    source = re.sub(r"\{%.*?%\}", "", source, flags=re.DOTALL)
    return re.sub(r"\{\{.*?\}\}", '""', source, flags=re.DOTALL)


class InlineScriptSyntaxTests(SimpleTestCase):
    def test_inline_scripts_parse(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")

        failures = []
        checked = 0
        for template, context in TEMPLATES.items():
            html = render_to_string(template, context)
            for index, script in enumerate(_inline_scripts(html)):
                checked += 1
                with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as handle:
                    handle.write(_strip_template_tags(script))
                    path = handle.name
                result = subprocess.run([node, "--check", path], capture_output=True, text=True)
                Path(path).unlink()
                if result.returncode != 0:
                    detail = next(
                        (
                            line
                            for line in result.stderr.splitlines()
                            if "Error" in line or "SyntaxError" in line
                        ),
                        result.stderr.splitlines()[0] if result.stderr else "",
                    )
                    failures.append(f"{template} script #{index}: {detail.strip()}")

        self.assertEqual(failures, [], "Inline scripts failed to parse:\n" + "\n".join(failures))
        self.assertGreater(checked, 0, "expected at least one inline script to check")
