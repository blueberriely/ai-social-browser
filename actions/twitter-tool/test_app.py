import asyncio
import inspect
import json as jsonlib
import os
import tempfile
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi.responses import JSONResponse
from starlette.requests import Request

import app as tw


AUTH_ENV = ("TW_SERVICE_TOKEN", "TW_ALLOW_WRITE", "TW_EXPECTED_ACTOR")
TEST_SERVICE_TOKEN = "test-service-token-0123456789abcdef"


class RouteResult:
    def __init__(self, value):
        if isinstance(value, JSONResponse):
            self.status_code = value.status_code
            self._body = jsonlib.loads(value.body.decode("utf-8"))
        else:
            self.status_code = 200
            self._body = value

    def json(self):
        return self._body


class TwitterToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old_env = {name: os.environ.get(name) for name in AUTH_ENV}
        for name in AUTH_ENV:
            os.environ.pop(name, None)

        self.tmp = tempfile.TemporaryDirectory()
        self.old_state_path = tw.STATE_PATH
        self.old_log_path = tw.LOG_PATH
        tw.STATE_PATH = Path(self.tmp.name) / "runtime-state.json"
        tw.LOG_PATH = Path(self.tmp.name) / "actions.log"
        tw._idempotency.clear()
        tw._owned_targets.clear()
        tw._owned_pages.clear()
        tw._rate_windows = {group: deque() for group in tw.RATE_LIMITS}
        tw._state_loaded = True
        tw._state_healthy = True
        tw._state_health_error = None
        tw.WRITE_LOCK = asyncio.Lock()
        tw.READ_SEM = asyncio.Semaphore(2)

    async def asyncTearDown(self):
        tw.STATE_PATH = self.old_state_path
        tw.LOG_PATH = self.old_log_path
        for name, value in self.old_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.tmp.cleanup()

    async def request(self, method, path, *, headers=None, json=None, content=None):
        header_map = {key.lower(): value for key, value in (headers or {}).items()}
        if json is not None:
            body = jsonlib.dumps(json, ensure_ascii=False).encode("utf-8")
            header_map.setdefault("content-type", "application/json")
        elif content is None:
            body = b""
        elif isinstance(content, bytes):
            body = content
        else:
            body = str(content).encode("utf-8")

        sent = False

        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": [
                (key.encode("latin-1"), value.encode("latin-1"))
                for key, value in header_map.items()
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8272),
        }
        req = Request(scope, receive)
        value = await (tw.health(req) if path == "/health" else tw.twitter(req))
        return RouteResult(value)

    def enable_auth(self, *, write=False, actor="owner"):
        os.environ["TW_SERVICE_TOKEN"] = TEST_SERVICE_TOKEN
        os.environ["TW_ALLOW_WRITE"] = "1" if write else "0"
        os.environ["TW_EXPECTED_ACTOR"] = actor

    @property
    def auth(self):
        return {"authorization": f"Bearer {TEST_SERVICE_TOKEN}"}

    async def test_health_requires_token_and_reports_policy(self):
        response = await self.request("GET", "/health")
        self.assertEqual(response.status_code, 503)

        self.enable_auth(write=True, actor="@Owner")
        with mock.patch.object(
                tw, "get_context",
                new=mock.AsyncMock(return_value=SimpleNamespace(pages=[]))):
            response = await self.request("GET", "/health", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["service"], "ai-social-browser-twitter")
        self.assertEqual(body["api_version"], 2)
        self.assertTrue(body["allow_write"])
        self.assertEqual(body["expected_actor"], "owner")

        response = await self.request(
            "GET", "/health", headers={"authorization": "Bearer wrong"})
        self.assertEqual(response.status_code, 401)

    def test_public_schema_and_interactive_docs_are_disabled(self):
        paths = {route.path for route in tw.app.routes}
        self.assertEqual(paths, {"/health", "/twitter"})
        self.assertNotIn("/openapi.json", paths)
        self.assertNotIn("/docs", paths)
        self.assertNotIn("/redoc", paths)

    async def test_health_rejects_short_and_example_placeholder_tokens(self):
        for token in (
                "short-secret",
                "replace-with-a-long-random-secret",
                "密" * 32):
            with self.subTest(token=token):
                os.environ["TW_SERVICE_TOKEN"] = token
                response = await self.request(
                    "GET", "/health",
                    headers={"authorization": "Bearer definitely-not-the-token"})
                self.assertEqual(response.status_code, 503)
                self.assertIn("至少 32 字节", response.json()["error"])

    async def test_non_ascii_authorization_safely_returns_401(self):
        self.enable_auth()
        response = await self.request(
            "GET", "/health",
            headers={"authorization": "Bearer \xff" * 40})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "未授权")

    async def test_twitter_rejects_content_type_and_remote_origin(self):
        self.enable_auth()
        response = await self.request(
            "POST", "/twitter", headers=self.auth, content="{}")
        self.assertEqual(response.status_code, 415)

        headers = {**self.auth, "origin": "https://evil.example"}
        response = await self.request(
            "POST", "/twitter", headers=headers, json={"action": "feed"})
        self.assertEqual(response.status_code, 403)

        headers = {**self.auth, "origin": "http://127.0.0.1:3000"}
        with mock.patch.object(
                tw, "get_context", new=mock.AsyncMock(side_effect=RuntimeError("fake"))):
            response = await self.request(
                "POST", "/twitter", headers=headers, json={"action": "feed"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("fake", response.json()["error"])

    async def test_write_gate_fails_before_browser_or_idempotency(self):
        self.enable_auth(write=False)
        with mock.patch.object(tw, "get_context", new=mock.AsyncMock()) as get_context:
            response = await self.request(
                "POST", "/twitter", headers=self.auth,
                json={"action": "post", "text": "nope"})
        self.assertEqual(response.json()["outcome"], "failed")
        self.assertIn("TW_ALLOW_WRITE", response.json()["error"])
        get_context.assert_not_awaited()
        self.assertFalse(tw._idempotency)

    async def test_enabled_write_requires_idempotency_key(self):
        self.enable_auth(write=True)
        with mock.patch.object(tw, "get_context", new=mock.AsyncMock()) as get_context:
            response = await self.request(
                "POST", "/twitter", headers=self.auth,
                json={"action": "post", "text": "no key"})
        self.assertEqual(response.json()["outcome"], "failed")
        self.assertIn("Idempotency-Key", response.json()["error"])
        get_context.assert_not_awaited()

    async def test_wrong_actor_blocks_action_before_any_click(self):
        self.enable_auth(write=True, actor="owner")
        action = mock.AsyncMock(return_value=tw._confirmed("should not happen"))
        old = tw.WRITE_ACTIONS["like"]
        tw.WRITE_ACTIONS["like"] = action
        try:
            with (
                mock.patch.object(tw, "get_context", new=mock.AsyncMock(return_value=object())),
                mock.patch.object(
                    tw, "_verify_expected_actor",
                    new=mock.AsyncMock(return_value=(
                        "other", tw._failed(
                            "当前登录账号是 @other，不是要求的 @owner，拒绝写入",
                            actor_handle="@other")))),
            ):
                response = await self.request(
                    "POST", "/twitter", headers={
                        **self.auth, "idempotency-key": "actor-wrong-001"},
                    json={"action": "like", "url": "https://x.com/a/status/1"})
        finally:
            tw.WRITE_ACTIONS["like"] = old
        body = response.json()
        self.assertEqual(body["outcome"], "failed")
        self.assertEqual(body["actor_handle"], "@other")
        self.assertFalse(body["write_attempted"])
        self.assertTrue(body["idempotency_released"])
        self.assertNotIn("actor-wrong-001", tw._idempotency)
        action.assert_not_awaited()

    async def test_current_actor_reads_and_cross_checks_real_page_markers(self):
        class Locator:
            def __init__(self, *, text=None, href=None, count=1):
                self.text = text
                self.href = href
                self._count = count
                self.first = self

            async def count(self):
                return self._count

            async def inner_text(self):
                return self.text

            async def get_attribute(self, name):
                return self.href if name == "href" else None

        class Page:
            def locator(self, selector):
                if "AccountSwitcher" in selector:
                    return Locator(text="Wrong Account\n@Wrong")
                return Locator(href="/Wrong")

        actual, error = await tw._read_current_actor(Page())
        self.assertIsNone(error)
        self.assertEqual(actual, "wrong")

        class ConflictPage(Page):
            def locator(self, selector):
                if "AccountSwitcher" in selector:
                    return Locator(text="Wrong Account\n@Wrong")
                return Locator(href="/Other")

        actual, error = await tw._read_current_actor(ConflictPage())
        self.assertIsNone(actual)
        self.assertIn("冲突", error)

    async def test_exact_status_not_found_never_falls_back_to_first_article(self):
        with (
            mock.patch.object(tw, "goto", new=mock.AsyncMock()),
            mock.patch.object(tw, "wait_tweets", new=mock.AsyncMock(return_value=None)),
            mock.patch.object(
                tw, "_find_exact_status_article",
                new=mock.AsyncMock(return_value=None)),
        ):
            article, target, error = await tw._open_main_article(
                object(), "https://x.com/right/status/123")
        self.assertIsNone(article)
        self.assertIsNone(target)
        self.assertEqual(error["outcome"], "failed")
        self.assertIn("精确 status", error["error"])

    async def test_read_action_never_returns_a_different_status_as_main(self):
        tweets = [{
            "url": "https://x.com/wrong/status/999",
            "handle": "@wrong",
            "text": "wrong tweet",
        }]
        with (
            mock.patch.object(tw, "new_page", new=mock.AsyncMock(return_value=object())),
            mock.patch.object(tw, "goto", new=mock.AsyncMock()),
            mock.patch.object(tw, "wait_tweets", new=mock.AsyncMock(return_value=None)),
            mock.patch.object(tw, "collect_tweets", new=mock.AsyncMock(return_value=tweets)),
            mock.patch.object(tw, "_close_owned_page", new=mock.AsyncMock()),
        ):
            result = await tw.act_read(
                object(), {"url": "https://x.com/right/status/123"})
        self.assertFalse(result["ok"])
        self.assertIn("精确 status", result["error"])

    async def test_card_click_ignores_target_status_inside_nested_quote(self):
        class Collection:
            def __init__(self, items):
                self.items = items

            async def count(self):
                return len(self.items)

            def nth(self, index):
                return self.items[index]

        class Article:
            async def evaluate(self, script):
                self.assertIs(script, tw.OWN_STATUS_HREF_JS)
                return "https://x.com/outer/status/999"

            def locator(self, selector):
                raise AssertionError("外层 status 不匹配时不能去点引用卡链接")

        article = Article()
        article.assertIs = self.assertIs

        class Page:
            keyboard = SimpleNamespace(press=mock.AsyncMock())
            mouse = SimpleNamespace(wheel=mock.AsyncMock())
            wait_for_timeout = mock.AsyncMock()

            def locator(self, selector):
                self_test.assertEqual(selector, 'article[data-testid="tweet"]')
                return Collection([article])

        self_test = self
        with mock.patch.object(tw, "_raise_if_challenge", new=mock.AsyncMock()):
            link = await tw._find_tweet_link(Page(), "123")
        self.assertIsNone(link)

    async def test_restore_list_never_swallows_challenge(self):
        challenge = tw.ChallengeDetected(tw._challenge_result(
            "https://x.com/i/flow/challenge"))
        page = SimpleNamespace(
            keyboard=SimpleNamespace(press=mock.AsyncMock()),
            wait_for_function=mock.AsyncMock(),
        )
        with mock.patch.object(
                tw, "wait_tweets", new=mock.AsyncMock(side_effect=challenge)):
            with self.assertRaises(tw.ChallengeDetected):
                await tw._restore_twitter_list(page, "123")

    def test_extract_js_reports_my_repost_state(self):
        self.assertIn(
            "t.reposted_by_me = !!art.querySelector('button[data-testid=\"unretweet\"]')",
            tw.EXTRACT_JS)

    async def test_exact_status_uses_article_own_timestamp_not_nested_quote(self):
        class Article:
            def __init__(self, outer_href, nested_href, name):
                self.outer_href = outer_href
                self.nested_href = nested_href
                self.name = name

            async def evaluate(self, script):
                self.assert_outer_status_guards(script)
                return self.outer_href

            @staticmethod
            def assert_outer_status_guards(script):
                assert "timeEl.closest(articleSelector) !== article" in script
                assert "containingName !== outerName" in script
                assert "wrappingLink !== anchor" in script

        # 两篇外层推文作者相同；第一篇只在引用卡里出现目标 status。
        # 只有第二篇自己的外层 timestamp 才能命中。
        wrong = Article(
            "https://x.com/same_author/status/999",
            "https://x.com/same_author/status/123",
            "same_author")
        right = Article(
            "https://x.com/same_author/status/123?s=20",
            "https://x.com/quoted/status/777",
            "same_author")

        class Articles:
            async def count(self):
                return 2

            def nth(self, index):
                return (wrong, right)[index]

        class Page:
            def locator(self, selector):
                return Articles()

            async def wait_for_timeout(self, ms):
                return None

        found = await tw._find_exact_status_article(Page(), "123", timeout_ms=20)
        self.assertIs(found, right)

    async def test_nested_target_without_outer_match_is_never_selected(self):
        test_case = self

        class DecoyArticle:
            async def evaluate(self, script):
                test_case.assertNotIn(".locator(\"time\")", inspect.getsource(
                    tw._find_exact_status_article))
                return "https://x.com/same_author/status/999"

        decoy = DecoyArticle()

        class Articles:
            async def count(self):
                return 1

            def nth(self, index):
                return decoy

        class Page:
            def locator(self, selector):
                return Articles()

            async def wait_for_timeout(self, ms):
                return None

        found = await tw._find_exact_status_article(Page(), "123", timeout_ms=2)
        self.assertIsNone(found)

    def test_outer_status_dom_guards_are_shared_by_read_and_write_paths(self):
        for script in (tw.OWN_STATUS_HREF_JS, tw.EXTRACT_JS):
            self.assertIn("timeEl.closest(articleSelector) !== article", script)
            self.assertIn("containingName !== outerName", script)
            self.assertIn("wrappingLink !== anchor", script)

    async def test_expected_target_handle_is_compared_to_actual_article_author(self):
        missing = tw._check_expected_target({}, "someone_else")
        self.assertEqual(missing["outcome"], "failed")
        self.assertIn("必须带", missing["error"])

        result = tw._check_expected_target(
            {"expected_target_handle": "@owner"}, "someone_else")
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(result["target_handle"], "@someone_else")

    async def test_like_http_403_is_uncertain_even_if_optimistic_ui_flips(self):
        class Locator:
            def __init__(self, *, flips=False):
                self.first = self
                self.flips = flips

            async def count(self):
                return 1

            async def click(self, **kwargs):
                return None

            async def wait_for(self, **kwargs):
                if not self.flips:
                    raise tw.PWTimeout("not flipped")

        class Article:
            def __init__(self, flips):
                self.flips = flips

            def locator(self, selector):
                return Locator(flips=("unlike" in selector and self.flips))

        for flips in (True, False):
            with self.subTest(flips=flips):
                verify = mock.AsyncMock(return_value=True)
                with (
                    mock.patch.object(tw, "new_page", new=mock.AsyncMock(return_value=object())),
                    mock.patch.object(
                        tw, "_open_main_article",
                        new=mock.AsyncMock(return_value=(Article(flips), "target", None))),
                    mock.patch.object(
                        tw, "_click_with_x_response",
                        new=mock.AsyncMock(return_value={
                            "captured": True,
                            "status": 403,
                            "url": "https://x.com/i/api/graphql/FavoriteTweet",
                            "payload": {"errors": [{"message": "forbidden"}]},
                        })),
                    mock.patch.object(tw, "_verify_target_button", new=verify),
                    mock.patch.object(tw, "_close_owned_page", new=mock.AsyncMock()),
                ):
                    result = await tw.act_like(object(), {
                        "action": "like",
                        "url": "https://x.com/target/status/123",
                        "expected_target_handle": "@target",
                    })
                self.assertEqual(result["outcome"], "uncertain")
                self.assertFalse(result["retry_safe"])
                self.assertIn("HTTP 403", result["error"])
                verify.assert_not_awaited()

    async def test_like_confirms_only_after_fresh_status_reread(self):
        class Locator:
            first = None

            def __init__(self):
                self.first = self

            async def count(self):
                return 1

            async def click(self, **kwargs):
                return None

            async def wait_for(self, **kwargs):
                return None

        class Article:
            def locator(self, selector):
                return Locator()

        with (
            mock.patch.object(tw, "new_page", new=mock.AsyncMock(return_value=object())),
            mock.patch.object(
                tw, "_open_main_article",
                new=mock.AsyncMock(return_value=(Article(), "target", None))),
            mock.patch.object(
                tw, "_click_with_x_response",
                new=mock.AsyncMock(return_value={
                    "captured": True, "status": 200,
                    "url": "https://x.com/i/api/graphql/FavoriteTweet",
                    "payload": {"data": {}},
                })),
            mock.patch.object(
                tw, "_verify_target_button", new=mock.AsyncMock(return_value=True)),
            mock.patch.object(tw, "_close_owned_page", new=mock.AsyncMock()),
        ):
            result = await tw.act_like(object(), {
                "action": "like",
                "url": "https://x.com/target/status/123",
                "expected_target_handle": "target",
            })
        self.assertEqual(result["outcome"], "confirmed")
        self.assertIn("重新打开精确 status", result["verified"])

    async def test_idempotency_replays_across_short_restart(self):
        self.enable_auth(write=True, actor="owner")
        calls = 0

        async def fake_post(ctx, body):
            nonlocal calls
            calls += 1
            return tw._confirmed("fake confirmation", changed=False)

        old = tw.WRITE_ACTIONS["post"]
        tw.WRITE_ACTIONS["post"] = fake_post
        try:
            with (
                mock.patch.object(tw, "get_context", new=mock.AsyncMock(return_value=object())),
                mock.patch.object(
                    tw, "_verify_expected_actor",
                    new=mock.AsyncMock(return_value=("owner", None))),
            ):
                payload = {
                    "action": "post", "text": "once",
                    "idempotency_key": "restart-key-001",
                }
                first = await self.request(
                    "POST", "/twitter", headers=self.auth, json=payload)
                tw._idempotency.clear()
                tw._state_loaded = False
                tw._load_state()
                second = await self.request(
                    "POST", "/twitter", headers=self.auth, json=payload)
        finally:
            tw.WRITE_ACTIONS["post"] = old
        self.assertEqual(first.json()["outcome"], "confirmed")
        self.assertEqual(second.json()["outcome"], "confirmed")
        self.assertTrue(second.json()["replayed"])
        self.assertEqual(calls, 1)

    def test_loads_pre_idempotency_state_file_unchanged(self):
        tw.STATE_PATH.write_text(jsonlib.dumps({
            "rate_windows": {"publish": [], "engage": []},
            "owned_targets": {},
        }), encoding="utf-8")
        tw._state_loaded = False
        tw._load_state()
        self.assertTrue(tw._state_loaded)
        self.assertEqual(tw._idempotency, {})

    async def test_corrupt_state_file_fails_closed_without_overwrite(self):
        corrupt = "{this is not json"
        tw.STATE_PATH.write_text(corrupt, encoding="utf-8")
        tw._state_loaded = False
        tw._state_healthy = True
        tw._state_health_error = None
        tw._load_state()
        self.assertFalse(tw._state_healthy)

        self.enable_auth(write=True, actor="owner")
        with mock.patch.object(tw, "get_context", new=mock.AsyncMock()) as get_context:
            response = await self.request(
                "POST", "/twitter", headers=self.auth,
                json={
                    "action": "post", "text": "must not send",
                    "idempotency_key": "corrupt-state-001",
                })
        body = response.json()
        self.assertEqual(body["outcome"], "failed")
        self.assertFalse(body["retry_safe"])
        self.assertIn("运行状态不健康", body["error"])
        get_context.assert_not_awaited()
        self.assertEqual(tw.STATE_PATH.read_text(encoding="utf-8"), corrupt)

        health = await self.request("GET", "/health", headers=self.auth)
        self.assertFalse(health.json()["ok"])
        self.assertFalse(health.json()["state_healthy"])

    def test_write_fingerprint_canonicalizes_url_text_and_actor(self):
        composed = "é"
        decomposed = "e\u0301"
        first = tw._write_fingerprint({
            "action": "reply",
            "url": "https://x.com/Owner/status/123?s=20",
            "text": f"  {decomposed}\r\nline  ",
            "expected_target_handle": "@Target",
        }, "owner")
        second = tw._write_fingerprint({
            "action": "reply",
            "url": "https://twitter.com/anything/status/123",
            "text": f"{composed}\nline",
            "expected_target_handle": "target",
        }, "owner")
        different_actor = tw._write_fingerprint({
            "action": "reply",
            "url": "https://x.com/Owner/status/123",
            "text": f"{composed}\nline",
            "expected_target_handle": "target",
        }, "other")
        self.assertEqual(first, second)
        self.assertNotEqual(first, different_actor)

        post_a = tw._write_fingerprint({
            "action": "post", "text": "hello", "url": "dummy-a",
        }, "owner")
        post_b = tw._write_fingerprint({
            "action": "post", "text": "hello", "url": "dummy-b",
        }, "owner")
        like_a = tw._write_fingerprint({
            "action": "like", "url": "https://x.com/a/status/1",
            "text": "dummy-a", "expected_target_handle": "a",
        }, "owner")
        like_b = tw._write_fingerprint({
            "action": "like", "url": "https://x.com/a/status/1",
            "text": "dummy-b", "expected_target_handle": "a",
        }, "owner")
        self.assertEqual(post_a, post_b)
        self.assertEqual(like_a, like_b)

    def test_created_tweet_id_only_comes_from_create_mutation_result(self):
        expected = "same text"
        decoy = {"rest_id": "111", "legacy": {"full_text": expected}}
        payload = {
            "data": {
                "decoy_tweet": decoy,
                "create_tweet": {
                    "tweet_results": {
                        "result": {
                            "__typename": "Tweet",
                            "rest_id": "222",
                            "legacy": {"full_text": expected},
                        },
                    },
                },
            },
        }
        self.assertEqual(tw._find_created_tweet_id(payload, expected), "222")
        self.assertIsNone(tw._find_created_tweet_id(
            {"data": {"decoy_tweet": decoy}}, expected))

        wrapped = {
            "data": {"create_tweet": {"tweet_results": {"result": {
                "__typename": "TweetWithVisibilityResults",
                "tweet": {
                    "rest_id": "333",
                    "legacy": {"full_text": expected},
                },
            }}}},
        }
        self.assertEqual(tw._find_created_tweet_id(wrapped, expected), "333")

    async def test_created_tweet_fresh_read_requires_actor_and_exact_text(self):
        class TextLocator:
            def __init__(self, text):
                self.text = text
                self.first = self

            async def count(self):
                return 1

            async def inner_text(self):
                return self.text

        class Article:
            def __init__(self, text):
                self.text = text

            def locator(self, selector):
                return TextLocator(self.text)

        with mock.patch.object(
                tw, "_open_main_article",
                new=mock.AsyncMock(return_value=(Article("hello"), "intruder", None))):
            self.assertFalse(await tw._verify_created_tweet(
                object(), "123", "hello", "owner"))

        with mock.patch.object(
                tw, "_open_main_article",
                new=mock.AsyncMock(return_value=(Article("hello world"), "owner", None))):
            self.assertFalse(await tw._verify_created_tweet(
                object(), "123", "hello\nworld", "owner"))

        with mock.patch.object(
                tw, "_open_main_article",
                new=mock.AsyncMock(return_value=(Article("hello\nworld"), "owner", None))):
            self.assertTrue(await tw._verify_created_tweet(
                object(), "123", "hello\nworld", "@Owner"))

    def test_systemd_unit_uses_private_file_umask(self):
        service = (Path(__file__).parents[1] / "systemd" / "twitter-tool.service")
        self.assertIn("UMask=0077", service.read_text(encoding="utf-8"))

    async def test_safe_preclick_failure_releases_key_for_same_key_retry(self):
        self.enable_auth(write=True, actor="owner")
        calls = 0

        async def fail_then_confirm(ctx, body):
            nonlocal calls
            calls += 1
            if calls == 1:
                return tw._failed("temporary preflight failure")
            return tw._confirmed("second attempt worked", changed=False)

        old = tw.WRITE_ACTIONS["post"]
        tw.WRITE_ACTIONS["post"] = fail_then_confirm
        try:
            with (
                mock.patch.object(tw, "get_context", new=mock.AsyncMock(return_value=object())),
                mock.patch.object(
                    tw, "_verify_expected_actor",
                    new=mock.AsyncMock(return_value=("owner", None))),
            ):
                payload = {
                    "action": "post", "text": "retry me",
                    "idempotency_key": "retry-safe-key-001",
                }
                first = await self.request(
                    "POST", "/twitter", headers=self.auth, json=payload)
                second = await self.request(
                    "POST", "/twitter", headers=self.auth, json=payload)
        finally:
            tw.WRITE_ACTIONS["post"] = old
        self.assertEqual(first.json()["outcome"], "failed")
        self.assertTrue(first.json()["idempotency_released"])
        self.assertEqual(second.json()["outcome"], "confirmed")
        self.assertEqual(calls, 2)

    def test_in_progress_idempotency_record_blocks_resend_after_restart(self):
        fingerprint = tw._write_fingerprint({
            "action": "post", "text": "possibly sent",
        }, "owner")
        result, claimed = tw._claim_idempotency("in-progress-key-001", fingerprint)
        self.assertTrue(claimed)
        self.assertIsNone(result)

        tw._idempotency.clear()
        tw._state_loaded = False
        tw._load_state()
        result, claimed = tw._claim_idempotency("in-progress-key-001", fingerprint)
        self.assertFalse(claimed)
        self.assertEqual(result["outcome"], "uncertain")
        self.assertFalse(result["retry_safe"])

    def test_prune_never_expires_in_progress_or_uncertain_records(self):
        now = 2_000_000_000.0
        old = now - tw.IDEMPOTENCY_TTL_SECONDS - 10
        tw._idempotency.update({
            "in-progress-old-001": {
                "fingerprint": "a", "status": "in_progress", "result": None,
                "created_at": old, "updated_at": old,
            },
            "uncertain-old-001": {
                "fingerprint": "b", "status": "completed",
                "result": tw._uncertain("unknown"),
                "created_at": old, "updated_at": old,
            },
            "confirmed-old-001": {
                "fingerprint": "c", "status": "completed",
                "result": tw._confirmed("done"),
                "created_at": old, "updated_at": old,
            },
        })
        tw._prune_idempotency(now=now)
        self.assertIn("in-progress-old-001", tw._idempotency)
        self.assertIn("uncertain-old-001", tw._idempotency)
        self.assertNotIn("confirmed-old-001", tw._idempotency)

    def test_load_keeps_old_in_progress_and_uncertain_records(self):
        now = 2_000_000_000.0
        old = now - tw.IDEMPOTENCY_TTL_SECONDS - 10
        payload = {
            "rate_windows": {"publish": [], "engage": []},
            "owned_targets": {},
            "idempotency": {
                "in-progress-load-001": {
                    "fingerprint": "a", "status": "in_progress", "result": None,
                    "created_at": old, "updated_at": old,
                },
                "uncertain-load-001": {
                    "fingerprint": "b", "status": "completed",
                    "result": tw._uncertain("unknown"),
                    "created_at": old, "updated_at": old,
                },
                "confirmed-load-001": {
                    "fingerprint": "c", "status": "completed",
                    "result": tw._confirmed("done"),
                    "created_at": old, "updated_at": old,
                },
            },
        }
        tw.STATE_PATH.write_text(jsonlib.dumps(payload), encoding="utf-8")
        tw._state_loaded = False
        with mock.patch.object(tw.time, "time", return_value=now):
            tw._load_state()
        self.assertTrue(tw._state_healthy)
        self.assertIn("in-progress-load-001", tw._idempotency)
        self.assertIn("uncertain-load-001", tw._idempotency)
        self.assertNotIn("confirmed-load-001", tw._idempotency)

    def test_state_persist_fsyncs_file_before_replace_then_parent(self):
        events = []
        original_fsync = tw.os.fsync
        original_replace = tw.os.replace

        def fsync_file(fd):
            events.append("file_fsync")
            return original_fsync(fd)

        def replace_file(source, target):
            events.append("replace")
            return original_replace(source, target)

        def fsync_parent(path):
            events.append("parent_fsync")

        with (
            mock.patch.object(tw.os, "fsync", side_effect=fsync_file),
            mock.patch.object(tw.os, "replace", side_effect=replace_file),
            mock.patch.object(tw, "_fsync_parent_directory", side_effect=fsync_parent),
        ):
            tw._persist_state()
        self.assertEqual(events, ["file_fsync", "replace", "parent_fsync"])
        source = inspect.getsource(tw._persist_state)
        self.assertLess(source.index("os.fsync(handle.fileno())"),
                        source.index("os.replace(tmp, STATE_PATH)"))
        self.assertLess(source.index("os.replace(tmp, STATE_PATH)"),
                        source.index("_fsync_parent_directory(STATE_PATH.parent)"))

    def test_windows_directory_fsync_unsupported_is_tolerated(self):
        with (
            mock.patch.object(tw.os, "name", "nt"),
            mock.patch.object(tw.os, "open", side_effect=OSError("unsupported")),
        ):
            tw._fsync_parent_directory(tw.STATE_PATH.parent)

    async def test_completed_result_downgrades_if_final_state_cannot_persist(self):
        self.enable_auth(write=True, actor="owner")

        async def fake_post(ctx, body):
            return tw._confirmed("fake confirmation", changed=False)

        old = tw.WRITE_ACTIONS["post"]
        tw.WRITE_ACTIONS["post"] = fake_post
        audit_log = mock.Mock()
        try:
            with (
                mock.patch.object(tw, "get_context", new=mock.AsyncMock(return_value=object())),
                mock.patch.object(
                    tw, "_verify_expected_actor",
                    new=mock.AsyncMock(return_value=("owner", None))),
                mock.patch.object(tw, "log_action", audit_log),
                mock.patch.object(
                    tw, "_persist_state",
                    side_effect=[None, OSError("disk full")]),
            ):
                response = await self.request(
                    "POST", "/twitter", headers=self.auth,
                    json={
                        "action": "post", "text": "once",
                        "idempotency_key": "disk-fail-key-001",
                    })
        finally:
            tw.WRITE_ACTIONS["post"] = old
        body = response.json()
        self.assertEqual(body["outcome"], "uncertain")
        self.assertFalse(body["retry_safe"])
        self.assertIn("幂等状态无法落盘", body["error"])
        self.assertEqual(tw._idempotency["disk-fail-key-001"]["status"], "in_progress")
        self.assertEqual(audit_log.call_count, 2)
        first_record = audit_log.call_args_list[0].args[0]
        correction = audit_log.call_args_list[1].args[0]
        self.assertEqual(first_record["action"], "post")
        self.assertEqual(first_record["result"]["outcome"], "confirmed")
        self.assertEqual(correction["action"], "_write_finalization_failed")
        self.assertEqual(correction["original_action"], "post")
        self.assertEqual(correction["idempotency_key"], "disk-fail-key-001")
        self.assertEqual(correction["result"]["outcome"], "uncertain")

    async def test_finalization_correction_log_failure_stays_uncertain(self):
        self.enable_auth(write=True, actor="owner")

        async def fake_post(ctx, body):
            return tw._confirmed("fake confirmation", changed=False)

        old = tw.WRITE_ACTIONS["post"]
        tw.WRITE_ACTIONS["post"] = fake_post
        try:
            with (
                mock.patch.object(tw, "get_context", new=mock.AsyncMock(return_value=object())),
                mock.patch.object(
                    tw, "_verify_expected_actor",
                    new=mock.AsyncMock(return_value=("owner", None))),
                mock.patch.object(
                    tw, "log_action",
                    side_effect=[None, OSError("correction log failed")]),
                mock.patch.object(
                    tw, "_persist_state",
                    side_effect=[None, OSError("disk full")]),
            ):
                response = await self.request(
                    "POST", "/twitter", headers=self.auth,
                    json={
                        "action": "post", "text": "once",
                        "idempotency_key": "disk-log-fail-key-001",
                    })
        finally:
            tw.WRITE_ACTIONS["post"] = old
        body = response.json()
        self.assertEqual(body["outcome"], "uncertain")
        self.assertFalse(body["retry_safe"])
        self.assertIn("幂等状态无法落盘", body["error"])

    async def test_audit_log_failure_downgrades_confirmed_result(self):
        self.enable_auth(write=True, actor="owner")

        async def fake_post(ctx, body):
            return tw._confirmed("fake confirmation", changed=False)

        old = tw.WRITE_ACTIONS["post"]
        tw.WRITE_ACTIONS["post"] = fake_post
        try:
            with (
                mock.patch.object(tw, "get_context", new=mock.AsyncMock(return_value=object())),
                mock.patch.object(
                    tw, "_verify_expected_actor",
                    new=mock.AsyncMock(return_value=("owner", None))),
                mock.patch.object(tw, "log_action", side_effect=OSError("read only")),
            ):
                response = await self.request(
                    "POST", "/twitter", headers=self.auth,
                    json={
                        "action": "post", "text": "once",
                        "idempotency_key": "log-fail-key-001",
                    })
        finally:
            tw.WRITE_ACTIONS["post"] = old
        body = response.json()
        self.assertEqual(body["outcome"], "uncertain")
        self.assertFalse(body["retry_safe"])
        self.assertIn("审计日志无法落盘", body["error"])

    async def test_challenge_before_click_requires_human_and_same_key_never_reexecutes(self):
        self.enable_auth(write=True, actor="owner")
        calls = 0

        async def challenge_before_click(action, body, expected_actor):
            nonlocal calls
            calls += 1
            raise tw.ChallengeDetected(tw._challenge_result(
                "https://x.com/account/access"))

        key = "challenge-before-click-001"
        with mock.patch.object(tw, "_execute_write", new=challenge_before_click):
            first = await self.request("POST", "/twitter", headers=self.auth, json={
                "action": "post", "text": "once", "idempotency_key": key,
            })
            tw._idempotency.clear()
            tw._state_loaded = False
            tw._load_state()
            second = await self.request("POST", "/twitter", headers=self.auth, json={
                "action": "post", "text": "once", "idempotency_key": key,
            })

        body = first.json()
        self.assertEqual(body["outcome"], "failed")
        self.assertEqual(body["status"], "challenge")
        self.assertFalse(body["retry_safe"])
        self.assertFalse(body["write_attempted"])
        self.assertTrue(body["manual_intervention_required"])
        self.assertEqual(len(tw._rate_windows["publish"]), 0)
        self.assertTrue(tw._state_healthy)
        self.assertTrue(second.json()["replayed"])
        self.assertEqual(calls, 1)

    async def test_challenge_after_click_is_uncertain_counted_and_never_reexecutes(self):
        self.enable_auth(write=True, actor="owner")
        calls = 0

        async def challenge_after_click(action, body, expected_actor):
            nonlocal calls
            calls += 1
            tracker = tw._write_click_attempted.get()
            tw.record_rate(action)
            tracker["rate_recorded"] = True
            tracker["attempted"] = True
            raise tw.ChallengeDetected(tw._challenge_result(
                "https://x.com/i/flow/challenge"))

        key = "challenge-after-click-001"
        with mock.patch.object(tw, "_execute_write", new=challenge_after_click):
            first = await self.request("POST", "/twitter", headers=self.auth, json={
                "action": "post", "text": "once", "idempotency_key": key,
            })
            tw._idempotency.clear()
            tw._state_loaded = False
            tw._load_state()
            second = await self.request("POST", "/twitter", headers=self.auth, json={
                "action": "post", "text": "once", "idempotency_key": key,
            })

        body = first.json()
        self.assertEqual(body["outcome"], "uncertain")
        self.assertEqual(body["status"], "challenge")
        self.assertFalse(body["retry_safe"])
        self.assertTrue(body["write_attempted"])
        self.assertTrue(body["manual_intervention_required"])
        self.assertEqual(len(tw._rate_windows["publish"]), 1)
        self.assertTrue(tw._state_healthy)
        self.assertTrue(second.json()["replayed"])
        self.assertEqual(calls, 1)

    async def test_every_platform_click_attempt_reserves_rate_before_click(self):
        self.enable_auth(write=True, actor="owner")

        class ResponseContext:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        class Page:
            def expect_response(self, predicate, timeout):
                return ResponseContext()

        async def broken_click():
            raise RuntimeError("detached")

        tracker = {
            "attempted": False,
            "rate_recorded": False,
            "action": "like",
            "expected_actor": "owner",
        }
        token = tw._write_click_attempted.set(tracker)
        try:
            with mock.patch.object(
                    tw, "_read_current_actor",
                    new=mock.AsyncMock(return_value=("owner", None))):
                with self.assertRaises(RuntimeError):
                    await tw._click_with_x_response(Page(), broken_click, ["FavoriteTweet"])
        finally:
            tw._write_click_attempted.reset(token)
        self.assertTrue(tracker["attempted"])
        self.assertTrue(tracker["rate_recorded"])
        self.assertEqual(len(tw._rate_windows["engage"]), 1)
        self.assertTrue(tw.STATE_PATH.exists())


if __name__ == "__main__":
    unittest.main()
