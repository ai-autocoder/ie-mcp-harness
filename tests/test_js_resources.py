import re
import unittest
from pathlib import Path

from ie_mcp import js

JS_DIR = Path(js.__file__).parent
FORBIDDEN = ["=>", "`", r"\blet\b", r"\bconst\b", r"JSON\.", r"\.forEach\(", r"\.map\(",
             r"\.filter\(", r"Object\.keys", r"\.trim\(", r"\.apply\(", r"\.call\("]


def _scripts():
    found = sorted(JS_DIR.glob("*.js"))
    assert found, f"no scripts in {JS_DIR}"
    return found


class JsResourceTests(unittest.TestCase):
    def test_js_resources_are_es3(self):
        for path in _scripts():
            text = path.read_text(encoding="utf-8")
            for pattern in FORBIDDEN:
                self.assertIsNone(re.search(pattern, text),
                                  f"{path.name} contains forbidden construct {pattern!r}")
            for number, line in enumerate(text.splitlines(), 1):
                if "querySelector" in line:
                    self.assertIn("documentMode", line,
                                  f"{path.name}:{number} uses querySelector without documentMode")

    def test_js_resources_have_no_raw_separator_bytes(self):
        for path in _scripts():
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("\x1e", text, f"{path.name} has a raw record separator")
            self.assertNotIn("\x1f", text, f"{path.name} has a raw field separator")

    def test_load_returns_script_text(self):
        self.assertIn("getElementsByTagName('table')", js.load("grid.js"))

    def test_expected_scripts_exist(self):
        for name in ("select.js", "grid.js", "ready_probe.js", "frames.js", "instrument.js", "hook.js",
                     "drain.js"):
            self.assertTrue(js.load(name).strip(), name)


if __name__ == "__main__":
    unittest.main()
