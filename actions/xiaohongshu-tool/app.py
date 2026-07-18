"""xiaohongshu-tool: 小红书只读 HTTP 包装。

连接 browser-relay 的常驻 headed Chrome（CDP 9333），复用人工登录态。
调用方：任何会发 HTTP 的 agent 或脚本。

POST /xiaohongshu
  {"action":"feed", "n":10}
  {"action":"search", "query":"咖啡", "n":10}
  {"action":"read", "url":"https://www.xiaohongshu.com/...", "n":10}
  {"action":"profile", "url":"https://www.xiaohongshu.com/user/profile/...", "n":10}

read/profile 的 url 支持 App 分享的 xhslink.com 短链，会先自动展开成主站链接。
只读边界：允许为搜索进行导航、点击、输入、滚动和 DOM 读取；不包含发布或互动写动作。
"""

import asyncio
import os
import random
import re
import time
import urllib.error
import urllib.request
from contextlib import suppress
from urllib.parse import urljoin, urlparse

from fastapi import FastAPI, Request
from playwright.async_api import TimeoutError as PWTimeout
from playwright.async_api import async_playwright


CDP_URL = os.environ.get("XHS_CDP_URL", "http://127.0.0.1:9333").rstrip("/")
BASE = "https://www.xiaohongshu.com"
MAX_ITEMS = 30
MAX_COMMENTS = 40
BROWSE_TTL_SECONDS = max(30, int(os.environ.get("XHS_BROWSE_TTL_SECONDS", "180")))

# 小红书页面较重；VPS 上一次只开一个工作页，避免和人工 relay 页面抢内存。
ACTION_LOCK = asyncio.Lock()
_PAGE_LOCK = asyncio.Lock()
_CONN_LOCK = asyncio.Lock()
_pw = None
_browser = None
_browse_session = None  # 最近一次 feed/search/profile 列表页
_sweeper_task = None

app = FastAPI(title="xiaohongshu-tool", version="1.0.0")


class ChallengeDetected(RuntimeError):
    """页面进入滑块/验证码风控；由路由层统一返回结构化状态。"""

    def __init__(self, result):
        super().__init__(result["hint"])
        self.result = result


XHS_CHALLENGE_PATH_MARKERS = (
    "/captcha", "/challenge", "/risk-control", "/security/verify",
)
XHS_CHALLENGE_SELECTORS = ", ".join((
    'iframe[src*="captcha" i]',
    'iframe[src*="challenge" i]',
    'iframe[src*="verify" i]',
    '.captcha-container',
    '[class*="geetest" i]',
    '[class*="yidun" i]',
    '[class*="slide-verify" i]',
    '[class*="slider-verify" i]',
    '[id*="captcha" i]',
))


def _challenge_result(url):
    return {
        "ok": False,
        "outcome": "challenge",
        "status": "challenge",
        "changed": False,
        "retry_safe": False,
        "platform": "xiaohongshu",
        "url": url,
        "hint": "检测到小红书滑块/验证码，请用手机接管 browser-relay 完成验证后再试",
    }


async def _raise_if_challenge(page):
    path = urlparse(page.url or "").path.lower()
    if any(marker in path for marker in XHS_CHALLENGE_PATH_MARKERS):
        raise ChallengeDetected(_challenge_result(page.url))
    try:
        matches = page.locator(XHS_CHALLENGE_SELECTORS)
        for index in range(min(await matches.count(), 5)):
            if await matches.nth(index).is_visible():
                raise ChallengeDetected(_challenge_result(page.url))
    except ChallengeDetected:
        raise
    except Exception:
        return


async def _guarded_click(page, locator, **kwargs):
    await _raise_if_challenge(page)
    await locator.click(**kwargs)
    await _raise_if_challenge(page)


async def _human_type(page, text):
    for char in text:
        delay = random.randint(20, 80)
        if char.isascii():
            await page.keyboard.type(char, delay=delay)
        else:
            await page.keyboard.insert_text(char)
            await asyncio.sleep(delay / 1000)
        if char in "，。！？,.!?\n" or random.random() < 0.06:
            await asyncio.sleep(random.uniform(0.12, 0.36))


def _bounded_int(value, default, maximum):
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(1, min(number, maximum))


def _safe_xhs_url(value, kind="any"):
    """只允许小红书 Web 主站 URL，避免把服务变成任意 URL 抓取器。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("缺少小红书 URL")
    raw = value.strip()
    if raw.startswith("/"):
        raw = BASE + raw
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or host not in {
        "xiaohongshu.com",
        "www.xiaohongshu.com",
    }:
        raise ValueError("只接受 https://www.xiaohongshu.com/ 下的 URL")
    path = parsed.path or "/"
    if kind == "note" and not any(
        marker in path for marker in (
            "/explore/", "/discovery/item/", "/search_result/", "/user/profile/")
    ):
        raise ValueError("这不是可识别的小红书笔记 URL")
    if kind == "profile" and "/user/profile/" not in path:
        raise ValueError("这不是可识别的小红书用户主页 URL")
    return raw.replace("http://", "https://", 1)


_SHORT_LINK_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_SHORT_LINK_OPENER = urllib.request.build_opener(_NoRedirect())


def _is_short_link(value):
    if not isinstance(value, str):
        return False
    host = (urlparse(value.strip()).hostname or "").lower()
    return host == "xhslink.com" or host.endswith(".xhslink.com")


def _expand_short_link_sync(raw):
    """把 App 分享的 xhslink 短链展开成主站链接（保留 xsec_token）。

    手动跟 Location 跳转：只向 xhslink 域名发请求，见到主站链接立刻返回，
    绝不抓取最终页面；中途跳到白名单外的域名直接拒绝。"""
    url = raw.strip()
    for _ in range(5):
        host = (urlparse(url).hostname or "").lower()
        if host in {"xiaohongshu.com", "www.xiaohongshu.com"}:
            return url
        if host == "m.xiaohongshu.com":
            return url.replace("//m.xiaohongshu.com", "//www.xiaohongshu.com", 1)
        if not (host == "xhslink.com" or host.endswith(".xhslink.com")):
            raise ValueError(f"短链跳转到了白名单外的域名：{host or '(空)'}")
        request = urllib.request.Request(
            url, headers={"User-Agent": _SHORT_LINK_UA}, method="GET")
        location = None
        try:
            with _SHORT_LINK_OPENER.open(request, timeout=8) as resp:
                location = resp.headers.get("Location")
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400 and exc.headers:
                location = exc.headers.get("Location")
            else:
                raise ValueError(f"展开短链失败：HTTP {exc.code}") from exc
        except Exception as exc:
            raise ValueError(f"展开短链失败：{exc}") from exc
        if not location:
            raise ValueError("短链没有返回跳转目标，可能已失效")
        url = urljoin(url, location)
    raise ValueError("短链跳转次数过多")


async def _resolve_short_link(raw):
    """短链在线程池里展开，不卡 event loop；非短链原样返回。"""
    if _is_short_link(raw):
        return await asyncio.to_thread(_expand_short_link_sync, raw)
    return raw


def _note_id_from_url(url):
    path = urlparse(url).path.rstrip("/")
    candidates = re.findall(r"(?<![0-9a-f])([0-9a-f]{24})(?![0-9a-f])", path, re.I)
    return candidates[-1] if candidates else None


async def get_context():
    """连接或重连常驻 Chrome；不清理、不关闭任何既有人工标签页。"""
    global _pw, _browser
    async with _CONN_LOCK:
        if _browser is not None and _browser.is_connected():
            if not _browser.contexts:
                raise RuntimeError("CDP 已连接但没有浏览器 context")
            return _browser.contexts[0]

        _browser = None
        if _pw is not None:
            with suppress(Exception):
                await asyncio.wait_for(_pw.stop(), timeout=5)
            _pw = None

        try:
            _pw = await async_playwright().start()
            _browser = await asyncio.wait_for(
                _pw.chromium.connect_over_cdp(CDP_URL), timeout=12
            )
        except Exception as exc:
            _browser = None
            if _pw is not None:
                with suppress(Exception):
                    await asyncio.wait_for(_pw.stop(), timeout=5)
                _pw = None
            raise RuntimeError(f"连接中继浏览器失败：{exc}") from exc

        if not _browser.contexts:
            raise RuntimeError("CDP 已连接但没有浏览器 context")
        return _browser.contexts[0]


async def new_work_page(context):
    """创建自己的后台页；调用方必须在 finally 中只关闭这一页。"""
    page = None
    async with _PAGE_LOCK:
        session = None
        try:
            session = await _browser.new_browser_cdp_session()
            async with context.expect_page(timeout=5000) as page_info:
                await session.send(
                    "Target.createTarget", {"url": "about:blank", "background": True}
                )
            page = await page_info.value
        except Exception:
            page = await asyncio.wait_for(context.new_page(), timeout=10)
        finally:
            if session is not None:
                with suppress(Exception):
                    await session.detach()

    await page.set_viewport_size({"width": 1100, "height": 1300})
    page.set_default_timeout(18000)

    async def block_heavy(route):
        # 保留图片请求：详情页的图片地址和轮播状态依赖图片组件初始化。
        if route.request.resource_type in ("media", "font"):
            await route.abort()
        else:
            await route.continue_()

    await page.route("**/*", block_heavy)
    return page


def _browse_session_alive(session):
    if not session or time.time() - session["last_active_at"] > BROWSE_TTL_SECONDS:
        return False
    page = session.get("page")
    return bool(page and not page.is_closed())


async def _close_browse_session(reason):
    """只关闭 wrapper 自己为最近列表保留的后台页。"""
    global _browse_session
    session, _browse_session = _browse_session, None
    if not session:
        return
    page = session.get("page")
    if page is not None and not page.is_closed():
        with suppress(Exception):
            await asyncio.wait_for(page.close(), timeout=5)


def _install_browse_session(page, items, source):
    global _browse_session
    ids = {item.get("id") for item in items if item.get("id")}
    _browse_session = {
        "page": page,
        "ids": ids,
        "source": source,
        "list_url": page.url,
        "last_active_at": time.time(),
    }


async def _find_note_cover(page, note_id):
    """用真实滚轮寻找对应卡片，返回可点击的封面链接。"""
    for step in range(12):
        cards = page.locator(f'section.note-item[data-note-id="{note_id}"]')
        for index in range(await cards.count()):
            cover = cards.nth(index).locator("a.cover[href]").first
            if await cover.count() and await cover.is_visible():
                await cover.scroll_into_view_if_needed()
                return cover
        if step == 0:
            await page.keyboard.press("Home")
        else:
            await page.mouse.wheel(0, random.randint(750, 1150))
        await page.wait_for_timeout(random.randint(220, 520))
        await _raise_if_challenge(page)
    return None


async def _xhs_detail_visible(page):
    selectors = (
        "#noteContainer:visible, .note-detail-mask:visible, "
        ".note-detail:visible, #detail-title:visible, #detail-desc:visible"
    )
    return await page.locator(selectors).count() > 0


async def _restore_xhs_list(page):
    """优先按 Escape 关闭详情浮层；若是整页路由，再模拟浏览器后退。"""
    try:
        await page.keyboard.press("Escape")
        for _ in range(10):
            if (await page.locator("section.note-item[data-note-id]:visible").count()
                    and not await _xhs_detail_visible(page)):
                return True
            await page.wait_for_timeout(250)
        await page.keyboard.press("Alt+ArrowLeft")
        for _ in range(10):
            if (await page.locator("section.note-item[data-note-id]:visible").count()
                    and not await _xhs_detail_visible(page)):
                return True
            await page.wait_for_timeout(250)
        try:
            await page.go_back(wait_until="commit", timeout=12000)
        except PWTimeout:
            pass
        await page.wait_for_selector(
            "section.note-item[data-note-id]:visible", timeout=12000
        )
        return not await _xhs_detail_visible(page)
    except Exception:
        return False


async def _login_error(page):
    """仅在内容没加载时判断登录态，避免把页面上的普通“登录”字样当掉线。"""
    if "/login" in page.url:
        return "登录态失效：请在中继浏览器（browser-relay）里重新人工登录小红书"
    login_visible = await page.evaluate(
        """() => Boolean(
          document.querySelector('.login-container, .login-modal, [class*="login-modal"]')
        )"""
    )
    if login_visible:
        return "登录态失效：请在中继浏览器（browser-relay）里重新人工登录小红书"
    return None


CARD_EXTRACT_JS = r"""
() => {
  const absolute = (href) => {
    try { return href ? new URL(href, location.origin).href : null; }
    catch (_) { return null; }
  };
  const idFromHref = (href) => {
    const path = (() => { try { return new URL(href, location.origin).pathname; } catch (_) { return ''; } })();
    const matches = path.match(/[0-9a-f]{24}/ig) || [];
    return matches.length ? matches[matches.length - 1] : null;
  };
  const out = [];
  for (const card of document.querySelectorAll('section.note-item[data-note-id]')) {
    const coverLink = card.querySelector('a.cover[href]');
    if (!coverLink) continue;
    const href = coverLink.getAttribute('href') || '';
    const id = idFromHref(href) || card.getAttribute('data-note-id');
    if (!id || !/^[0-9a-f]{24}$/i.test(id)) continue;

    const authorLink = card.querySelector('a.author[href*="/user/profile/"]');
    const titleEl = card.querySelector('a.title span, .title span, .title');
    const cover = card.querySelector('a.cover img, img');
    const avatar = card.querySelector('.author img, img.avatar, .author-wrapper img');
    const authorName = card.querySelector('.author .name, .name-time-wrapper .name, a.author .name, a.author span');
    const timeEl = card.querySelector('.name-time-wrapper .time, .time');
    const likeEl = card.querySelector('.like-wrapper .count, .like-wrapper');

    out.push({
      id,
      url: absolute(href),
      title: titleEl ? titleEl.textContent.trim() : '',
      cover: cover ? (cover.currentSrc || cover.src || cover.getAttribute('data-src')) : null,
      type: card.querySelector('.play-icon, [class*="play-icon"]') ? 'video' : 'normal',
      author: authorName ? authorName.textContent.trim() : '',
      author_id: authorLink ? idFromHref(authorLink.getAttribute('href') || '') : null,
      author_url: authorLink ? absolute(authorLink.getAttribute('href')) : null,
      avatar: avatar ? (avatar.currentSrc || avatar.src || avatar.getAttribute('data-src')) : null,
      likes: likeEl ? likeEl.textContent.trim() : null,
      date: timeEl ? timeEl.textContent.trim() : null,
    });
  }
  return out;
}
"""


async def _wait_for_cards(page):
    await _raise_if_challenge(page)
    try:
        await page.wait_for_selector("section.note-item[data-note-id]", timeout=18000)
        await _raise_if_challenge(page)
        return None
    except PWTimeout:
        await _raise_if_challenge(page)
        login = await _login_error(page)
        if login:
            return login
        empty = await page.evaluate(
            """() => Boolean(document.querySelector(
              '.empty, .empty-container, [class*="no-result"], [class*="empty"]'
            ))"""
        )
        if empty:
            return "EMPTY"
        return "等待笔记列表超时：可能是网络慢或小红书页面结构已变化"


async def _collect_cards(page, n):
    seen = set()
    result = []
    for _ in range(8):
        await _raise_if_challenge(page)
        batch = await page.evaluate(CARD_EXTRACT_JS)
        for item in batch:
            key = item.get("id")
            if key and key not in seen:
                seen.add(key)
                result.append(item)
        if len(result) >= n:
            break
        before_ids = [item.get("id") for item in batch if item.get("id")]
        await page.mouse.wheel(0, random.randint(850, 1250))
        try:
            await page.wait_for_function(
                """(before) => [...document.querySelectorAll(
                  'section.note-item[data-note-id]'
                )].some(card => !before.includes(card.dataset.noteId))""",
                arg=before_ids,
                timeout=5000,
            )
        except PWTimeout:
            await _raise_if_challenge(page)
            await page.wait_for_timeout(random.randint(260, 680))
    return result[:n]


async def _goto(page, url):
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except PWTimeout as exc:
        await _raise_if_challenge(page)
        raise RuntimeError("打开小红书页面超时") from exc
    await _raise_if_challenge(page)


async def action_feed(page, body):
    n = _bounded_int(body.get("n"), 10, MAX_ITEMS)
    await _goto(page, f"{BASE}/explore")
    error = await _wait_for_cards(page)
    if error == "EMPTY":
        return {"ok": True, "action": "feed", "items": [], "count": 0}
    if error:
        return {"ok": False, "error": error}
    items = await _collect_cards(page, n)
    return {"ok": True, "action": "feed", "items": items, "count": len(items)}


async def action_search(page, body):
    query = body.get("query")
    if not isinstance(query, str) or not query.strip():
        return {"ok": False, "error": "search 需要非空 query"}
    query = query.strip()
    if len(query) > 100:
        return {"ok": False, "error": "query 最长 100 个字符"}
    n = _bounded_int(body.get("n"), 10, MAX_ITEMS)
    await _goto(page, f"{BASE}/explore")
    search_input = page.locator('textarea#search-input-in-feeds:visible').first
    try:
        await search_input.wait_for(timeout=10000)
    except PWTimeout:
        # 旧版布局没有 feeds 专用 id，退回最后一个可见搜索编辑器。
        search_input = page.locator(
            'textarea[name="aiSearchTextarea"]:visible, '
            'input[placeholder*="搜索"]:visible'
        ).last
        await search_input.wait_for(timeout=5000)
    await _guarded_click(page, search_input)
    await _human_type(page, query)
    await page.keyboard.press("Enter")
    try:
        await page.wait_for_url(
            re.compile(r"/search_result(?:_ai)?(?:\?|$)"), timeout=18000
        )
    except PWTimeout:
        await _raise_if_challenge(page)
        raise RuntimeError("点击搜索后没有进入结果页，小红书搜索入口可能已变化")
    await _raise_if_challenge(page)
    error = await _wait_for_cards(page)
    if error == "EMPTY":
        return {
            "ok": True,
            "action": "search",
            "query": query,
            "items": [],
            "count": 0,
        }
    if error:
        return {"ok": False, "error": error}
    items = await _collect_cards(page, n)
    return {
        "ok": True,
        "action": "search",
        "query": query,
        "items": items,
        "count": len(items),
    }


PROFILE_EXTRACT_JS = r"""
() => {
  const text = (selector) => document.querySelector(selector)?.textContent?.trim() || '';
  const avatar = document.querySelector('.user-info img.user-image, .user-info .user-image img, .user-image');
  const stats = {};
  for (const item of document.querySelectorAll('.user-interactions > div, .user-interactions .interaction-item')) {
    const label = item.querySelector('.shows')?.textContent?.trim();
    const value = item.querySelector('.count')?.textContent?.trim();
    if (label) stats[label] = value || '';
  }
  return {
    nickname: text('.user-info .user-name, .user-name'),
    red_id: text('.user-info .user-redId, .user-redId').replace(/^小红书号[:：]?\s*/, ''),
    ip_location: text('.user-info .user-IP, .user-IP'),
    description: text('.user-info .user-desc, .user-desc'),
    tags: [...document.querySelectorAll('.user-tags .tag-item')]
      .map(el => el.textContent.trim()).filter(Boolean),
    avatar: avatar ? (avatar.currentSrc || avatar.src || avatar.getAttribute('data-src')) : null,
    stats,
  };
}
"""


async def action_profile(page, body):
    raw = body.get("url") or body.get("user")
    if isinstance(raw, str) and re.fullmatch(r"[0-9a-f]{24}", raw.strip(), re.I):
        raw = f"{BASE}/user/profile/{raw.strip()}"
    try:
        raw = await _resolve_short_link(raw)
        url = _safe_xhs_url(raw, "profile")
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    n = _bounded_int(body.get("n"), 10, MAX_ITEMS)
    await _goto(page, url)
    try:
        await page.wait_for_selector(".user-info, #userPageContainer", timeout=18000)
        await _raise_if_challenge(page)
    except PWTimeout:
        await _raise_if_challenge(page)
        login = await _login_error(page)
        return {
            "ok": False,
            "error": login or "等待用户主页超时：URL 可能失效或页面结构已变化",
        }

    profile = await page.evaluate(PROFILE_EXTRACT_JS)
    error = await _wait_for_cards(page)
    notes = [] if error else await _collect_cards(page, n)
    if error and error != "EMPTY":
        # 主页本身已加载；无笔记时仍保留用户资料，并明确列表状态。
        profile["notes_warning"] = error
    profile["url"] = page.url
    match = re.search(r"/user/profile/([0-9a-f]{24})", page.url, re.I)
    profile["user_id"] = match.group(1) if match else None
    return {
        "ok": True,
        "action": "profile",
        "profile": profile,
        "items": notes,
        "count": len(notes),
    }


DOM_DETAIL_EXTRACT_JS = r"""
(noteId) => {
  const visible = (element) => {
    if (!element) return false;
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden'
      && Number(style.opacity || 1) !== 0 && rect.width > 0 && rect.height > 0;
  };
  const roots = [...document.querySelectorAll(
    '#noteContainer, .note-detail-mask, .note-detail, .note-content'
  )].filter(visible);
  const root = roots.reverse().find(element => element.querySelector(
    '#detail-title, #detail-desc, .author-wrapper, .author-container'
  )) || document;
  const query = (selector) => root.querySelector(selector);
  const queryAll = (selector) => root.querySelectorAll(selector);
  const text = (selector) => query(selector)?.textContent?.trim() || '';
  const absolute = (value) => {
    try { return value ? new URL(value, location.origin).href : null; }
    catch (_) { return null; }
  };
  const userLink = query(
    '.author-wrapper a[href*="/user/profile/"], .author-container a[href*="/user/profile/"]'
  );
  const userHref = userLink?.getAttribute('href') || null;
  const userId = userHref?.match(/\/user\/profile\/([0-9a-f]{24})/i)?.[1] || null;
  const avatar = query(
    '.author-wrapper img, .author-container img, .user-info img'
  );

  const imageNodes = [...queryAll(
    '.swiper-slide img, .note-slider img, .slider-container img, '
    + '.media-container img, [class*="carousel"] img'
  )];
  const seenImages = new Set();
  const images = [];
  for (const node of imageNodes) {
    const url = node.currentSrc || node.src || node.getAttribute('data-src');
    if (!url || seenImages.has(url)) continue;
    seenImages.add(url);
    images.push({
      index: images.length,
      url,
      width: node.naturalWidth || null,
      height: node.naturalHeight || null,
      live_photo: false,
    });
  }

  const tags = [...queryAll(
    '#detail-desc a[href*="search_result"], .note-text a[href*="search_result"]'
  )].map(link => ({
    id: null,
    name: link.textContent.trim().replace(/^#/, ''),
    type: null,
  })).filter(tag => tag.name);

  const count = (selector) => {
    const value = text(selector);
    return value || null;
  };
  const title = text('#detail-title, .note-content .title, .note-detail .title');
  const description = text('#detail-desc, .note-content .desc, .note-detail .desc');
  const hasVideo = Boolean(query('video, .player-container'));
  const rootVisible = root !== document || Boolean(query('.note-content'));

  return {
    found: Boolean(rootVisible && (title || description || images.length || hasVideo)),
    note: {
      id: noteId || null,
      type: hasVideo ? 'video' : (images.length ? 'normal' : null),
      title,
      description,
      author: userLink ? {
        user_id: userId,
        nickname: userLink.textContent.trim(),
        avatar: avatar ? (avatar.currentSrc || avatar.src) : null,
        url: absolute(userHref),
      } : null,
      created_at: text('.date, .publish-time, [class*="publish-time"]') || null,
      updated_at: null,
      ip_location: text('.location, .ip-location, [class*="ip-location"]') || null,
      tags,
      images,
      stats: {
        likes: count('.like-wrapper .count, [data-testid="like"] .count'),
        collected: count('.collect-wrapper .count, [data-testid="collect"] .count'),
        comments: count('.chat-wrapper .count, .comment-wrapper .count'),
        shares: count('.share-wrapper .count'),
      },
      viewer_state: {
        liked: Boolean(query('.like-wrapper.active, .like-wrapper.liked')),
        collected: Boolean(query('.collect-wrapper.active, .collect-wrapper.collected')),
        followed_author: Boolean(query('.follow-button.followed, .followed')),
      },
    },
    comments: [],
    comments_cursor: null,
    comments_has_more: false,
  };
}
"""


STORE_DETAIL_EXTRACT_JS = r"""
(noteId) => {
  const unwrap = (value) => {
    if (!value || typeof value !== 'object') return value;
    if ('_value' in value) return value._value;
    if ('_rawValue' in value) return value._rawValue;
    return value;
  };
  const root = window.__INITIAL_STATE__ || {};
  const map = unwrap(root.note?.noteDetailMap) || {};
  const entry = map[noteId] || Object.values(map)[0] || {};
  const note = unwrap(entry.note) || {};
  const commentsState = unwrap(entry.comments) || {};

  const imageUrl = (image) => image?.urlDefault || image?.url || image?.urlPre ||
    image?.infoList?.find?.(x => x?.url)?.url || null;
  const mapUser = (user) => user ? {
    user_id: user.userId || user.userid || null,
    nickname: user.nickname || user.nickName || '',
    avatar: user.avatar || null,
    url: user.userId ? `${location.origin}/user/profile/${user.userId}${user.xsecToken ? `?xsec_token=${encodeURIComponent(user.xsecToken)}&xsec_source=pc_note` : ''}` : null,
  } : null;
  const mapComment = (comment) => ({
    id: comment.id || null,
    content: comment.content || '',
    created_at: comment.createTime || null,
    ip_location: comment.ipLocation || null,
    likes: comment.likeCount ?? null,
    liked_by_me: Boolean(comment.liked),
    user: mapUser(comment.userInfo),
    pictures: (comment.pictures || []).map(imageUrl).filter(Boolean),
    sub_comment_count: comment.subCommentCount ?? 0,
    sub_comments: (comment.subComments || []).map(sub => ({
      id: sub.id || null,
      content: sub.content || '',
      created_at: sub.createTime || null,
      ip_location: sub.ipLocation || null,
      likes: sub.likeCount ?? null,
      liked_by_me: Boolean(sub.liked),
      user: mapUser(sub.userInfo),
      pictures: (sub.pictures || []).map(imageUrl).filter(Boolean),
    })),
  });

  const domText = (selector) => document.querySelector(selector)?.textContent?.trim() || '';
  const title = note.title ?? domText('#detail-title');
  const description = note.desc ?? domText('#detail-desc');
  const images = (note.imageList || []).map((image, index) => ({
    index,
    url: imageUrl(image),
    width: image.width ?? null,
    height: image.height ?? null,
    live_photo: Boolean(image.livePhoto),
  })).filter(image => image.url);
  const tags = (note.tagList || []).map(tag => ({
    id: tag.id || null,
    name: tag.name || '',
    type: tag.type || null,
  })).filter(tag => tag.name);
  const interact = note.interactInfo || {};
  const comments = (commentsState.list || []).map(mapComment);

  return {
    found: Boolean(note.noteId || title || description),
    note: {
      id: note.noteId || noteId || null,
      type: note.type || (images.length ? 'normal' : null),
      title,
      description,
      author: mapUser(note.user),
      created_at: note.time ?? null,
      updated_at: note.lastUpdateTime ?? null,
      ip_location: note.ipLocation || null,
      tags,
      images,
      stats: {
        likes: interact.likedCount ?? interact.niceCount ?? null,
        collected: interact.collectedCount ?? null,
        comments: interact.commentCount ?? null,
        shares: interact.shareCount ?? null,
      },
      viewer_state: {
        liked: Boolean(interact.liked),
        collected: Boolean(interact.collected),
        followed_author: Boolean(interact.followed),
      },
    },
    comments,
    comments_cursor: commentsState.cursor || null,
    comments_has_more: Boolean(commentsState.hasMore),
  };
}
"""


DOM_COMMENT_EXTRACT_JS = r"""
() => {
  const visible = (element) => {
    if (!element) return false;
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden'
      && Number(style.opacity || 1) !== 0 && rect.width > 0 && rect.height > 0;
  };
  const roots = [...document.querySelectorAll(
    '#noteContainer, .note-detail-mask, .note-detail, .note-content'
  )].filter(visible);
  const root = roots.reverse().find(element => element.querySelector('.comment-item')) || document;
  return [...root.querySelectorAll('.comment-item:not(.comment-item-sub)')].map(item => {
  const text = (selector) => item.querySelector(selector)?.textContent?.trim() || '';
  const userLink = item.querySelector('.author-wrapper .author a.name[href], .author a.name[href]');
  const href = userLink?.href || null;
  const userId = userLink?.dataset?.userId || href?.match(/\/user\/profile\/([0-9a-f]{24})/i)?.[1] || null;
  const image = item.querySelector('.comment-picture img');
  return {
    id: item.getAttribute('data-id') || item.id || null,
    content: text(':scope > .comment-inner-container .content .note-text, :scope > .content .note-text'),
    created_at: text(':scope > .comment-inner-container .info .date, :scope > .info .date'),
    ip_location: text(':scope > .comment-inner-container .info .location, :scope > .info .location'),
    likes: text(':scope > .comment-inner-container .interactions .like .count, :scope > .interactions .like .count') || null,
    user: {user_id: userId, nickname: userLink?.textContent?.trim() || '', url: href},
    pictures: image ? [image.currentSrc || image.src] : [],
    sub_comments: [],
  };
  });
}
"""


def _fill_missing(primary, fallback):
    """DOM 为主；只用页面状态补 DOM 未渲染出的字段。"""
    if isinstance(primary, dict) and isinstance(fallback, dict):
        merged = dict(primary)
        for key, value in fallback.items():
            if key in merged:
                merged[key] = _fill_missing(merged[key], value)
            else:
                merged[key] = value
        return merged
    if primary is None or primary == "" or primary == [] or primary == {}:
        return fallback
    return primary


def _dom_detail_needs_fallback(detail, comments):
    note = detail.get("note") or {}
    has_body = bool(note.get("title") or note.get("description"))
    has_media = bool(note.get("images") or note.get("type") == "video")
    return not detail.get("found") or not has_body or not has_media or not comments


async def action_read(page, body, from_list=False):
    try:
        raw = await _resolve_short_link(body.get("url"))
        url = _safe_xhs_url(raw, "note")
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    note_id = _note_id_from_url(url)
    if not note_id:
        return {"ok": False, "error": "URL 中没有可识别的 24 位笔记 ID"}

    n = _bounded_int(body.get("n"), 10, MAX_COMMENTS)
    opened_from_list = False
    if from_list:
        cover = await _find_note_cover(page, note_id)
        if cover is None:
            await _close_browse_session("card_not_found")
            return {
                "ok": False,
                "error": "命中刚才的 feed，但在渲染列表里找不到对应笔记卡片；为避免硬跳 URL，未自动降级",
                "navigation": "card_click_failed",
            }
        await _guarded_click(page, cover)
        opened_from_list = True
    else:
        await _goto(page, url)

    restored = False
    try:
        try:
            await page.wait_for_function(
                """(id) => Boolean(
                  location.pathname.includes(id) && (
                    [...document.querySelectorAll('#detail-title, #detail-desc')].some(el => {
                      const style = getComputedStyle(el);
                      const rect = el.getBoundingClientRect();
                      return style.display !== 'none' && style.visibility !== 'hidden'
                        && rect.width > 0 && rect.height > 0;
                    }) || window.__INITIAL_STATE__?.note?.noteDetailMap?.[id]
                  )
                )""",
                arg=note_id,
                timeout=20000,
            )
            await _raise_if_challenge(page)
        except PWTimeout:
            await _raise_if_challenge(page)
            login = await _login_error(page)
            result = {
                "ok": False,
                "error": login or "等待笔记详情超时：链接可能缺少有效 xsec_token，或页面结构已变化",
            }
        else:
            # 先读已经渲染出来的 DOM；只有关键字段或评论缺失时才碰页面内部状态。
            detail = await page.evaluate(DOM_DETAIL_EXTRACT_JS, note_id)
            comments = await page.evaluate(DOM_COMMENT_EXTRACT_JS)
            extraction_source = "dom"
            if _dom_detail_needs_fallback(detail, comments):
                fallback = await page.evaluate(STORE_DETAIL_EXTRACT_JS, note_id)
                detail = _fill_missing(detail, fallback)
                detail["found"] = bool(detail.get("found") or fallback.get("found"))
                if not comments:
                    comments = fallback.get("comments") or []
                extraction_source = "dom+store_fallback"

            if not detail.get("found"):
                result = {"ok": False, "error": "笔记详情没有加载出来，链接可能已失效或无权限"}
            else:
                detail["comments"] = comments[:n]
                detail["extraction_source"] = extraction_source
                detail["source_url"] = page.url
                detail["action"] = "read"
                detail["ok"] = True
                detail.pop("found", None)
                result = detail
        result["navigation"] = (
            "feed_card_click" if from_list else "direct_url_fallback"
        )
    finally:
        if opened_from_list:
            restored = await _restore_xhs_list(page)
            if (_browse_session and _browse_session.get("page") is page
                    and restored):
                _browse_session["last_active_at"] = time.time()
            elif _browse_session and _browse_session.get("page") is page:
                await _close_browse_session("list_restore_failed")

    if opened_from_list and not restored:
        result["browse_session_warning"] = "详情已读取，但返回 feed 失败；下一次 read 将走外部链接兜底"
    return result


ACTIONS = {
    "feed": action_feed,
    "search": action_search,
    "read": action_read,
    "profile": action_profile,
}


@app.get("/health")
async def health():
    try:
        context = await get_context()
        return {
            "ok": True,
            "connected": bool(_browser and _browser.is_connected()),
            "contexts": len(_browser.contexts),
            "existing_tabs": len(context.pages),
            "read_only_actions": sorted(ACTIONS),
            "browse_session": bool(_browse_session_alive(_browse_session)),
            "browse_ttl_seconds": BROWSE_TTL_SECONDS,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/xiaohongshu")
async def xiaohongshu(request: Request):
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "error": "请求体必须是 JSON"}
    if not isinstance(body, dict):
        return {"ok": False, "error": "请求体必须是 JSON object"}

    action = body.get("action")
    if action not in ACTIONS:
        return {
            "ok": False,
            "error": "不支持的 action；只读动作只有 feed、search、read、profile",
            "allowed_actions": sorted(ACTIONS),
        }

    async with ACTION_LOCK:
        page = None
        keep_page = False
        try:
            if _browse_session and not _browse_session_alive(_browse_session):
                await _close_browse_session("expired_before_action")
            if action in {"feed", "search", "profile"}:
                await _close_browse_session("replaced_by_list_action")

            context = await get_context()
            raw_read_url = body.get("url")
            note_id = (
                _note_id_from_url(raw_read_url)
                if action == "read" and isinstance(raw_read_url, str)
                else None
            )
            use_cached_list = bool(
                action == "read"
                and _browse_session_alive(_browse_session)
                and note_id in _browse_session["ids"]
            )
            if use_cached_list:
                page = _browse_session["page"]
                keep_page = True
                result = await action_read(page, body, from_list=True)
            else:
                page = await new_work_page(context)
                result = await ACTIONS[action](page, body)

            if action in {"feed", "search", "profile"} and result.get("ok"):
                items = result.get("items") or []
                if items:
                    _install_browse_session(page, items, action)
                    keep_page = True
                    result["read_navigation"] = "card_click_available"
            return result
        except asyncio.CancelledError:
            raise
        except ChallengeDetected as exc:
            if _browse_session and _browse_session.get("page") is page:
                await _close_browse_session("challenge")
                keep_page = False
            return exc.result
        except Exception as exc:
            return {"ok": False, "error": f"{action} 失败：{exc}"}
        finally:
            if page is not None and not keep_page and not page.is_closed():
                with suppress(Exception):
                    await asyncio.wait_for(page.close(), timeout=5)


async def _session_sweeper():
    while True:
        await asyncio.sleep(30)
        async with ACTION_LOCK:
            if _browse_session and not _browse_session_alive(_browse_session):
                await _close_browse_session("expired")


@app.on_event("startup")
async def startup():
    global _sweeper_task
    _sweeper_task = asyncio.create_task(_session_sweeper())


@app.on_event("shutdown")
async def shutdown():
    """只断开 Playwright 控制连接，绝不关闭常驻 Chrome。"""
    global _pw, _browser, _sweeper_task
    if _sweeper_task is not None:
        _sweeper_task.cancel()
        with suppress(asyncio.CancelledError):
            await _sweeper_task
        _sweeper_task = None
    await _close_browse_session("service_shutdown")
    _browser = None
    if _pw is not None:
        with suppress(Exception):
            await asyncio.wait_for(_pw.stop(), timeout=5)
        _pw = None
