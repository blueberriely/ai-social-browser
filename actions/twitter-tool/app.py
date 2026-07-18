"""twitter-tool: 把刷推特包装成几个具体动作的 HTTP 服务。

连接 browser-relay 的常驻 Chrome (CDP 9333)，复用已登录的默认 context。
调用方：任何会发 HTTP 的 agent 或脚本。

POST /twitter  body: {"action": "...", ...}
  feed    [query?, n?, latest?]  不带 query=首页时间线，带 query=搜索
  profile [user, n?]             某人最近推文
  read    [url, n?]              单条推文 + 评论
  like    [url] / unlike [url]
  repost  [url]
  reply   [url, text]
  post    [text]

并发：读写共享总上限 2（标签页池），写操作另有全局串行锁 + 频率闸。
HTTP 强制 Bearer 鉴权；写操作默认关闭，并校验登录账号、目标作者和幂等键。
所有写操作记录到 actions.log。
"""

import asyncio
import contextvars
import hashlib
import hmac
import json
import math
import os
import random
import re
import time
import unicodedata
from collections import deque
from contextlib import suppress
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

CDP_URL = os.environ.get("TW_CDP_URL", "http://127.0.0.1:9333").rstrip("/")
BASE = "https://x.com"
LOG_PATH = Path(__file__).parent / "actions.log"
STATE_PATH = Path(os.environ.get(
    "TW_STATE_PATH", str(Path(__file__).parent / "runtime-state.json")))
SERVICE_NAME = "ai-social-browser-twitter"
SERVICE_VERSION = "2"
API_VERSION = 2
WRITE_TIMEOUT_SECONDS = 150
MIN_SERVICE_TOKEN_BYTES = 32
REJECTED_SERVICE_TOKENS = {"replace-with-a-long-random-secret"}
IDEMPOTENCY_TTL_SECONDS = max(
    3600, int(float(os.environ.get("TW_IDEMPOTENCY_TTL_HOURS", "168")) * 3600))

# 频率闸：滑动窗口 1 小时
RATE_LIMITS = {
    "publish": 5,    # reply + post 合计
    "engage": 20,    # like + unlike + repost 合计
}
RATE_GROUP = {"reply": "publish", "post": "publish",
              "like": "engage", "unlike": "engage", "repost": "engage"}
_rate_windows = {g: deque() for g in RATE_LIMITS}  # epoch seconds，需跨重启保留
_idempotency = {}  # key -> {fingerprint,status,result,created_at,updated_at}

READ_SEM = asyncio.Semaphore(2)  # 读操作自身上限；读写还会共同占用 PAGE_CAPACITY
WRITE_LOCK = asyncio.Lock()
PAGE_CAPACITY = asyncio.Semaphore(2)  # 读写共用：工具自己的 x.com 工作页总数最多 2
BROWSE_TTL_SECONDS = max(30, int(os.environ.get("TW_BROWSE_TTL_SECONDS", "180")))

_pw = None
_browser = None
_conn_lock = asyncio.Lock()
_owned_targets = {}  # target_id -> {created_at,last_active_at}，落盘后可清理重启遗孤
_owned_pages = {}    # id(page) -> {page,target_id,closing}
_cleanup_tasks = set()
_state_loaded = False
_state_healthy = False
_state_health_error = "运行状态尚未加载"
_write_click_attempted = contextvars.ContextVar(
    "twitter_write_click_attempted", default=None)
_browse_session = None  # 最近一次列表页；feed/profile -> read 时复用并点击真实卡片
_browse_lock = asyncio.Lock()
_sweeper_task = None

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def _normalize_handle(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.startswith("@"):
        value = value[1:]
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", value):
        return None
    return value.lower()


def _guard_error(status_code, error):
    return JSONResponse(status_code=status_code, content={
        "ok": False,
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "api_version": API_VERSION,
        "error": error,
    })


def _request_guard(req, *, require_json=False):
    """本机端口也必须鉴权；Origin 只允许明确的本机 HTTP(S) 页面。"""
    token = os.environ.get("TW_SERVICE_TOKEN", "")
    try:
        token_bytes = token.encode("ascii")
    except (AttributeError, UnicodeError):
        token_bytes = None
    if (not token
            or token != token.strip()
            or token_bytes is None
            or len(token_bytes) < MIN_SERVICE_TOKEN_BYTES
            or token.lower() in REJECTED_SERVICE_TOKENS):
        return _guard_error(
            503,
            "TW_SERVICE_TOKEN 必须是至少 32 字节的 ASCII 随机密钥，且不能使用示例占位值")
    auth = req.headers.get("authorization", "")
    scheme, _, supplied = auth.partition(" ")
    try:
        supplied_bytes = supplied.strip().encode("ascii")
    except UnicodeError:
        supplied_bytes = b""
    if (scheme.lower() != "bearer" or not supplied_bytes
            or not hmac.compare_digest(supplied_bytes, token_bytes)):
        return _guard_error(401, "未授权")

    origin = req.headers.get("origin", "").strip()
    if origin:
        try:
            parsed = urlsplit(origin)
            local_origin = (
                parsed.scheme in ("http", "https")
                and parsed.hostname in ("localhost", "127.0.0.1")
                and not parsed.username
                and not parsed.password
                and parsed.path in ("", "/")
                and not parsed.query
                and not parsed.fragment
            )
        except Exception:
            local_origin = False
        if not local_origin:
            return _guard_error(403, "Origin 不是允许的本机来源")

    if require_json:
        media_type = req.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            return _guard_error(415, "Content-Type 必须是 application/json")
    return None


class ChallengeDetected(RuntimeError):
    """页面进入平台风控挑战；由路由层统一转成结构化结果。"""

    def __init__(self, result):
        super().__init__(result["hint"])
        self.result = result


TW_CHALLENGE_URL_MARKERS = (
    "arkoselabs", "/i/flow/challenge", "/account/access", "challenge_id=",
)
TW_CHALLENGE_SELECTORS = ", ".join((
    'iframe[src*="arkose" i]',
    'iframe[src*="arkoselabs" i]',
    'iframe[id*="arkose" i]',
    'iframe[data-testid*="arkose" i]',
    'input[name*="arkose" i]',
    '[data-testid="arkoseFrame"]',
))


def _challenge_result(url):
    return {
        "ok": False,
        "outcome": "challenge",
        "status": "challenge",
        "changed": False,
        "retry_safe": False,
        "manual_intervention_required": True,
        "platform": "twitter",
        "url": url,
        "hint": "检测到 X 人机验证，请用手机接管 browser-relay 完成验证后再试",
    }


async def _raise_if_challenge(page):
    """只识别强特征，识别不到时保持原有超时/失败路径。"""
    page_url = getattr(page, "url", "") or ""
    url = page_url.lower()
    if any(marker in url for marker in TW_CHALLENGE_URL_MARKERS):
        raise ChallengeDetected(_challenge_result(page_url))
    try:
        matches = page.locator(TW_CHALLENGE_SELECTORS)
        for index in range(min(await matches.count(), 4)):
            if await matches.nth(index).is_visible():
                raise ChallengeDetected(_challenge_result(page_url))
    except ChallengeDetected:
        raise
    except Exception:
        # 检测器本身不能盖掉旧路径。
        return


async def _guarded_click(page, locator, **kwargs):
    await _raise_if_challenge(page)
    await locator.click(**kwargs)
    await _raise_if_challenge(page)


async def _human_type(page, text):
    """逐字输入；节奏有抖动并偶尔停顿，不再固定 15ms。"""
    for char in text:
        delay = random.randint(20, 80)
        if char.isascii():
            await page.keyboard.type(char, delay=delay)
        else:
            await page.keyboard.insert_text(char)
            await asyncio.sleep(delay / 1000)
        if char in "，。！？,.!?\n" or random.random() < 0.06:
            await asyncio.sleep(random.uniform(0.12, 0.36))


def _now_cn():
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def log_action(entry: dict):
    entry["ts"] = _now_cn()
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _prune_idempotency(now=None):
    """只清理终态；执行中/不确定记录永久保留，直到人工核账。"""
    now = time.time() if now is None else now
    expired = []
    for key, rec in _idempotency.items():
        result = rec.get("result") if isinstance(rec, dict) else None
        if (isinstance(rec, dict)
                and (rec.get("status") == "in_progress"
                or (isinstance(result, dict)
                    and result.get("outcome") == "uncertain"))):
            continue
        try:
            updated_at = float(rec.get("updated_at") or rec.get("created_at") or 0)
        except (AttributeError, TypeError, ValueError):
            expired.append(key)
            continue
        if now - updated_at > IDEMPOTENCY_TTL_SECONDS:
            expired.append(key)
    for key in expired:
        _idempotency.pop(key, None)


def _fsync_parent_directory(path):
    """持久化目录项；Windows 不支持目录 fsync 时安全降级。"""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        if os.name == "nt":
            return
        raise
    try:
        try:
            os.fsync(fd)
        except OSError:
            if os.name != "nt":
                raise
    finally:
        os.close(fd)


def _persist_state():
    """频率、幂等和自建 target 原子落盘；绝不记录/接管用户手动标签页。"""
    global _state_healthy, _state_health_error
    if not _state_loaded:
        raise RuntimeError("运行状态尚未加载，拒绝覆盖状态文件")
    if not _state_healthy:
        raise RuntimeError(
            f"运行状态不健康，拒绝覆盖原文件：{_state_health_error}")
    _prune_idempotency()
    payload = {
        "rate_windows": {group: list(win) for group, win in _rate_windows.items()},
        "owned_targets": _owned_targets,
        "idempotency": _idempotency,
    }
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_name(f".{STATE_PATH.name}.{os.getpid()}.tmp")
        encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, STATE_PATH)
        _fsync_parent_directory(STATE_PATH.parent)
    except Exception as e:
        _state_healthy = False
        _state_health_error = f"状态落盘失败：{type(e).__name__}: {e}"
        raise
    _state_healthy = True
    _state_health_error = None


def _load_state():
    global _state_loaded, _state_healthy, _state_health_error
    if _state_loaded:
        return
    _state_loaded = True
    _state_healthy = False
    _state_health_error = "运行状态加载中"
    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _state_healthy = True
        _state_health_error = None
        return
    except Exception as e:
        _state_health_error = f"状态文件无法解析：{type(e).__name__}: {e}"
        log_action({"action": "_state_load_failed", "error": _state_health_error})
        return

    if not isinstance(payload, dict):
        _state_health_error = "runtime-state.json 顶层不是对象"
        log_action({"action": "_state_load_failed",
                    "error": _state_health_error})
        return

    now = time.time()
    try:
        raw_windows = payload.get("rate_windows", {})
        if not isinstance(raw_windows, dict):
            raise ValueError("rate_windows 不是对象")
        loaded_windows = {}
        for group in RATE_LIMITS:
            values = raw_windows.get(group, [])
            if not isinstance(values, list) or any(
                    not isinstance(ts, (int, float)) for ts in values):
                raise ValueError(f"rate_windows.{group} 格式损坏")
            numeric_values = [float(ts) for ts in values]
            if any(not math.isfinite(ts) or ts > now + 300 for ts in numeric_values):
                raise ValueError(f"rate_windows.{group} 含非法时间戳")
            loaded_windows[group] = deque(
                ts for ts in numeric_values if 0 <= now - ts <= 3600)

        raw_targets = payload.get("owned_targets", {})
        if not isinstance(raw_targets, dict):
            raise ValueError("owned_targets 不是对象")
        loaded_targets = {}
        for target_id, meta in raw_targets.items():
            if not isinstance(target_id, str) or not isinstance(meta, dict):
                raise ValueError("owned_targets 含非法记录")
            created_at = float(meta.get("created_at") or now)
            last_active_at = float(meta.get("last_active_at") or now)
            if not math.isfinite(created_at) or not math.isfinite(last_active_at):
                raise ValueError("owned_targets 含非法时间戳")
            loaded_targets[target_id] = {
                "created_at": created_at,
                "last_active_at": last_active_at,
            }

        raw_idempotency = payload.get("idempotency", {})
        if not isinstance(raw_idempotency, dict):
            raise ValueError("idempotency 不是对象")
        loaded_idempotency = {}
        for key, rec in raw_idempotency.items():
            if (not isinstance(key, str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:~-]{7,199}", key)
                    or not isinstance(rec, dict)):
                raise ValueError("idempotency 含非法记录")
            created_at = float(rec.get("created_at") or now)
            updated_at = float(rec.get("updated_at") or created_at)
            if not math.isfinite(created_at) or not math.isfinite(updated_at):
                raise ValueError(f"idempotency.{key} 含非法时间戳")
            status = rec.get("status")
            if status not in ("in_progress", "completed"):
                raise ValueError(f"idempotency.{key} 状态非法")
            result = rec.get("result")
            if status == "completed" and not isinstance(result, dict):
                raise ValueError(f"idempotency.{key} 完成记录缺少结果")
            if (status == "completed"
                    and result.get("outcome") not in ("confirmed", "failed", "uncertain")):
                raise ValueError(f"idempotency.{key} 结果状态非法")
            if (status == "completed"
                    and result.get("outcome") != "uncertain"
                    and now - updated_at > IDEMPOTENCY_TTL_SECONDS):
                continue
            fingerprint = rec.get("fingerprint")
            if not isinstance(fingerprint, str) or not fingerprint:
                raise ValueError(f"idempotency.{key} 缺少 fingerprint")
            loaded_idempotency[key] = {
                "fingerprint": fingerprint,
                "status": status,
                "result": result if isinstance(result, dict) else None,
                "created_at": created_at,
                "updated_at": updated_at,
            }
    except (TypeError, ValueError) as e:
        _state_health_error = f"状态文件结构损坏：{e}"
        log_action({"action": "_state_load_failed", "error": _state_health_error})
        return

    _rate_windows.update(loaded_windows)
    _owned_targets.clear()
    _owned_targets.update(loaded_targets)
    _idempotency.clear()
    _idempotency.update(loaded_idempotency)
    _state_healthy = True
    _state_health_error = None


def check_rate(action: str):
    group = RATE_GROUP.get(action)
    if not group:
        return None
    win = _rate_windows[group]
    now = time.time()
    changed = False
    while win and now - win[0] > 3600:
        win.popleft()
        changed = True
    if changed:
        _persist_state()
    if len(win) >= RATE_LIMITS[group]:
        wait_min = int((3600 - (now - win[0])) / 60) + 1
        return f"频率闸：{group} 组最近 1 小时已用满 {RATE_LIMITS[group]} 次，约 {wait_min} 分钟后再试"
    return None


def record_rate(action: str):
    group = RATE_GROUP.get(action)
    if group:
        _rate_windows[group].append(time.time())
        _persist_state()


def _close_owned_targets_sync(target_ids):
    """通过 CDP HTTP 只关明确登记为 twitter-tool 所有的 target。"""
    import urllib.request
    report = {"closed": [], "missing": [], "errors": []}
    wanted = set(target_ids)
    if not wanted:
        return report
    try:
        targets = json.load(urllib.request.urlopen(
            f"{CDP_URL}/json", timeout=5))
        pages = {t.get("id"): t for t in targets if t.get("type") == "page"}
        for target_id in wanted:
            if target_id not in pages:
                report["missing"].append(target_id)
                continue
            try:
                urllib.request.urlopen(
                    f"{CDP_URL}/json/close/{target_id}", timeout=5)
                report["closed"].append(target_id)
            except Exception as e:
                report["errors"].append({"target_id": target_id, "error": str(e)})
    except Exception as e:
        report["errors"].append({"error": str(e)})
    return report


async def _sweep_owned_orphans():
    """清理工具自建且静默超过 30 分钟的遗孤；不按 URL 猜归属。"""
    cutoff = time.time() - max(30.0, float(os.environ.get("TW_ORPHAN_MINUTES", "30"))) * 60
    stale = [
        target_id for target_id, meta in list(_owned_targets.items())
        if float(meta.get("last_active_at") or 0) <= cutoff
    ]
    if not stale:
        return []
    result = await asyncio.get_running_loop().run_in_executor(
        None, _close_owned_targets_sync, stale)
    resolved = set(result.get("closed", [])) | set(result.get("missing", []))
    for target_id in resolved:
        _owned_targets.pop(target_id, None)
    _persist_state()
    return result


async def get_context():
    """连接（或重连）常驻 Chrome，返回带登录态的默认 context。

    连接卡死（通常是僵尸标签页拖住接管）时自动清僵尸重连一次。
    """
    global _pw, _browser
    async with _conn_lock:
        if _browser is not None and _browser.is_connected():
            return _browser.contexts[0]
        for attempt in (1, 2):
            try:
                if _pw is None:
                    _pw = await async_playwright().start()
                _browser = await asyncio.wait_for(
                    _pw.chromium.connect_over_cdp(CDP_URL), timeout=12)
                if not _browser.contexts:
                    raise RuntimeError("CDP 连上了但没有 browser context")
                return _browser.contexts[0]
            except Exception:
                _browser = None
                if _pw is not None:
                    try:
                        await asyncio.wait_for(_pw.stop(), timeout=5)
                    except Exception:
                        pass
                    _pw = None
                if attempt == 2:
                    raise RuntimeError("连中继浏览器失败（清了僵尸标签页重试仍不行）")
                swept = await _sweep_owned_orphans()
                log_action({"action": "_selfheal", "swept": swept})


_PAGE_LOCK = asyncio.Lock()  # target 创建与 Page 精确匹配必须串行，避免并发扫描互相干扰


async def _page_target_id(ctx, page):
    """读取 Page 自己的 CDP target id；不能靠“下一个新页事件”猜归属。"""
    session = await ctx.new_cdp_session(page)
    try:
        info = await session.send("Target.getTargetInfo")
        return (info.get("targetInfo") or {}).get("targetId")
    finally:
        await session.detach()


async def _find_page_by_target_id(ctx, target_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for candidate in reversed(ctx.pages):
            try:
                if await _page_target_id(ctx, candidate) == target_id:
                    return candidate
            except Exception:
                continue
        await asyncio.sleep(0.05)
    raise RuntimeError(f"创建 target {target_id} 后找不到对应 Page，拒绝接管其他标签页")


async def _configure_page_resources(page, block_images):
    """切换工作页资源策略；列表页点进详情时需要重新放行图片。"""
    await page.unroute("**/*")
    blocked = ("image", "media", "font") if block_images else ("media", "font")

    async def _block_heavy(route):
        if route.request.resource_type in blocked:
            await route.abort()
        else:
            await route.continue_()

    await page.route("**/*", _block_heavy)


async def new_page(ctx, block_images=True):
    """开后台标签页干活，尽量不抢中继浏览器当前焦点。

    block_images=False 用于读单条推文详情：详情页图片请求被掐会导致
    img 元素不渲染、抓不到图片链接，所以看图的动作放行图片加载。"""
    await PAGE_CAPACITY.acquire()
    page = None
    target_id = None
    try:
        async with _PAGE_LOCK:
            cdp = await _browser.new_browser_cdp_session()
            try:
                created = await cdp.send(
                    "Target.createTarget",
                    {"url": "about:blank", "background": True})
                target_id = created.get("targetId")
            finally:
                await cdp.detach()

            if not target_id:
                raise RuntimeError("创建工作页成功但拿不到 target id，拒绝接管不明标签页")
            page = await _find_page_by_target_id(ctx, target_id)

        now = time.time()
        _owned_targets[target_id] = {"created_at": now, "last_active_at": now}
        _owned_pages[id(page)] = {
            "page": page, "target_id": target_id, "closing": False,
        }
        _persist_state()

        await page.set_viewport_size({"width": 1100, "height": 1300})
        page.set_default_timeout(15000)

        # 列表页默认省流；点进详情时会在同一页重新放行图片。
        await _configure_page_resources(page, block_images)
        return page
    except BaseException:
        if page is not None and id(page) in _owned_pages:
            try:
                await _close_owned_page(page)
            except asyncio.CancelledError:
                raise
        else:
            if target_id:
                fallback = await asyncio.get_running_loop().run_in_executor(
                    None, _close_owned_targets_sync, [target_id])
                resolved = target_id in fallback.get("closed", []) or target_id in fallback.get("missing", [])
                if resolved:
                    _owned_targets.pop(target_id, None)
                else:
                    now = time.time()
                    _owned_targets[target_id] = {
                        "created_at": now, "last_active_at": now,
                    }
                try:
                    _persist_state()
                finally:
                    PAGE_CAPACITY.release()
            else:
                PAGE_CAPACITY.release()
        raise


def _touch_owned_page(page):
    rec = _owned_pages.get(id(page))
    if not rec:
        return
    target_id = rec.get("target_id")
    if target_id in _owned_targets:
        _owned_targets[target_id]["last_active_at"] = time.time()
        _persist_state()


async def _finish_owned_page_close(page, rec):
    target_id = rec.get("target_id")
    cleanup_error = None
    resolved = False
    try:
        await asyncio.wait_for(page.close(), timeout=10)
        resolved = True
    except Exception as e:
        cleanup_error = f"page.close: {type(e).__name__}: {e}"
        if target_id:
            fallback = await asyncio.get_running_loop().run_in_executor(
                None, _close_owned_targets_sync, [target_id])
            cleanup_error += f"; cdp_fallback={fallback}"
            resolved = (target_id in fallback.get("closed", [])
                        or target_id in fallback.get("missing", []))
    finally:
        _owned_pages.pop(id(page), None)
        if target_id and resolved:
            _owned_targets.pop(target_id, None)
        persist_error = None
        try:
            _persist_state()
        except Exception as e:
            persist_error = e
        finally:
            PAGE_CAPACITY.release()
        if cleanup_error:
            log_action({
                "action": "_page_cleanup_failed",
                "target_id": target_id,
                "error": cleanup_error,
            })
        if persist_error:
            raise persist_error


async def _close_owned_page(page):
    """关闭失败只单独记日志，不允许覆盖动作本身的成功/失败结论。"""
    rec = _owned_pages.get(id(page))
    if not rec or rec.get("closing"):
        return
    rec["closing"] = True
    task = asyncio.create_task(_finish_owned_page_close(page, rec))
    _cleanup_tasks.add(task)
    task.add_done_callback(_cleanup_tasks.discard)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        # 外层 150s 超时可以返回；shield 内的 close 仍会跑完并释放容量。
        raise


def _browse_session_alive(session):
    if not session or time.time() - session["last_active_at"] > BROWSE_TTL_SECONDS:
        return False
    page = session.get("page")
    return bool(page and not page.is_closed() and id(page) in _owned_pages)


async def _drop_browse_session_locked(reason):
    """调用方须持有 _browse_lock；只关闭 wrapper 自己登记的列表页。"""
    global _browse_session
    session, _browse_session = _browse_session, None
    if not session:
        return
    page = session.get("page")
    if page is not None and not page.is_closed():
        await _close_owned_page(page)
    log_action({"action": "_browse_session_closed", "reason": reason})


async def _expire_browse_session():
    async with _browse_lock:
        if _browse_session and not _browse_session_alive(_browse_session):
            await _drop_browse_session_locked("expired")


def _install_browse_session(page, items, source):
    global _browse_session
    ids = {_status_id(item.get("url")) for item in items}
    _browse_session = {
        "page": page,
        "ids": {sid for sid in ids if sid},
        "source": source,
        "list_url": page.url,
        "last_active_at": time.time(),
    }
    _touch_owned_page(page)


async def _find_tweet_link(page, sid):
    """只点击外层推文自己的永久链接；引用卡片里的同号链接不算。"""
    for step in range(12):
        articles = page.locator('article[data-testid="tweet"]')
        for article_index in range(await articles.count()):
            article = articles.nth(article_index)
            try:
                own_href = await article.evaluate(OWN_STATUS_HREF_JS)
            except Exception:
                continue
            if _status_id(own_href) != sid:
                continue
            own_path = urlsplit(urljoin(BASE, own_href or "")).path.rstrip("/").lower()
            links = article.locator("a:has(time)")
            for link_index in range(await links.count()):
                link = links.nth(link_index)
                try:
                    href = await link.evaluate("a => a.href")
                    link_path = urlsplit(urljoin(BASE, href or "")).path.rstrip("/").lower()
                    visible = await link.is_visible()
                except Exception:
                    continue
                if link_path == own_path and visible:
                    await link.scroll_into_view_if_needed()
                    return link
        if step == 0:
            await page.keyboard.press("Home")
        else:
            await page.mouse.wheel(0, random.randint(900, 1450))
        await page.wait_for_timeout(random.randint(220, 520))
        await _raise_if_challenge(page)
    return None


async def _restore_twitter_list(page, sid):
    """详情读完后模拟浏览器后退，保留列表页供同一批下一次 read。"""
    try:
        await page.keyboard.press("Alt+ArrowLeft")
        try:
            await page.wait_for_function(
                "sid => !location.pathname.includes('/status/' + sid)",
                arg=sid,
                timeout=2500,
            )
        except PWTimeout:
            # 渲染进程里的按键不一定触发 Chrome 外壳快捷键；走浏览器历史，
            # 仍然不构造/跳转 URL。
            try:
                await page.go_back(wait_until="commit", timeout=12000)
            except PWTimeout:
                # SPA/bfcache 可能已回退却不发新的 load 事件，以实际页面状态为准。
                pass
            await page.wait_for_function(
                "sid => !location.pathname.includes('/status/' + sid)",
                arg=sid,
                timeout=12000,
            )
        return await wait_tweets(page) is None
    except ChallengeDetected:
        raise
    except Exception:
        return False


# ---------- 页面解析 ----------

OWN_STATUS_HREF_JS = r"""
article => {
  const articleSelector = 'article[data-testid="tweet"]';
  const nameSelector = 'div[data-testid="User-Name"]';
  const outerName = article.querySelector(nameSelector);
  if (!outerName || outerName.closest(articleSelector) !== article) return null;

  for (const timeEl of article.querySelectorAll('time')) {
    if (timeEl.closest(articleSelector) !== article) continue;
    const anchor = timeEl.closest('a');
    if (!anchor || anchor.closest(articleSelector) !== article) continue;

    const containingName = timeEl.closest(nameSelector);
    if (containingName && containingName !== outerName) continue;
    if (!containingName) {
      const wrappingLink = timeEl.parentElement
        ? timeEl.parentElement.closest('[role="link"]')
        : null;
      if (wrappingLink && wrappingLink !== anchor
          && wrappingLink.closest(articleSelector) === article) continue;
    }
    return anchor.href || null;
  }
  return null;
}
"""


EXTRACT_JS = r"""
() => {
  const articleSelector = 'article[data-testid="tweet"]';
  const nameSelector = 'div[data-testid="User-Name"]';
  const ownStatusTime = (article) => {
    const outerName = article.querySelector(nameSelector);
    if (!outerName || outerName.closest(articleSelector) !== article) return null;
    for (const timeEl of article.querySelectorAll('time')) {
      if (timeEl.closest(articleSelector) !== article) continue;
      const anchor = timeEl.closest('a');
      if (!anchor || anchor.closest(articleSelector) !== article) continue;
      const containingName = timeEl.closest(nameSelector);
      if (containingName && containingName !== outerName) continue;
      if (!containingName) {
        const wrappingLink = timeEl.parentElement
          ? timeEl.parentElement.closest('[role="link"]')
          : null;
        if (wrappingLink && wrappingLink !== anchor
            && wrappingLink.closest(articleSelector) === article) continue;
      }
      return timeEl;
    }
    return null;
  };
  const out = [];
  for (const art of document.querySelectorAll('article[data-testid="tweet"]')) {
    const t = {};
    const timeEl = ownStatusTime(art);
    const link = timeEl ? timeEl.closest('a') : null;
    t.url = link ? link.href : null;
    t.time = timeEl ? timeEl.getAttribute('datetime') : null;
    if (!timeEl) t.promoted = true;  // 广告卡片没有时间戳/永久链接
    const nameEl = art.querySelector('div[data-testid="User-Name"]');
    if (nameEl) {
      const lines = nameEl.innerText.split('\n').map(s => s.trim()).filter(Boolean);
      t.author = lines[0] || null;
      t.handle = lines.find(s => s.startsWith('@')) || null;
    }
    const textEl = art.querySelector('div[data-testid="tweetText"]');
    t.text = textEl ? textEl.innerText : '';
    if (art.querySelector('[data-testid="tweet-text-show-more-link"]'))
      t.truncated = true;
    const social = art.querySelector('[data-testid="socialContext"]');
    if (social) t.repost_context = social.innerText;
    const quotedTexts = art.querySelectorAll('div[data-testid="tweetText"]');
    if (quotedTexts.length > 1) t.quoted_text = quotedTexts[1].innerText;
    const stats = {};
    for (const [key, tid] of [['replies','reply'],['reposts','retweet'],['likes','like'],['likes','unlike']]) {
      const btn = art.querySelector(`button[data-testid="${tid}"]`);
      if (btn) {
        const label = btn.getAttribute('aria-label') || '';
        const m = label.match(/[\d,.]+\s*[万KM]?/);
        stats[key] = m ? m[0].trim() : (btn.innerText.trim() || '0');
      }
    }
    t.stats = stats;
    t.liked_by_me = !!art.querySelector('button[data-testid="unlike"]');
    t.reposted_by_me = !!art.querySelector('button[data-testid="unretweet"]');
    const imgs = [...art.querySelectorAll('img[src*="pbs.twimg.com/media"]')]
      .map(i => i.src.replace(/&name=\w+$/, ''));
    if (imgs.length) t.images = imgs;
    if (art.querySelector('[data-testid="videoPlayer"]')) t.has_video = true;
    if (art.querySelector('[data-testid="placementTracking"]')) t.promoted = true;
    out.push(t);
  }
  return out;
}
"""


async def check_logged_in(page):
    if "/login" in page.url or "/i/flow/login" in page.url:
        return False
    if await page.query_selector('[data-testid="loginButton"]'):
        return False
    return True


def _handles_in_text(value):
    return {
        handle.lower()
        for handle in re.findall(r"(?<![A-Za-z0-9_])@([A-Za-z0-9_]{1,15})\b", value or "")
    }


async def _read_current_actor(page):
    """从 X 自己的账号切换按钮和个人主页链接交叉读取当前登录账号。"""
    candidates = set()

    switcher = page.locator('button[data-testid="SideNav_AccountSwitcher_Button"]')
    if await switcher.count():
        try:
            candidates.update(_handles_in_text(await switcher.first.inner_text()))
        except Exception:
            pass

    profile = page.locator('a[data-testid="AppTabBar_Profile_Link"]')
    if await profile.count():
        try:
            href = await profile.first.get_attribute("href")
            path = urlsplit(href or "").path.strip("/")
            if "/" not in path:
                normalized = _normalize_handle(path)
                if normalized:
                    candidates.add(normalized)
        except Exception:
            pass

    if not candidates:
        return None, "页面上读不到当前登录账号 handle，拒绝写入"
    if len(candidates) != 1:
        return None, f"页面上的账号标识互相冲突：{sorted(candidates)}，拒绝写入"
    return next(iter(candidates)), None


async def wait_tweets(page):
    """等推文出现；区分「没登录」「空结果」「真超时」。"""
    await _raise_if_challenge(page)
    try:
        await page.wait_for_selector('article[data-testid="tweet"]', timeout=15000)
        await _raise_if_challenge(page)
        return None
    except PWTimeout:
        await _raise_if_challenge(page)
        if not await check_logged_in(page):
            return "登录态失效：中继浏览器里推特掉登录了，需要重新登录"
        if await page.query_selector('[data-testid="empty_state_header_text"]'):
            return "EMPTY"
        if await page.query_selector('[data-testid="error-detail"]'):
            return "EMPTY"
        return "等推文加载超时（页面开了但没刷出内容，可能是网络慢或页面结构变了）"


async def collect_tweets(page, n):
    seen, result = set(), []
    for _ in range(10):
        await _raise_if_challenge(page)
        batch = await page.evaluate(EXTRACT_JS)
        for t in batch:
            key = t.get("url") or t.get("text", "")[:80]
            if key and key not in seen:
                seen.add(key)
                result.append(t)
        if len(result) >= n:
            break
        before_urls = [t.get("url") for t in batch if t.get("url")]
        await page.mouse.wheel(0, random.randint(2200, 3000))
        try:
            await page.wait_for_function(
                """(before) => [...document.querySelectorAll(
                  'article[data-testid="tweet"] a[href*="/status/"]'
                )].some(a => a.href && !before.includes(a.href))""",
                arg=before_urls,
                timeout=5500,
            )
        except PWTimeout:
            await _raise_if_challenge(page)
            await page.wait_for_timeout(random.randint(280, 720))
    return result[:n]


async def goto(page, url):
    _touch_owned_page(page)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
    except PWTimeout:
        await _raise_if_challenge(page)
        raise
    _touch_owned_page(page)
    await _raise_if_challenge(page)


async def _open_search_via_ui(page, query, latest=False):
    """从首页点搜索入口并真实输入，不直接拼 /search URL。"""
    await goto(page, f"{BASE}/home")
    search_link = page.locator(
        '[data-testid="AppTabBar_Explore_Link"]:visible, a[href="/explore"]:visible'
    ).first
    await search_link.wait_for(timeout=12000)
    await _guarded_click(page, search_link)

    search_input = page.locator(
        'input[data-testid="SearchBox_Search_Input"]:visible'
    ).first
    await search_input.wait_for(timeout=12000)
    await _guarded_click(page, search_input)
    await _human_type(page, query)
    await page.keyboard.press("Enter")
    try:
        await page.wait_for_url(re.compile(r"/search(?:\?|$)"), timeout=15000)
    except PWTimeout:
        await _raise_if_challenge(page)
        raise
    await _raise_if_challenge(page)

    if latest:
        latest_tab = page.locator(
            'a[href*="/search"][href*="f=live"]:visible'
        ).first
        try:
            await latest_tab.wait_for(timeout=8000)
            await _guarded_click(page, latest_tab)
        except PWTimeout:
            # X 偶尔先用 tab 角色渲染，不带 href。
            latest_role = page.get_by_role(
                "tab", name=re.compile(r"^(Latest|最新)$", re.I)
            ).first
            await latest_role.wait_for(timeout=5000)
            await _guarded_click(page, latest_role)


# ---------- 读动作 ----------

async def act_feed(ctx, body):
    n = min(int(body.get("n", 10)), 30)
    query = (body.get("query") or "").strip()
    async with _browse_lock:
        await _drop_browse_session_locked("replaced_by_feed")
        page = await new_page(ctx)
        keep_page = False
        try:
            if query:
                await _open_search_via_ui(page, query, bool(body.get("latest")))
            else:
                await goto(page, f"{BASE}/home")
            err = await wait_tweets(page)
            if err == "EMPTY":
                return {"ok": True, "tweets": [], "note": "搜索无结果"}
            if err:
                return {"ok": False, "error": err}
            tweets = await collect_tweets(page, n)
            source = "search" if query else "home_timeline"
            if tweets:
                _install_browse_session(page, tweets, source)
                keep_page = True
            return {"ok": True, "source": source, "tweets": tweets,
                    "read_navigation": "card_click_available" if tweets else None}
        finally:
            if not keep_page:
                await _close_owned_page(page)


async def act_profile(ctx, body):
    user = (body.get("user") or "").strip().lstrip("@")
    if not user:
        # 不指定 user 时回退到配置的默认主页（TW_DEFAULT_USER）
        user = os.environ.get("TW_DEFAULT_USER", "").strip().lstrip("@")
    if not user:
        return {"ok": False, "error":
                "缺参数 user，且默认主页还没配置（TW_DEFAULT_USER）"}
    n = min(int(body.get("n", 10)), 30)
    async with _browse_lock:
        await _drop_browse_session_locked("replaced_by_profile")
        page = await new_page(ctx)
        keep_page = False
        try:
            await goto(page, f"{BASE}/{quote(user)}")
            if await page.query_selector('[data-testid="emptyState"]'):
                return {"ok": False, "error": f"用户 @{user} 不存在或无内容"}
            err = await wait_tweets(page)
            if err == "EMPTY" or err is None:
                tweets = await collect_tweets(page, n) if err is None else []
                if tweets:
                    _install_browse_session(page, tweets, "profile")
                    keep_page = True
                return {"ok": True, "user": user, "tweets": tweets,
                        "read_navigation": "card_click_available" if tweets else None}
            return {"ok": False, "error": err}
        finally:
            if not keep_page:
                await _close_owned_page(page)


def _status_id(url):
    m = re.search(r"/status/(\d+)", url or "")
    return m.group(1) if m else None


async def _extract_tweet_detail(page, sid, n):
    err = await wait_tweets(page)
    if err:
        return {"ok": False, "error": "推文不存在或已删除" if err == "EMPTY" else err}
    tweets = await collect_tweets(page, n + 5)
    main, above, replies = None, [], []
    for tweet in tweets:
        if main is None and _status_id(tweet.get("url")) == sid:
            main = tweet
        elif main is None:
            above.append(tweet)
        else:
            replies.append(tweet)
    if main is None:
        return {"ok": False,
                "error": "页面没有找到链接所指向的精确 status，拒绝拿别的推文代替"}
    return {"ok": True, "tweet": main, "thread_above": above,
            "replies": replies[:n]}


async def act_read(ctx, body):
    url = (body.get("url") or "").strip()
    sid = _status_id(url)
    if not sid:
        return {"ok": False, "error": "url 不像推文链接，要形如 https://x.com/xxx/status/123..."}
    n = min(int(body.get("n", 15)), 40)

    # 命中最近一批列表结果时，必须从原卡片点击；定位失败不静默硬跳。
    async with _browse_lock:
        if _browse_session and not _browse_session_alive(_browse_session):
            await _drop_browse_session_locked("expired_before_read")
        session = _browse_session
        if session and sid in session["ids"]:
            page = session["page"]
            restored = False
            try:
                await _configure_page_resources(page, block_images=False)
                link = await _find_tweet_link(page, sid)
                if link is None:
                    await _drop_browse_session_locked("card_not_found")
                    return {
                        "ok": False,
                        "error": "命中刚才的 feed，但在渲染列表里找不到对应推文卡片；为避免硬跳 URL，未自动降级",
                        "navigation": "card_click_failed",
                    }
                await _guarded_click(page, link)
                try:
                    await page.wait_for_function(
                        "sid => location.pathname.includes('/status/' + sid)",
                        arg=sid,
                        timeout=15000,
                    )
                except PWTimeout:
                    await _raise_if_challenge(page)
                    raise RuntimeError("点击推文卡片后没有进入详情页")
                result = await _extract_tweet_detail(page, sid, n)
                result["navigation"] = "feed_card_click"
            finally:
                if _browse_session is session:
                    try:
                        restored = await _restore_twitter_list(page, sid)
                    except ChallengeDetected:
                        with suppress(Exception):
                            await _drop_browse_session_locked("challenge_during_restore")
                        raise
                    if restored:
                        session["last_active_at"] = time.time()
                        _touch_owned_page(page)
                        with suppress(Exception):
                            await _configure_page_resources(page, block_images=True)
                    else:
                        await _drop_browse_session_locked("list_restore_failed")
            if not restored:
                result["browse_session_warning"] = "详情已读取，但返回 feed 失败；下一次 read 将走外部链接兜底"
            return result

    # 外部空降链接、缓存过期或不属于最近列表：保留直接打开作为明确兜底。
    page = await new_page(ctx, block_images=False)
    try:
        await goto(page, f"{BASE}/i/status/{sid}")
        result = await _extract_tweet_detail(page, sid, n)
        result["navigation"] = "direct_url_fallback"
        return result
    finally:
        await _close_owned_page(page)


# ---------- 写动作 ----------


def _confirmed(verified: str, *, changed=True, **extra):
    retry_safe = extra.pop("retry_safe", False)
    return {"ok": True, "outcome": "confirmed", "changed": changed,
            "retry_safe": retry_safe, "verified": verified, **extra}


def _failed(error: str, *, changed=False, **extra):
    retry_safe = extra.pop("retry_safe", True)
    return {"ok": False, "outcome": "failed", "changed": changed,
            "retry_safe": retry_safe, "error": error, **extra}


def _uncertain(error: str, *, changed=True, **extra):
    return {"ok": None, "outcome": "uncertain", "changed": changed,
            "error": error, "retry_safe": False, **extra}


def _canonical_write_text(value):
    value = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return unicodedata.normalize("NFC", value)


def _write_fingerprint(body, expected_actor):
    action = body.get("action")
    raw_url = body.get("url")
    raw_url = raw_url.strip() if isinstance(raw_url, str) else ""
    status_id = _status_id(raw_url)
    canonical = {
        "actor": expected_actor,
        "action": action,
    }
    if action in ("like", "unlike", "repost", "reply"):
        canonical["target"] = f"status:{status_id}" if status_id else raw_url
        canonical["expected_target_handle"] = (
            _normalize_handle(body.get("expected_target_handle")) or "")
    if action in ("reply", "post"):
        canonical["text"] = _canonical_write_text(body.get("text"))
    raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _request_idempotency_key(req, body):
    header_key = req.headers.get("idempotency-key", "").strip()
    body_key = str(body.get("idempotency_key") or "").strip()
    if header_key and body_key and header_key != body_key:
        return None, _failed(
            "Idempotency-Key header 与 body.idempotency_key 不一致",
            retry_safe=False)
    key = header_key or body_key
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:~-]{7,199}", key):
        return None, _failed(
            "写请求必须带 8-200 位 Idempotency-Key（字母、数字、点、冒号、下划线、波浪线或短横线）")
    return key, None


def _claim_idempotency(key, fingerprint):
    _prune_idempotency()
    rec = _idempotency.get(key)
    if rec:
        if rec.get("fingerprint") != fingerprint:
            return _failed("这个幂等键已经用于另一项写操作，拒绝复用",
                           retry_safe=False, idempotency_key=key), False
        if rec.get("status") == "completed" and isinstance(rec.get("result"), dict):
            result = dict(rec["result"])
            result["replayed"] = True
            result["idempotency_key"] = key
            return result, False
        return _uncertain(
            "同一幂等键已有执行中记录，可能曾在点击后中断；禁止重发",
            idempotency_key=key, replayed=True), False

    now = time.time()
    _idempotency[key] = {
        "fingerprint": fingerprint,
        "status": "in_progress",
        "result": None,
        "created_at": now,
        "updated_at": now,
    }
    try:
        _persist_state()
    except Exception as e:
        _idempotency.pop(key, None)
        return _failed(
            f"幂等保护无法落盘，已在点击前拒绝执行：{type(e).__name__}: {e}",
            idempotency_key=key), False
    return None, True


def _complete_idempotency(key, result):
    """只有原子落盘成功才把内存记录也从 in_progress 改成 completed。"""
    rec = _idempotency.get(key)
    if not rec:
        raise RuntimeError("幂等记录意外丢失")
    previous = dict(rec)
    rec["status"] = "completed"
    rec["result"] = dict(result)
    rec["updated_at"] = time.time()
    try:
        _persist_state()
    except Exception:
        _idempotency[key] = previous
        raise


def _release_idempotency(key):
    """确认尚未点击且可安全重试时释放 claim；落盘失败则恢复 in_progress。"""
    previous = _idempotency.pop(key, None)
    if previous is None:
        raise RuntimeError("幂等记录意外丢失")
    try:
        _persist_state()
    except Exception:
        _idempotency[key] = previous
        raise


async def _verify_expected_actor(ctx, expected_actor):
    page = await new_page(ctx)
    try:
        await goto(page, f"{BASE}/home")
        if not await check_logged_in(page):
            return None, _failed("登录态失效，拒绝写入")
        actual, error = await _read_current_actor(page)
        if error:
            return None, _failed(error)
        if actual != expected_actor:
            return actual, _failed(
                f"当前登录账号是 @{actual}，不是要求的 @{expected_actor}，拒绝写入",
                actor_handle=f"@{actual}")
        return actual, None
    finally:
        await _close_owned_page(page)


async def _click_with_x_response(page, click, markers):
    """先挂网络监听再点击；没抓到响应时不能把“可能已执行”误报成失败。"""
    await _raise_if_challenge(page)
    try:
        async with page.expect_response(
                lambda response: any(marker in response.url for marker in markers),
                timeout=12000) as response_info:
            tracker = _write_click_attempted.get()
            if isinstance(tracker, dict):
                actual_actor, actor_error = await _read_current_actor(page)
                if actor_error:
                    raise RuntimeError(actor_error)
                if actual_actor != tracker["expected_actor"]:
                    raise RuntimeError(
                        f"点击前账号已变成 @{actual_actor}，不是要求的 "
                        f"@{tracker['expected_actor']}，拒绝写入")
                tracker["actor_handle"] = actual_actor
                # 先把频率额度原子落盘，再调用真实写按钮。调用一旦开始，哪怕随后
                # 断线/取消/接口 4xx，也已经算一次尝试，不能漏账。
                record_rate(tracker["action"])
                tracker["rate_recorded"] = True
                tracker["attempted"] = True
            await click()
        await _raise_if_challenge(page)
        response = await response_info.value
        try:
            payload = await response.json()
        except Exception:
            payload = None
        return {
            "captured": True,
            "status": response.status,
            "url": response.url,
            "payload": payload,
        }
    except PWTimeout:
        await _raise_if_challenge(page)
        return {"captured": False}


def _network_public(network):
    return {k: v for k, v in (network or {}).items() if k != "payload"}


def _network_problem(network):
    if not (network or {}).get("captured"):
        return None
    status = int(network.get("status") or 0)
    if status < 200 or status >= 300:
        return f"X 接口返回 HTTP {status}"
    payload = network.get("payload")
    if isinstance(payload, dict) and payload.get("errors"):
        return "X GraphQL 响应包含 errors"
    return None


def _find_created_tweet_id(value, expected_text):
    """只接受 CreateTweet mutation 自己的 result，绝不递归捞旁支 rest_id。"""
    try:
        result = value["data"]["create_tweet"]["tweet_results"]["result"]
    except (KeyError, TypeError):
        return None

    # X 偶尔用可见性包装层包住 Tweet；只解这一层明确结构，不扫描其他节点。
    if (isinstance(result, dict)
            and result.get("__typename") == "TweetWithVisibilityResults"):
        result = result.get("tweet")
    if not isinstance(result, dict):
        return None

    rest_id = result.get("rest_id")
    legacy = result.get("legacy")
    if (not re.fullmatch(r"\d+", str(rest_id or ""))
            or not isinstance(legacy, dict)):
        return None
    full_text = legacy.get("full_text")
    if not isinstance(full_text, str):
        return None
    if _canonical_write_text(full_text) != _canonical_write_text(expected_text):
        return None
    return str(rest_id)


async def _verify_created_tweet(page, tweet_id, expected_text, expected_actor):
    """按精确 status、作者和正文三项，从 X 页面重新读取确认。"""
    try:
        expected_actor = _normalize_handle(expected_actor)
        if not expected_actor:
            return False
        article, actual_actor, error = await _open_main_article(
            page, f"{BASE}/i/status/{tweet_id}")
        if error or actual_actor != expected_actor:
            return False
        text_locator = article.locator('div[data-testid="tweetText"]')
        if not await text_locator.count():
            return False
        actual_text = await text_locator.first.inner_text()
        return (_canonical_write_text(actual_text)
                == _canonical_write_text(expected_text))
    except Exception as e:
        log_action({"action": "_write_verify_failed", "tweet_id": tweet_id,
                    "error": f"{type(e).__name__}: {e}"})
    return False


def _active_expected_actor():
    tracker = _write_click_attempted.get()
    if not isinstance(tracker, dict):
        return None
    return _normalize_handle(tracker.get("expected_actor"))


async def _find_exact_status_article(page, sid, timeout_ms=8000):
    """只认 article 自己的时间戳永久链接；引用卡片里的 status 链接不算。"""
    deadline = time.monotonic() + timeout_ms / 1000
    articles = page.locator('article[data-testid="tweet"]')
    while time.monotonic() < deadline:
        count = await articles.count()
        for index in range(count):
            article = articles.nth(index)
            try:
                href = await article.evaluate(OWN_STATUS_HREF_JS)
            except Exception:
                continue
            if _status_id(href) == sid:
                return article
        await page.wait_for_timeout(100)
    return None


async def _open_main_article(page, url):
    sid = _status_id(url)
    if not sid:
        return None, None, _failed("url 不像推文链接")
    await goto(page, f"{BASE}/i/status/{sid}")
    err = await wait_tweets(page)
    if err:
        return None, None, _failed("推文不存在或已删除" if err == "EMPTY" else err)
    art = await _find_exact_status_article(page, sid)
    if art is None:
        return None, None, _failed(
            "页面没有找到链接所指向的精确 status，拒绝操作别的推文")

    handles = set()
    name = art.locator('div[data-testid="User-Name"]')
    if await name.count():
        try:
            handles.update(_handles_in_text(await name.first.inner_text()))
        except Exception:
            pass
        links = name.first.locator("a")
        try:
            count = min(await links.count(), 6)
            for index in range(count):
                href = await links.nth(index).get_attribute("href")
                first_part = urlsplit(href or "").path.strip("/").split("/", 1)[0]
                normalized = _normalize_handle(first_part)
                if normalized:
                    handles.add(normalized)
        except Exception:
            pass
    if len(handles) != 1:
        detail = "读不到" if not handles else f"读到冲突值 {sorted(handles)}"
        return None, None, _failed(f"目标推文作者 handle {detail}，拒绝写入")
    target_handle = next(iter(handles))
    return art, target_handle, None


def _target_result(result, target_handle):
    result.setdefault("target_handle", f"@{target_handle}" if target_handle else None)
    return result


def _check_expected_target(body, target_handle):
    raw_expected = body.get("expected_target_handle")
    if raw_expected in (None, ""):
        return _failed(
            "目标写动作必须带 expected_target_handle，拒绝跳过作者核对",
            target_handle=f"@{target_handle}")
    expected = _normalize_handle(raw_expected)
    if not expected:
        return _failed("expected_target_handle 格式不合法",
                       target_handle=f"@{target_handle}")
    if expected != target_handle:
        return _failed(
            f"目标推文实际作者是 @{target_handle}，不是要求的 @{expected}，拒绝写入",
            target_handle=f"@{target_handle}")
    return None


async def _verify_target_button(page, url, target_handle, button_tid):
    """重新打开精确 status，从服务端回读后的 DOM 验证，避开乐观 UI 假翻转。"""
    try:
        article, actual_handle, error = await _open_main_article(page, url)
        if error or actual_handle != target_handle:
            return False
        button = article.locator(f'button[data-testid="{button_tid}"]')
        return bool(await button.count())
    except Exception:
        return False


async def act_like(ctx, body, undo=False):
    page = await new_page(ctx)
    try:
        art, target_handle, err = await _open_main_article(page, body.get("url"))
        if err:
            return err
        mismatch = _check_expected_target(body, target_handle)
        if mismatch:
            return mismatch
        want, other = ("unlike", "like") if undo else ("like", "unlike")
        btn = art.locator(f'button[data-testid="{want}"]')
        if await btn.count() == 0:
            if await art.locator(f'button[data-testid="{other}"]').count():
                return _target_result(
                    _confirmed("本来就是目标状态", changed=False), target_handle)
            return _target_result(
                _failed("页面上找不到点赞按钮，结构可能变了"), target_handle)
        marker = "UnfavoriteTweet" if undo else "FavoriteTweet"
        network = await _click_with_x_response(page, btn.first.click, [marker])
        ui_flipped = False
        try:
            await art.locator(f'button[data-testid="{other}"]').first.wait_for(timeout=6000)
            ui_flipped = True
        except PWTimeout:
            pass
        problem = _network_problem(network)
        if problem:
            return _target_result(
                _uncertain(f"已经点击，但 {problem}；即使本地按钮翻转也不能确认，禁止重试",
                           network=_network_public(network)), target_handle)
        if await _verify_target_button(page, body.get("url"), target_handle, other):
            return _target_result(
                _confirmed("重新打开精确 status 后按钮仍是目标状态",
                           ui_flipped=ui_flipped,
                           network=_network_public(network)), target_handle)
        return _target_result(
            _uncertain("已经点击，但重新打开精确 status 后没能确认目标状态，禁止自动重试",
                       ui_flipped=ui_flipped,
                       network=_network_public(network)), target_handle)
    finally:
        await _close_owned_page(page)


async def act_repost(ctx, body):
    page = await new_page(ctx)
    try:
        art, target_handle, err = await _open_main_article(page, body.get("url"))
        if err:
            return err
        mismatch = _check_expected_target(body, target_handle)
        if mismatch:
            return mismatch
        btn = art.locator('button[data-testid="retweet"]')
        if await btn.count() == 0:
            if await art.locator('button[data-testid="unretweet"]').count():
                return _target_result(
                    _confirmed("本来就已转发", changed=False), target_handle)
            return _target_result(_failed("找不到转发按钮"), target_handle)
        await _guarded_click(page, btn.first)
        confirm = page.locator('[data-testid="retweetConfirm"]')
        network = await _click_with_x_response(
            page, lambda: confirm.click(timeout=6000), ["CreateRetweet"])
        ui_flipped = False
        try:
            await art.locator('button[data-testid="unretweet"]').first.wait_for(timeout=6000)
            ui_flipped = True
        except PWTimeout:
            pass
        problem = _network_problem(network)
        if problem:
            return _target_result(
                _uncertain(f"已经点了转发确认，但 {problem}；即使本地按钮翻转也不能确认，禁止重试",
                           network=_network_public(network)), target_handle)
        if await _verify_target_button(
                page, body.get("url"), target_handle, "unretweet"):
            return _target_result(
                _confirmed("重新打开精确 status 后仍显示已转发",
                           ui_flipped=ui_flipped,
                           network=_network_public(network)), target_handle)
        return _target_result(
            _uncertain("已经点了转发确认，但重新打开精确 status 后没能确认，禁止自动重试",
                       ui_flipped=ui_flipped,
                       network=_network_public(network)), target_handle)
    finally:
        await _close_owned_page(page)


async def _type_and_send(page, text, button_tid):
    box = page.locator('[data-testid="tweetTextarea_0"]')
    await _guarded_click(page, box.first, timeout=10000)
    await _human_type(page, text)
    await _raise_if_challenge(page)
    btn = page.locator(f'button[data-testid="{button_tid}"]')
    await btn.first.wait_for(timeout=6000)
    if await btn.first.is_disabled():
        return {"error": "发送按钮是灰的：内容可能超长或不合规"}
    network = await _click_with_x_response(page, btn.first.click, ["CreateTweet"])
    return {"network": network}


async def act_reply(ctx, body):
    text = (body.get("text") or "").strip()
    if not text:
        return _failed("缺参数 text")
    page = await new_page(ctx)
    try:
        art, target_handle, err = await _open_main_article(page, body.get("url"))
        if err:
            return err
        mismatch = _check_expected_target(body, target_handle)
        if mismatch:
            return mismatch
        sent = await _type_and_send(page, text, "tweetButtonInline")
        if sent.get("error"):
            return _target_result(_failed(sent["error"]), target_handle)
        network = sent.get("network") or {}
        problem = _network_problem(network)
        if problem:
            return _target_result(
                _uncertain(f"发送点击已经发生，但 {problem}；禁止自动重试",
                           network=_network_public(network)), target_handle)
        tweet_id = _find_created_tweet_id(network.get("payload"), text)
        if tweet_id and await _verify_created_tweet(
                page, tweet_id, text, _active_expected_actor()):
            return _target_result(
                _confirmed("回复已按 tweet id 二次读取确认", tweet_id=tweet_id,
                           network=_network_public(network)), target_handle)
        return _target_result(
            _uncertain("发送动作已发生，但没有拿到可二次确认的回复，禁止自动重试",
                       tweet_id=tweet_id, network=_network_public(network)),
            target_handle)
    finally:
        await _close_owned_page(page)


async def act_post(ctx, body):
    text = (body.get("text") or "").strip()
    if not text:
        return _failed("缺参数 text")
    page = await new_page(ctx)
    try:
        await goto(page, f"{BASE}/home")
        if not await check_logged_in(page):
            return _failed("登录态失效")
        compose = page.locator(
            '[data-testid="SideNav_NewTweet_Button"]:visible, '
            'a[href="/compose/post"]:visible'
        ).first
        await compose.wait_for(timeout=12000)
        await _guarded_click(page, compose)
        await page.locator('[data-testid="tweetTextarea_0"]').first.wait_for(
            timeout=12000
        )
        sent = await _type_and_send(page, text, "tweetButton")
        if sent.get("error"):
            return _failed(sent["error"])
        network = sent.get("network") or {}
        problem = _network_problem(network)
        if problem:
            return _uncertain(f"发送点击已经发生，但 {problem}；禁止自动重试",
                              network=_network_public(network))
        tweet_id = _find_created_tweet_id(network.get("payload"), text)
        if tweet_id and await _verify_created_tweet(
                page, tweet_id, text, _active_expected_actor()):
            return _confirmed("新推文已按 tweet id 二次读取确认", tweet_id=tweet_id,
                              network=_network_public(network))
        return _uncertain("发送动作已发生，但没有拿到可二次确认的新推文，禁止自动重试",
                          tweet_id=tweet_id, network=_network_public(network))
    finally:
        await _close_owned_page(page)


# ---------- 路由 ----------

READ_ACTIONS = {"feed": act_feed, "profile": act_profile, "read": act_read}
WRITE_ACTIONS = {"like": act_like,
                 "unlike": lambda c, b: act_like(c, b, undo=True),
                 "repost": act_repost, "reply": act_reply, "post": act_post}


async def _execute_write(action, body, expected_actor):
    """写锁内的浏览器阶段；调用方用 wait_for 给整个阶段统一封顶。"""
    gate = check_rate(action)
    if gate:
        return _failed(gate), None
    ctx = await get_context()
    actual_actor, actor_error = await _verify_expected_actor(ctx, expected_actor)
    if actor_error:
        return actor_error, actual_actor
    result = await WRITE_ACTIONS[action](ctx, body)
    if not isinstance(result, dict):
        raise RuntimeError("写动作返回了非对象结果")
    return result, actual_actor


def _downgrade_for_infra(result, message, *, attempted):
    core = {"ok", "outcome", "changed", "retry_safe", "verified", "error"}
    extra = {key: value for key, value in result.items() if key not in core}
    extra["original_outcome"] = result.get("outcome")
    if result.get("verified"):
        extra["original_verified"] = result.get("verified")
    if result.get("error"):
        extra["original_error"] = result.get("error")
    return _uncertain(message, changed=bool(result.get("changed") or attempted), **extra)


async def _tab_sweeper():
    while True:
        await asyncio.sleep(60)
        await _expire_browse_session()
        closed = await _sweep_owned_orphans()
        if closed:
            log_action({"action": "_sweep_owned_orphans", "closed": closed})


@app.on_event("startup")
async def _start_sweeper():
    global _sweeper_task
    _load_state()
    closed = await _sweep_owned_orphans()
    if closed:
        log_action({"action": "_startup_sweep_owned_orphans", "closed": closed})
    _sweeper_task = asyncio.create_task(_tab_sweeper())


@app.on_event("shutdown")
async def _shutdown():
    """先关短时列表页，再断 Playwright；不关闭常驻 Chrome 或人工标签页。"""
    global _pw, _browser, _sweeper_task
    if _sweeper_task is not None:
        _sweeper_task.cancel()
        with suppress(asyncio.CancelledError):
            await _sweeper_task
        _sweeper_task = None
    async with _browse_lock:
        await _drop_browse_session_locked("service_shutdown")
    for task in list(_cleanup_tasks):
        with suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), timeout=12)
    _browser = None
    if _pw is not None:
        with suppress(Exception):
            await asyncio.wait_for(_pw.stop(), timeout=5)
        _pw = None


@app.get("/health")
async def health(req: Request):
    guard = _request_guard(req)
    if guard is not None:
        return guard
    base = {
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "api_version": API_VERSION,
        "allow_write": os.environ.get("TW_ALLOW_WRITE", "").strip() == "1",
        "expected_actor": _normalize_handle(os.environ.get("TW_EXPECTED_ACTOR")),
        "state_healthy": _state_healthy,
        "state_error": _state_health_error,
    }
    if not _state_loaded or not _state_healthy:
        return {"ok": False, **base,
                "error": f"运行状态不健康：{_state_health_error}"}
    try:
        ctx = await asyncio.wait_for(get_context(), timeout=45)
        return {"ok": True, **base, "tabs": len(ctx.pages),
                "owned_pages": len(_owned_pages),
                "tracked_orphans": len(_owned_targets) - len(_owned_pages),
                "browse_session": bool(_browse_session_alive(_browse_session)),
                "browse_ttl_seconds": BROWSE_TTL_SECONDS}
    except asyncio.TimeoutError:
        return {"ok": False, **base, "error": "连中继浏览器超时（含自愈重试仍失败）"}
    except Exception as e:
        return {"ok": False, **base, "error": f"连不上中继浏览器: {e}"}


@app.post("/twitter")
async def twitter(req: Request):
    guard = _request_guard(req, require_json=True)
    if guard is not None:
        return guard
    try:
        body = await req.json()
    except Exception:
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "body 不是合法 JSON"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "body 必须是 JSON 对象"})
    action = body.get("action")
    if action in READ_ACTIONS:
        async with READ_SEM:
            try:
                ctx = await get_context()
                return await asyncio.wait_for(
                    READ_ACTIONS[action](ctx, body), timeout=150)
            except ChallengeDetected as exc:
                return exc.result
            except asyncio.TimeoutError:
                return {"ok": False, "error": "动作整体超时（150秒），浏览器可能卡住了"}
            except Exception as e:
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if action in WRITE_ACTIONS:
        if os.environ.get("TW_ALLOW_WRITE", "").strip() != "1":
            return _failed("TW_ALLOW_WRITE 未设为 1，写动作已硬关闭")
        if not _state_loaded or not _state_healthy:
            return _failed(
                f"运行状态不健康，写动作已硬关闭：{_state_health_error}",
                retry_safe=False)
        expected_actor = _normalize_handle(os.environ.get("TW_EXPECTED_ACTOR"))
        if not expected_actor:
            return _failed("TW_EXPECTED_ACTOR 未配置或格式不合法，拒绝写入")
        idempotency_key, key_error = _request_idempotency_key(req, body)
        if key_error:
            return key_error
        fingerprint = _write_fingerprint(body, expected_actor)
        async with WRITE_LOCK:
            replay, claimed = _claim_idempotency(idempotency_key, fingerprint)
            if not claimed:
                return replay

            tracker = {"attempted": False, "rate_recorded": False,
                       "action": action, "expected_actor": expected_actor}
            tracker_token = _write_click_attempted.set(tracker)
            actual_actor = None
            try:
                result, actual_actor = await asyncio.wait_for(
                    _execute_write(action, body, expected_actor),
                    timeout=WRITE_TIMEOUT_SECONDS)
            except ChallengeDetected as exc:
                challenge = exc.result
                if tracker["attempted"]:
                    result = _uncertain(
                        challenge["hint"] + "；写点击可能已经发生，禁止自动重试",
                        status="challenge", platform="twitter",
                        manual_intervention_required=True,
                        challenge_url=challenge.get("url"))
                else:
                    result = _failed(
                        challenge["hint"] + "；尚未发生平台写点击；完成验证后请人工发起一项新操作",
                        retry_safe=False, status="challenge", platform="twitter",
                        manual_intervention_required=True,
                        challenge_url=challenge.get("url"))
            except asyncio.TimeoutError:
                if tracker["attempted"]:
                    result = _uncertain("动作整体超时；写点击可能已经生效，禁止自动重试")
                else:
                    result = _failed("动作整体超时，但尚未发生平台写点击，可以换新幂等键重试")
            except Exception as e:
                if tracker["attempted"]:
                    result = _uncertain(
                        f"写点击后异常：{type(e).__name__}: {e}；是否生效不确定，禁止自动重试")
                else:
                    result = _failed(
                        f"写点击前异常：{type(e).__name__}: {e}；尚未触发平台写入")

            attempted = tracker["attempted"]
            if actual_actor:
                result.setdefault("actor_handle", f"@{actual_actor}")
            result["write_attempted"] = attempted
            result["idempotency_key"] = idempotency_key
            result.setdefault("replayed", False)

            try:
                log_action({
                    "action": action,
                    "url": body.get("url"),
                    "text": body.get("text"),
                    "expected_target_handle": body.get("expected_target_handle"),
                    "idempotency_key": idempotency_key,
                    "result": result,
                })
            except Exception as e:
                if attempted or result.get("outcome") in ("confirmed", "uncertain"):
                    result = _downgrade_for_infra(
                        result,
                        f"写动作已有结果，但审计日志无法落盘：{type(e).__name__}: {e}；禁止重试",
                        attempted=attempted)
                else:
                    result = _failed(
                        f"平台写点击未发生；审计日志落盘失败：{type(e).__name__}: {e}",
                        idempotency_key=idempotency_key,
                        write_attempted=False)

            try:
                if (not attempted
                        and result.get("outcome") == "failed"
                        and result.get("retry_safe") is True):
                    _release_idempotency(idempotency_key)
                    result["idempotency_released"] = True
                else:
                    _complete_idempotency(idempotency_key, result)
            except Exception as e:
                if attempted or result.get("outcome") in ("confirmed", "uncertain"):
                    result = _downgrade_for_infra(
                        result,
                        f"写动作已有结果，但幂等状态无法落盘：{type(e).__name__}: {e}；禁止重试",
                        attempted=attempted)
                else:
                    result = _failed(
                        f"平台写点击未发生；幂等状态无法安全更新：{type(e).__name__}: {e}",
                        retry_safe=False,
                        idempotency_key=idempotency_key,
                        write_attempted=False)
                try:
                    log_action({
                        "action": "_write_finalization_failed",
                        "original_action": action,
                        "idempotency_key": idempotency_key,
                        "finalization_error": f"{type(e).__name__}: {e}",
                        "result": result,
                    })
                except Exception:
                    # 首条平台结果日志若已成功，至少还在；修正日志失败不能把
                    # 对外的 uncertain / 禁止重试结论重新抬成 confirmed。
                    pass
            finally:
                _write_click_attempted.reset(tracker_token)
            return result
    return _failed(
        f"不认识的 action: {action}。可用: feed/profile/read/like/unlike/repost/reply/post")
