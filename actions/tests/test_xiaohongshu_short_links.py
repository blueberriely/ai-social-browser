import importlib.util
import unittest
from pathlib import Path


APP = Path(__file__).parents[1] / "xiaohongshu-tool" / "app.py"
SPEC = importlib.util.spec_from_file_location("xiaohongshu_tool", APP)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class XiaohongshuShortLinkTests(unittest.TestCase):
    def test_accepts_current_short_link_domains(self):
        self.assertTrue(MODULE._is_short_link("https://xhslink.com/a/example"))
        self.assertTrue(MODULE._is_short_link("https://xhslink.cn/o/example"))

    def test_rejects_lookalike_domains(self):
        self.assertFalse(MODULE._is_short_link("https://xhslink.cn.example.com/o/example"))
        self.assertFalse(MODULE._is_short_link("https://example.com/o/example"))


if __name__ == "__main__":
    unittest.main()
