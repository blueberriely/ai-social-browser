#!/usr/bin/env python3
"""Unified, read-only-by-default MCP adapter for social-browser actions."""

import base64
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


XHS_URL = os.environ.get("XHS_ACTION_URL", "http://127.0.0.1:8273/xiaohongshu")
X_URL = os.environ.get("X_ACTION_URL", "http://127.0.0.1:8272/twitter")
ENABLE_X_WRITE = os.environ.get("SOCIAL_ENABLE_X_WRITE", "").lower() in {"1", "true", "yes"}
FFMPEG = os.environ.get("SOCIAL_FFMPEG") or shutil.which("ffmpeg")
FFPROBE = os.environ.get("SOCIAL_FFPROBE") or shutil.which("ffprobe")
WHISPER = os.environ.get("SOCIAL_WHISPER") or shutil.which("whisper-cli")
WHISPER_MODEL = os.environ.get("SOCIAL_WHISPER_MODEL")
MAX_VIDEO_BYTES = int(os.environ.get("SOCIAL_MAX_VIDEO_BYTES", str(100 * 1024 * 1024)))
MAX_VIDEO_SECONDS = float(os.environ.get("SOCIAL_MAX_VIDEO_SECONDS", "600"))
LOCAL_ROOTS = tuple(
    Path(item).expanduser().resolve()
    for item in os.environ.get("SOCIAL_VIDEO_ROOTS", "").split(os.pathsep)
    if item.strip()
)


def _schema(properties=None, required=None, **extra):
    value = {"type": "object", "properties": properties or {}, **extra}
    if required:
        value["required"] = required
    return value


TOOLS = [
    {"name": "xhs_feed", "description": "读取小红书发现页。只读。", "inputSchema": _schema({"n": {"type": "integer", "minimum": 1, "maximum": 30}})},
    {"name": "xhs_search", "description": "搜索小红书笔记。只读。", "inputSchema": _schema({"query": {"type": "string", "minLength": 1, "maxLength": 100}, "n": {"type": "integer", "minimum": 1, "maximum": 30}}, ["query"])},
    {"name": "xhs_read", "description": "读取小红书笔记、图片与评论。支持 xhslink.com/.cn。只读。", "inputSchema": _schema({"url": {"type": "string", "minLength": 1}, "comments": {"type": "integer", "minimum": 0, "maximum": 40}}, ["url"])},
    {"name": "xhs_watch_video", "description": "实际获取小红书视频，返回原始音频转写与八格抽样画面。只读。", "inputSchema": _schema({"url": {"type": "string", "minLength": 1}}, ["url"])},
    {"name": "xhs_profile", "description": "读取小红书用户资料与最近笔记。只读。", "inputSchema": _schema({"url": {"type": "string"}, "user": {"type": "string"}, "n": {"type": "integer", "minimum": 1, "maximum": 30}}, anyOf=[{"required": ["url"]}, {"required": ["user"]}])},
    {"name": "x_feed", "description": "读取X首页或搜索结果。只读。", "inputSchema": _schema({"query": {"type": "string", "maxLength": 100}, "n": {"type": "integer", "minimum": 1, "maximum": 30}, "latest": {"type": "boolean"}})},
    {"name": "x_read", "description": "读取单条X帖子、线程与回复。只读。", "inputSchema": _schema({"url": {"type": "string", "minLength": 1}, "replies": {"type": "integer", "minimum": 0, "maximum": 40}}, ["url"])},
    {"name": "x_watch_video", "description": "实际获取X帖子视频，返回原始音频转写与八格抽样画面。只读。", "inputSchema": _schema({"url": {"type": "string", "minLength": 1}}, ["url"])},
    {"name": "x_profile", "description": "读取X用户资料与最近帖子。只读。", "inputSchema": _schema({"user": {"type": "string", "minLength": 1}, "n": {"type": "integer", "minimum": 1, "maximum": 30}}, ["user"])},
    {"name": "video_watch", "description": "分析普通视频文件或安全的公开HTTPS视频直链。返回原始音频转写与八格抽样画面。", "inputSchema": _schema({"url": {"type": "string"}, "path": {"type": "string"}}, anyOf=[{"required": ["url"]}, {"required": ["path"]}])},
]

if ENABLE_X_WRITE:
    TOOLS.extend([
        {"name": "x_like", "description": "点赞X帖子。写操作；受服务端限频和审计保护。", "inputSchema": _schema({"url": {"type": "string"}}, ["url"])},
        {"name": "x_repost", "description": "转发X帖子。写操作；受服务端限频和审计保护。", "inputSchema": _schema({"url": {"type": "string"}}, ["url"])},
        {"name": "x_reply", "description": "回复X帖子。写操作；uncertain结果禁止重试。", "inputSchema": _schema({"url": {"type": "string"}, "text": {"type": "string"}}, ["url", "text"])},
        {"name": "x_post", "description": "发布X帖子。写操作；uncertain结果禁止重试。", "inputSchema": _schema({"text": {"type": "string"}}, ["text"])},
    ])


def _action(endpoint, platform, action, **arguments):
    request = urllib.request.Request(
        endpoint,
        data=json.dumps({"action": action, **arguments}, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=160) as response:
            return json.load(response)
    except urllib.error.URLError as exc:
        return {"ok": False, "error": f"{platform}动作服务不可用：{exc}"}


def _xhs(action, **arguments):
    return _action(XHS_URL, "小红书", action, **arguments)


def _x(action, **arguments):
    return _action(X_URL, "X", action, **arguments)


def _is_public_host(host):
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"无法解析视频域名：{exc}") from exc
    for entry in addresses:
        address = ipaddress.ip_address(entry[4][0])
        if not address.is_global:
            raise ValueError("视频域名解析到了非公网地址")


def _safe_public_url(url, allowed_suffixes=None):
    parsed = urllib.parse.urlparse(url or "")
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        raise ValueError("视频直链必须是无凭据的HTTPS地址")
    if allowed_suffixes and not any(host == suffix or host.endswith("." + suffix) for suffix in allowed_suffixes):
        raise ValueError("视频地址不在允许的媒体域名内")
    _is_public_host(host)
    return url


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_suffixes=None):
        self.allowed_suffixes = allowed_suffixes

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _safe_public_url(newurl, self.allowed_suffixes)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download_video(url, target, allowed_suffixes=None):
    _safe_public_url(url, allowed_suffixes)
    if ".m3u8" in urllib.parse.urlparse(url).path.lower():
        if not FFMPEG:
            raise ValueError("找不到ffmpeg，无法读取HLS视频")
        # Platform playlists are only accepted from the explicit media-domain
        # allowlist above.  ffmpeg follows the signed variant URLs and muxes
        # audio/video into one disposable local file for the shared pipeline.
        subprocess.run(
            [FFMPEG, "-loglevel", "error", "-y", "-i", url, "-c", "copy", str(target)],
            timeout=180,
            check=True,
        )
        if not target.is_file() or target.stat().st_size > MAX_VIDEO_BYTES:
            raise ValueError("视频不存在或超过读取上限")
        return
    opener = urllib.request.build_opener(_SafeRedirect(allowed_suffixes))
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    total = 0
    with opener.open(request, timeout=30) as response, target.open("wb") as output:
        _safe_public_url(response.geturl(), allowed_suffixes)
        content_type = response.headers.get_content_type()
        if content_type not in {"application/octet-stream", "binary/octet-stream"} and not content_type.startswith("video/"):
            raise ValueError(f"直链返回的不是视频媒体：{content_type}")
        declared_size = response.headers.get("Content-Length")
        if declared_size and int(declared_size) > MAX_VIDEO_BYTES:
            raise ValueError("视频超过读取上限")
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_VIDEO_BYTES:
                raise ValueError("视频超过读取上限")
            output.write(chunk)


def _local_video(path):
    candidate = Path(path or "").expanduser().resolve()
    if not LOCAL_ROOTS:
        raise ValueError("本地视频读取未启用；请设置 SOCIAL_VIDEO_ROOTS")
    if not any(candidate == root or root in candidate.parents for root in LOCAL_ROOTS):
        raise ValueError("本地视频不在允许目录内")
    if not candidate.is_file() or candidate.stat().st_size > MAX_VIDEO_BYTES:
        raise ValueError("本地视频不存在或超过读取上限")
    return candidate


def _probe_duration(video):
    if not FFPROBE:
        raise ValueError("找不到ffprobe；请安装FFmpeg或设置SOCIAL_FFPROBE")
    completed = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(video)],
        capture_output=True, text=True, timeout=20, check=True,
    )
    duration = float(completed.stdout.strip())
    if duration > MAX_VIDEO_SECONDS:
        raise ValueError("视频超过时长上限")
    return duration


def _transcribe(video, workdir):
    if not (WHISPER and WHISPER_MODEL):
        return None, "未配置whisper-cli与模型，未生成音频转写"
    if not (Path(WHISPER).is_file() and Path(WHISPER_MODEL).is_file() and FFMPEG):
        return None, "Whisper、模型或ffmpeg不可用，未生成音频转写"
    audio = workdir / "audio.wav"
    subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-i", str(video), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(audio)], timeout=90, check=True)
    output = workdir / "transcript"
    subprocess.run([WHISPER, "-m", WHISPER_MODEL, "-f", str(audio), "-l", "auto", "-t", "2", "-nt", "-otxt", "-of", str(output)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300, check=True)
    return output.with_suffix(".txt").read_text(encoding="utf-8").strip(), None


def _contact_sheet(video, workdir, duration):
    if not FFMPEG:
        raise ValueError("找不到ffmpeg；请安装FFmpeg或设置SOCIAL_FFMPEG")
    output = workdir / "frames.jpg"
    interval = max(duration / 8.0, 0.25)
    subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-i", str(video), "-vf", f"fps=1/{interval:.3f},scale=320:-1,tile=4x2", "-frames:v", "1", str(output)], timeout=120, check=True)
    return base64.b64encode(output.read_bytes()).decode("ascii")


def _text_result(value, is_error=False):
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, indent=2)}], "isError": is_error}


def _process_video(source, metadata=None, allowed_suffixes=None):
    try:
        with tempfile.TemporaryDirectory(prefix="social-video-") as raw_workdir:
            workdir = Path(raw_workdir)
            if source.get("path"):
                video = _local_video(source["path"])
            else:
                video = workdir / "video.mp4"
                _download_video(source.get("url"), video, allowed_suffixes)
            duration = _probe_duration(video)
            transcript, warning = _transcribe(video, workdir)
            image = _contact_sheet(video, workdir, duration)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return _text_result({"ok": False, "error": f"视频处理失败：{exc}"}, True)
    summary = {
        "ok": True,
        "metadata": metadata,
        "video": {"duration_seconds": duration, "source": "redacted after processing"},
        "audio_transcript_raw": transcript,
        "transcript_warning": warning or "机器转写可能误认姓名、专名、方言和重叠语音，请结合抽样画面核对。",
        "frame_sampling": "8 frames across the duration, left-to-right then top-to-bottom",
    }
    return {"content": [{"type": "text", "text": json.dumps(summary, ensure_ascii=False, indent=2)}, {"type": "image", "data": image, "mimeType": "image/jpeg"}]}


def _platform_watch(platform, arguments):
    metadata = (_xhs if platform == "xhs" else _x)("watch", url=arguments.get("url"), n=10)
    if not metadata.get("ok"):
        return _text_result(metadata, True)
    media_url = (metadata.get("video") or {}).get("url")
    if not media_url:
        return _text_result({"ok": False, "error": f"{platform}页面已识别视频，但未捕获到媒体流"}, True)
    suffixes = ("xhscdn.com",) if platform == "xhs" else ("video.twimg.com", "pbs.twimg.com")
    clean_metadata = {**metadata, "video": {**(metadata.get("video") or {}), "url": "[redacted]"}}
    return _process_video({"url": media_url}, clean_metadata, suffixes)


def call_tool(name, arguments):
    arguments = arguments or {}
    if name == "xhs_watch_video":
        return _platform_watch("xhs", arguments)
    if name == "x_watch_video":
        return _platform_watch("x", arguments)
    if name == "video_watch":
        return _process_video({"url": arguments.get("url"), "path": arguments.get("path")})
    read_calls = {
        "xhs_feed": (_xhs, "feed", {"n": arguments.get("n", 10)}),
        "xhs_search": (_xhs, "search", {"query": arguments.get("query"), "n": arguments.get("n", 10)}),
        "xhs_read": (_xhs, "read", {"url": arguments.get("url"), "n": arguments.get("comments", 10)}),
        "xhs_profile": (_xhs, "profile", {"url": arguments.get("url"), "user": arguments.get("user"), "n": arguments.get("n", 10)}),
        "x_feed": (_x, "feed", {"query": arguments.get("query"), "n": arguments.get("n", 10), "latest": arguments.get("latest", False)}),
        "x_read": (_x, "read", {"url": arguments.get("url"), "n": arguments.get("replies", 15)}),
        "x_profile": (_x, "profile", {"user": arguments.get("user"), "n": arguments.get("n", 10)}),
    }
    if name in read_calls:
        caller, action, params = read_calls[name]
        value = caller(action, **params)
        return _text_result(value, not value.get("ok", False))
    if ENABLE_X_WRITE and name in {"x_like", "x_repost", "x_reply", "x_post"}:
        action = name.removeprefix("x_")
        value = _x(action, **arguments)
        return _text_result(value, value.get("outcome") in {"failed", "challenge"})
    return _text_result({"ok": False, "error": f"未知或未启用的工具：{name}"}, True)


def handle(message):
    method, request_id = message.get("method"), message.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": (message.get("params") or {}).get("protocolVersion", "2025-06-18"), "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "social-browser", "version": "0.2.0"}}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = message.get("params") or {}
        return {"jsonrpc": "2.0", "id": request_id, "result": call_tool(params.get("name"), params.get("arguments"))}
    if request_id is None:
        return None
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}


def main():
    for line in sys.stdin:
        try:
            response = handle(json.loads(line))
        except Exception as exc:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": str(exc)}}
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
