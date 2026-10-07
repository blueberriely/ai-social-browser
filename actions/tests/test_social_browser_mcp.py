import importlib.util
import unittest
from pathlib import Path


SERVER = Path(__file__).parents[1] / "social-browser-mcp" / "server.py"
SPEC = importlib.util.spec_from_file_location("social_browser_mcp", SERVER)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class SocialBrowserMcpTests(unittest.TestCase):
    def test_lists_read_only_tools(self):
        response = MODULE.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {tool["name"] for tool in response["result"]["tools"]}
        self.assertEqual(names, {
            "xhs_feed", "xhs_search", "xhs_read", "xhs_watch_video", "xhs_profile",
            "x_feed", "x_read", "x_watch_video", "x_profile", "video_watch",
        })

    def test_x_write_tools_are_disabled_by_default(self):
        names = {tool["name"] for tool in MODULE.TOOLS}
        self.assertNotIn("x_post", names)
        self.assertNotIn("x_reply", names)

    def test_rejects_media_outside_platform_allowlist(self):
        with self.assertRaisesRegex(ValueError, "允许"):
            MODULE._safe_public_url(
                "https://example.com/video.mp4", ("xhscdn.com",)
            )

    def test_rejects_insecure_media_before_dns_lookup(self):
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            MODULE._safe_public_url("http://video.twimg.com/example.mp4")

    def test_local_video_is_disabled_without_roots(self):
        old_roots = MODULE.LOCAL_ROOTS
        MODULE.LOCAL_ROOTS = ()
        try:
            with self.assertRaisesRegex(ValueError, "未启用"):
                MODULE._local_video(__file__)
        finally:
            MODULE.LOCAL_ROOTS = old_roots

    def test_initialize_declares_tools(self):
        response = MODULE.handle({
            "jsonrpc": "2.0",
            "id": 7,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        })
        self.assertEqual(response["id"], 7)
        self.assertEqual(response["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(response["result"]["serverInfo"]["name"], "social-browser")
        self.assertIn("tools", response["result"]["capabilities"])


if __name__ == "__main__":
    unittest.main()
