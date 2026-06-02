from fastapi import FastAPI, WebSocket, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse, PlainTextResponse, HTMLResponse, JSONResponse, RedirectResponse
import asyncio
import os
import logging
import httpx
import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from datetime import datetime
from typing import List, Optional

from ytb.models import (
    VideoInfoRequest, VideoInfo, DownloadRequest
)
from ytb.downloader import YTDownloader
from ytb.config import Config
from ytb.history_manager import HistoryManager
from ytb.updater import YtDlpUpdater
from ytb.browser_cookies import BrowserCookieExtractor
from telegram import TelegramService
from version import __version__

app = FastAPI(title="YouTube Video Downloader API")

# CORS配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize downloader with Docker-compatible path
download_dir = "/app/downloads" if os.path.exists("/app") else "downloads"
downloader = YTDownloader(download_dir)

# Initialize config
config = Config()

# Initialize history manager
history_manager = HistoryManager()

# Initialize Telegram notifications
telegram_service = TelegramService(config)

# Initialize yt-dlp updater
updater = YtDlpUpdater()

# Initialize browser cookie extractor with CookieCloud config
cookie_extractor = BrowserCookieExtractor(cookiecloud_config=config.get_cookiecloud_config())

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

# WebSocket connections
active_connections: List[WebSocket] = []
telegram_polling_task: Optional[asyncio.Task] = None
telegram_update_offset: Optional[int] = None
TELEGRAM_YOUTUBE_URL_RE = re.compile(
    r"https?://(?:(?:[a-z0-9-]+\.)*youtube\.com|youtu\.be)/[^\s<>\"]+",
    re.IGNORECASE,
)

AUTH_COOKIE_NAME = "ytb_dl_session"
SESSION_MAX_AGE = 7 * 24 * 60 * 60
AUTH_PUBLIC_PATHS = {
    "/login",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/status",
}


def _display_download_path(filepath: Optional[str]) -> Optional[str]:
    if not filepath:
        return None

    host_download_path = os.environ.get("HOST_DOWNLOAD_PATH", "").rstrip("/")
    if not host_download_path:
        return filepath

    container_download_dir = os.path.abspath(download_dir)
    absolute_path = os.path.abspath(filepath)
    if absolute_path == container_download_dir:
        return host_download_path
    if absolute_path.startswith(container_download_dir + os.sep):
        return host_download_path + absolute_path[len(container_download_dir):].replace(os.sep, "/")
    return filepath


def _auth_config_path() -> str:
    if os.path.exists("/app"):
        return "/app/config/auth.json"
    return os.path.join("config", "auth.json")


def _load_auth_settings() -> dict:
    """Load or create auth settings without committing secrets to the repo."""
    auth_path = _auth_config_path()
    os.makedirs(os.path.dirname(auth_path), exist_ok=True)

    settings = {}
    if os.path.exists(auth_path):
        try:
            with open(auth_path, "r", encoding="utf-8") as f:
                settings = json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load auth config, regenerating missing fields: {e}")

    changed = False
    if not settings.get("username"):
        settings["username"] = os.environ.get("WEB_AUTH_USERNAME", "admin")
        changed = True
    if not settings.get("password"):
        settings["password"] = os.environ.get("WEB_AUTH_PASSWORD") or secrets.token_urlsafe(18)
        changed = True
    if not settings.get("api_token"):
        settings["api_token"] = os.environ.get("API_TOKEN") or secrets.token_urlsafe(32)
        changed = True
    if not settings.get("session_secret"):
        settings["session_secret"] = os.environ.get("AUTH_SECRET") or secrets.token_urlsafe(32)
        changed = True

    for key, env_name in (
        ("username", "WEB_AUTH_USERNAME"),
        ("password", "WEB_AUTH_PASSWORD"),
        ("api_token", "API_TOKEN"),
        ("session_secret", "AUTH_SECRET"),
    ):
        if os.environ.get(env_name):
            settings[key] = os.environ[env_name]

    if changed or not os.path.exists(auth_path):
        with open(auth_path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
        try:
            os.chmod(auth_path, 0o600)
        except OSError:
            pass

    return settings


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _sign_session(payload: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
    return _b64url(digest)


def _create_session(username: str) -> str:
    settings = _load_auth_settings()
    expires = int(time.time()) + SESSION_MAX_AGE
    payload = _b64url(f"{username}:{expires}".encode("utf-8"))
    signature = _sign_session(payload, settings["session_secret"])
    return f"{payload}.{signature}"


def _verify_session_cookie(token: Optional[str]) -> bool:
    if not token or "." not in token:
        return False

    settings = _load_auth_settings()
    payload, signature = token.rsplit(".", 1)
    expected = _sign_session(payload, settings["session_secret"])
    if not hmac.compare_digest(signature, expected):
        return False

    try:
        padded = payload + "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        username, expires_raw = decoded.rsplit(":", 1)
        return username == settings["username"] and int(expires_raw) >= int(time.time())
    except Exception:
        return False


def _token_from_request(request: Request) -> Optional[str]:
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    return request.headers.get("x-api-token") or request.query_params.get("token")


def _verify_api_token(request: Request) -> bool:
    token = _token_from_request(request)
    settings = _load_auth_settings()
    return bool(token) and hmac.compare_digest(token, settings["api_token"])


def _is_authenticated(request: Request) -> bool:
    return _verify_api_token(request) or _verify_session_cookie(request.cookies.get(AUTH_COOKIE_NAME))


@app.on_event("startup")
async def start_background_tasks():
    global telegram_polling_task
    if telegram_polling_task is None or telegram_polling_task.done():
        telegram_polling_task = asyncio.create_task(telegram_polling_loop())


@app.on_event("shutdown")
async def stop_background_tasks():
    if telegram_polling_task and not telegram_polling_task.done():
        telegram_polling_task.cancel()


async def telegram_polling_loop() -> None:
    global telegram_update_offset
    initialized = False

    while True:
        try:
            if not telegram_service.bot_downloads_enabled():
                initialized = False
                await asyncio.sleep(10)
                continue

            if not initialized:
                updates = await telegram_service.get_updates(timeout=0)
                if updates:
                    telegram_update_offset = max(item.get("update_id", 0) for item in updates) + 1
                initialized = True

            updates = await telegram_service.get_updates(offset=telegram_update_offset, timeout=25)
            if updates is None:
                await asyncio.sleep(10)
                continue

            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    telegram_update_offset = max(telegram_update_offset or 0, update_id + 1)

                message = update.get("message") or update.get("channel_post") or {}
                chat = message.get("chat") or {}
                urls = _extract_telegram_urls(message)
                if not urls:
                    continue

                chat_id = str(chat.get("id", "")).strip()
                if chat_id != telegram_service.allowed_chat_id():
                    logger.info("Ignored Telegram YouTube link from unauthorized chat %s", chat_id)
                    if chat_id:
                        await telegram_service.send_message(
                            "YTB-DL 收到 YouTube 链接，但当前会话未授权。\n\n"
                            f"当前 Chat ID：{chat_id}\n"
                            "请把高级设置里的 Chat ID 改成这个值，或在已配置的会话里发送链接。",
                            chat_id=chat_id,
                        )
                    continue

                for url in urls:
                    asyncio.create_task(start_telegram_download(url))

        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Telegram polling loop failed: %s", exc)
            await asyncio.sleep(10)


def _extract_telegram_urls(message: dict) -> List[str]:
    seen = set()
    urls = []
    text = message.get("text") or message.get("caption") or ""
    entities = message.get("entities") or message.get("caption_entities") or []

    candidates = TELEGRAM_YOUTUBE_URL_RE.findall(text or "")
    for entity in entities:
        if (
            entity.get("type") == "text_link"
            and entity.get("url")
            and TELEGRAM_YOUTUBE_URL_RE.match(entity["url"])
        ):
            candidates.append(entity["url"])

    for match in candidates:
        url = match.rstrip(".,;，。；)]}>")
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


async def start_telegram_download(url: str) -> None:
    try:
        await telegram_service.send_message(f"📥 YTB-DL 已收到下载链接\n\n🔗 链接：{url}")
        result = await start_download(DownloadRequest(url=url, format_id=None), source="Telegram")
        task_id = result.get("task_id", "")
        if task_id:
            await telegram_service.send_message(f"🧾 YTB-DL 下载任务已创建\n\n🔗 链接：{url}\n🧾 任务：{task_id}")
    except Exception as exc:
        logger.error("Failed to start Telegram download for %s: %s", url, exc)
        await telegram_service.send_message(f"❌ YTB-DL 无法创建下载任务\n\n🔗 链接：{url}\n⚠️ 错误：{exc}")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path

    if request.method == "OPTIONS":
        return await call_next(request)

    if path in AUTH_PUBLIC_PATHS:
        return await call_next(request)

    if not _is_authenticated(request):
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Authentication required"}, status_code=401)
        return RedirectResponse("/login", status_code=303)

    return await call_next(request)


@app.get("/login")
async def login_page(request: Request):
    if _is_authenticated(request):
        return RedirectResponse("/", status_code=303)

    return HTMLResponse("""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>登录 - YTB-DL</title>
  <style>
    body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0f172a;color:#e5e7eb;font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
    form{width:min(360px,calc(100vw - 40px));display:grid;gap:14px}
    h1{font-size:24px;margin:0 0 8px}
    input,button{height:42px;border-radius:6px;border:1px solid #334155;background:#111827;color:#e5e7eb;padding:0 12px;font-size:15px}
    button{background:#2563eb;border-color:#2563eb;cursor:pointer;font-weight:600}
    p{min-height:22px;margin:0;color:#fca5a5;font-size:14px}
  </style>
</head>
<body>
  <form id="login-form">
    <h1>YTB-DL</h1>
    <input id="username" name="username" autocomplete="username" placeholder="用户名" required />
    <input id="password" name="password" type="password" autocomplete="current-password" placeholder="密码" required />
    <button type="submit">登录</button>
    <p id="message"></p>
  </form>
  <script>
    document.getElementById('login-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      const message = document.getElementById('message');
      message.textContent = '';
      const response = await fetch('/api/auth/login', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          username: document.getElementById('username').value,
          password: document.getElementById('password').value
        })
      });
      if (response.ok) {
        location.href = '/';
      } else {
        message.textContent = '用户名或密码错误';
      }
    });
  </script>
</body>
</html>
""")


@app.post("/api/auth/login")
async def login(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {}

    settings = _load_auth_settings()
    username_ok = hmac.compare_digest(str(data.get("username", "")), settings["username"])
    password_ok = hmac.compare_digest(str(data.get("password", "")), settings["password"])
    if not (username_ok and password_ok):
        raise HTTPException(status_code=401, detail="Invalid username or password")

    response = JSONResponse({"success": True})
    response.set_cookie(
        AUTH_COOKIE_NAME,
        _create_session(settings["username"]),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return response


@app.post("/api/auth/logout")
async def logout():
    response = JSONResponse({"success": True})
    response.delete_cookie(AUTH_COOKIE_NAME)
    return response


@app.get("/api/auth/status")
async def auth_status(request: Request):
    settings = _load_auth_settings()
    return {
        "authenticated": _is_authenticated(request),
        "username": settings["username"] if _is_authenticated(request) else None,
        "token_auth_available": bool(settings.get("api_token")),
    }


@app.get("/")
async def root():
    """返回主页面"""
    try:
        frontend_path = os.path.join(os.path.dirname(__file__), "frontend", "index.html")
        if os.path.exists(frontend_path):
            return FileResponse(frontend_path)
        else:
            return {"message": "YouTube Downloader API", "version": "1.0.0", "error": "Frontend not found"}
    except Exception as e:
        return {"message": "YouTube Downloader API", "version": "1.0.0", "error": str(e)}


@app.post("/api/video-info", response_model=VideoInfo)
async def get_video_info(request: VideoInfoRequest):
    """获取视频信息"""
    try:
        print(f"Fetching video info for URL: {request.url}")
        info = await downloader.get_video_info(request.url)
        print(f"Video info fetched successfully: {info.get('title', 'Unknown')}")
        return VideoInfo(**info)
    except Exception as e:
        print(f"Error in get_video_info: {str(e)}")
        error_msg = str(e)
        if "Unsupported URL" in error_msg:
            error_msg = "不支持的URL格式，请确保输入正确的YouTube链接"
        elif "Video unavailable" in error_msg:
            error_msg = "视频不可用或已被删除"
        raise HTTPException(status_code=400, detail=error_msg)


@app.post("/api/download")
async def start_download(request: DownloadRequest, source: str = "Web"):
    """开始下载视频"""
    try:
        # Generate task_id early
        import uuid
        task_id = str(uuid.uuid4())
        selected_format_id = request.format_id

        # Set up 403/network error notification callback for Web downloads
        async def web_error_callback(task_id: str, url: str, status: str, retry_count: int = 0, final: bool = False, success: bool = False):
            """Handle 403 and network error notifications for Web downloads"""
            entry = history_manager.get_entry(task_id)
            title = entry.get("title", "Unknown") if entry else "Unknown"
            is_network_error = "[网络错误]" in status

            if success:
                error_msg = "网络错误已恢复" if is_network_error else "Cookie刷新成功，下载已恢复"
                await notify_telegram(
                    task_id=task_id,
                    title=title,
                    url=url,
                    source=source,
                    status="completed",
                    error_message=error_msg,
                    format_id=selected_format_id
                )
            elif final:
                error_msg = (
                    f"网络连接错误（重试{retry_count}次失败）"
                    if is_network_error
                    else f"403 Forbidden - 需要登录（重试{retry_count}次失败）"
                )
                await notify_telegram(
                    task_id=task_id,
                    title=title,
                    url=url,
                    source=source,
                    status="error",
                    error_message=error_msg,
                    format_id=selected_format_id
                )
            else:
                clean_status = status.replace("[网络错误] ", "")
                retry_type = "网络错误" if is_network_error else "403/Cookie 错误"
                await notify_telegram(
                    task_id=task_id,
                    title=title,
                    url=url,
                    source=source,
                    status="error",
                    error_message=f"{retry_type}，第 {retry_count} 次重试：{clean_status}",
                    format_id=selected_format_id
                )

        # Get video info BEFORE starting download
        info = await downloader.get_video_info(request.url)

        # Add to history with correct info
        history_entry = {
            "id": task_id,
            "url": request.url,
            "title": info.get("title", "Unknown"),
            "thumbnail": info.get("thumbnail"),
            "uploader": info.get("uploader"),
            "downloaded_at": datetime.now().isoformat(),
            "status": "downloading",
            "file_path": None,
            "file_size": None
        }
        history_manager.add_entry(history_entry)

        # Register the callback
        downloader.set_403_notification_callback(task_id, web_error_callback)

        # Start download with pre-assigned task_id
        actual_task_id = await downloader.download_video_with_id(request.url, task_id, request.format_id)

        # Estimate file size for notification display
        estimated_size = None
        if info.get('formats'):
            # Try to get file size from formats
            for fmt in info['formats']:
                if fmt.get('filesize') or fmt.get('filesize_approx'):
                    size = fmt.get('filesize') or fmt.get('filesize_approx')
                    if not estimated_size or size > estimated_size:
                        estimated_size = size

        if estimated_size:
            info['estimated_filesize'] = estimated_size

        await notify_telegram(
            task_id=task_id,
            title=info.get("title", "Unknown"),
            url=request.url,
            source=source,
            video_info=info,
            status="started",
            format_id=selected_format_id
        )

        # Start monitoring task for completion
        import asyncio
        asyncio.create_task(monitor_web_download(
            task_id=task_id,
            title=info.get("title", "Unknown"),
            url=request.url,
            video_info=info,
            format_id=selected_format_id,
            source=source
        ))

        return {"task_id": task_id, "message": "Download started"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/remote/download")
async def remote_download(request: DownloadRequest):
    """Token-protected remote download entrypoint."""
    return await start_download(request)


@app.get("/api/download-status/{task_id}")
async def get_download_status(task_id: str):
    """获取下载进度"""
    status = downloader.get_download_status(task_id)

    if status is None:
        raise HTTPException(status_code=404, detail="Task not found")

    progress_info = status.get('progress', {})

    # Format speed
    speed = progress_info.get('speed', 0)
    speed_str = None
    if speed:
        if speed > 1024 * 1024:
            speed_str = f"{speed / 1024 / 1024:.2f} MB/s"
        elif speed > 1024:
            speed_str = f"{speed / 1024:.2f} KB/s"
        else:
            speed_str = f"{speed:.0f} B/s"

    # Format sizes
    def format_size(bytes_size):
        if not bytes_size:
            return None
        if bytes_size > 1024 * 1024 * 1024:
            return f"{bytes_size / 1024 / 1024 / 1024:.2f} GB"
        elif bytes_size > 1024 * 1024:
            return f"{bytes_size / 1024 / 1024:.2f} MB"
        elif bytes_size > 1024:
            return f"{bytes_size / 1024:.2f} KB"
        else:
            return f"{bytes_size} B"

    response = {
        "task_id": task_id,
        "status": status.get('status', 'unknown'),
        "progress": progress_info.get('percent', 0),
        "speed": speed_str,
        "downloaded_bytes": progress_info.get('downloaded_bytes', 0),
        "total_bytes": progress_info.get('total_bytes', 0),
        "downloaded_size": format_size(progress_info.get('downloaded_bytes', 0)),
        "total_size": format_size(progress_info.get('total_bytes', 0)),
        "eta": progress_info.get('eta'),
        "filename": status.get('filename'),
        "message": status.get('error') if status.get('status') == 'error' else None,
        "phase": progress_info.get('phase', 'downloading'),  # Add phase info
        "current_time": progress_info.get('current_time'),  # Add transcoding current time
        "total_time": progress_info.get('total_time')  # Add transcoding total time
    }

    # Update history if completed or error
    if status.get('status') in ['completed', 'error']:
        updates = {'status': status.get('status')}
        if status.get('status') == 'completed':
            updates['file_path'] = status.get('filepath')
            if status.get('filepath') and os.path.exists(status.get('filepath')):
                updates['file_size'] = os.path.getsize(status.get('filepath'))
        elif status.get('status') == 'error':
            updates['error_message'] = status.get('error', 'Unknown error')
        history_manager.update_entry(task_id, updates)

    return response


@app.get("/api/history")
async def get_history():
    """获取下载历史"""
    return history_manager.get_all()


@app.delete("/api/history/{task_id}")
async def delete_history(task_id: str):
    """删除历史记录和相关文件"""
    # 查找要删除的记录
    entry_to_delete = history_manager.get_entry(task_id)

    if entry_to_delete:
        # Check if task is currently transcoding and cancel it
        status = downloader.get_download_status(task_id)
        if status and (status.get('status') == 'transcoding' or
                      (status.get('progress', {}).get('phase') == 'transcoding')):
            # Cancel the transcoding process and delete original file
            print(f"Cancelling active transcoding for task {task_id}")
            await downloader.transcoder.cancel_transcode(task_id, delete_input=True)

        # 删除文件
        if entry_to_delete.get('file_path'):
            file_path = entry_to_delete['file_path']
            # 如果是相对路径，转换为绝对路径
            if not os.path.isabs(file_path):
                file_path = os.path.join(os.path.dirname(__file__), file_path.lstrip('/'))

            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    print(f"Deleted file: {file_path}")
                except Exception as e:
                    print(f"Error deleting file: {e}")

        # 从历史中删除
        history_manager.delete_entry(task_id)

        # 清理下载器中的任务
        downloader.cleanup_task(task_id)

        return {"message": "History entry and file deleted"}
    else:
        raise HTTPException(status_code=404, detail="History entry not found")


@app.post("/api/redownload/{task_id}")
async def redownload_video(task_id: str):
    """重新下载视频（删除原文件并重新下载）"""
    # 查找原始下载记录
    entry = history_manager.get_entry(task_id)

    if not entry:
        raise HTTPException(status_code=404, detail="Download record not found")

    # 获取原始URL
    original_url = entry.get('url')
    if not original_url:
        raise HTTPException(status_code=400, detail="Original URL not found in history")

    # 删除原文件
    if entry.get('file_path'):
        file_path = entry['file_path']
        # 如果是相对路径，转换为绝对路径
        if not os.path.isabs(file_path):
            file_path = os.path.join(os.path.dirname(__file__), file_path.lstrip('/'))

        if os.path.exists(file_path):
            try:
                os.remove(file_path)
                print(f"Deleted old file for redownload: {file_path}")
            except Exception as e:
                print(f"Error deleting old file: {e}")

    # 清理旧的下载任务
    downloader.cleanup_task(task_id)

    # 从历史中删除旧记录
    history_manager.delete_entry(task_id)

    # 开始新的下载
    try:
        # Generate new task_id
        import uuid
        new_task_id = str(uuid.uuid4())

        # Get video info BEFORE starting download
        info = await downloader.get_video_info(original_url)

        # Add to history with correct info
        history_entry = {
            "id": new_task_id,
            "url": original_url,
            "title": info.get("title", "Unknown"),
            "thumbnail": info.get("thumbnail"),
            "uploader": info.get("uploader"),
            "downloaded_at": datetime.now().isoformat(),
            "status": "downloading",
            "file_path": None,
            "file_size": None
        }
        history_manager.add_entry(history_entry)

        # Start download with new task_id
        actual_task_id = await downloader.download_video_with_id(original_url, new_task_id)

        # Start monitoring task for completion
        import asyncio
        asyncio.create_task(monitor_web_download(
            task_id=new_task_id,
            title=info.get("title", "Unknown"),
            url=original_url,
            video_info=info
        ))

        return {"task_id": new_task_id, "message": "Redownload started", "original_task_id": task_id}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/proxy-thumbnail")
async def proxy_thumbnail(url: str):
    """代理YouTube缩略图"""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url)
            response.raise_for_status()

            # 获取内容类型
            content_type = response.headers.get("content-type", "image/jpeg")

            return StreamingResponse(
                iter([response.content]),
                media_type=content_type,
                headers={
                    "Cache-Control": "public, max-age=3600",
                    "Access-Control-Allow-Origin": "*"
                }
            )
    except Exception as e:
        logger.error(f"Error proxying thumbnail {url}: {e}")
        raise HTTPException(status_code=404, detail="Unable to fetch thumbnail")


@app.websocket("/ws/progress")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket连接用于实时进度更新"""
    settings = _load_auth_settings()
    ws_token = websocket.query_params.get("token")
    token_ok = bool(ws_token) and hmac.compare_digest(ws_token, settings["api_token"])
    session_ok = _verify_session_cookie(websocket.cookies.get(AUTH_COOKIE_NAME))
    if not (token_ok or session_ok):
        await websocket.close(code=1008)
        return

    await websocket.accept()
    active_connections.append(websocket)

    try:
        while True:
            # Wait for any message from client
            data = await websocket.receive_text()

            # Send current status of all active downloads
            active_tasks = []
            for task_id, status in downloader.active_downloads.items():
                progress_info = status.get('progress', {})
                active_tasks.append({
                    "task_id": task_id,
                    "status": status.get('status'),
                    "progress": progress_info.get('percent', 0)
                })

            await websocket.send_json({"active_tasks": active_tasks})
    except Exception:
        pass
    finally:
        active_connections.remove(websocket)


@app.get("/api/download-file/{task_id}")
async def download_file(task_id: str):
    """下载文件到客户端"""
    # 首先尝试从active downloads获取
    status = downloader.get_download_status(task_id)

    if status and status.get('status') == 'completed':
        filepath = status.get('filepath')
    else:
        # 如果不在active downloads，从历史记录中查找
        filepath = None
        entry = history_manager.get_entry(task_id)
        if entry:
            filepath = entry.get('file_path')

        if not filepath:
            raise HTTPException(status_code=404, detail="File not found in history")

    # 处理文件路径
    if not os.path.isabs(filepath):
        # 如果是相对路径，转换为绝对路径
        filepath = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "downloads", os.path.basename(filepath)))

    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail=f"File not found: {filepath}")

    return FileResponse(
        path=filepath,
        filename=os.path.basename(filepath),
        media_type='application/octet-stream'
    )


@app.get("/api/stream/{task_id}")
async def stream_video(task_id: str):
    """流式播放视频"""
    # 查找文件路径
    filepath = None

    # 从active downloads查找
    status = downloader.get_download_status(task_id)
    if status and status.get('status') == 'completed':
        filepath = status.get('filepath')

    # 从历史记录中查找
    if not filepath:
        entry = history_manager.get_entry(task_id)
        if entry:
            filepath = entry.get('file_path')

    if not filepath:
        raise HTTPException(status_code=404, detail="Video not found")

    # 处理文件路径
    if not os.path.isabs(filepath):
        filepath = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "downloads", os.path.basename(filepath)))

    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail=f"Video file not found: {filepath}")

    # 获取文件大小
    file_size = os.path.getsize(filepath)

    def iterfile():
        with open(filepath, 'rb') as f:
            while chunk := f.read(1024 * 1024):  # 1MB chunks
                yield chunk

    # 根据文件扩展名设置正确的媒体类型
    ext = os.path.splitext(filepath)[1].lower()
    media_type = 'video/mp4' if ext == '.mp4' else 'video/webm' if ext == '.webm' else 'application/octet-stream'

    return StreamingResponse(
        iterfile(),
        media_type=media_type,
        headers={
            "Content-Length": str(file_size),
            "Accept-Ranges": "bytes",
        }
    )


@app.get("/api/config")
async def get_config():
    """获取配置"""
    return config.config


@app.get("/api/version")
async def get_version():
    """获取版本信息"""
    import yt_dlp
    import sys
    return {
        "app_version": __version__,
        "yt_dlp_version": yt_dlp.version.__version__,
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    }


@app.get("/api/yt-dlp/check-update")
async def check_yt_dlp_update():
    """检查yt-dlp更新"""
    return await updater.check_for_updates()


@app.post("/api/yt-dlp/update")
async def update_yt_dlp():
    """更新yt-dlp到最新版本"""
    global updater
    result = updater.update_yt_dlp()
    if result["success"]:
        # Reinitialize updater with new version
        updater = YtDlpUpdater()
    return result


@app.get("/api/yt-dlp/version-info")
async def get_yt_dlp_version_info():
    """获取详细的版本信息"""
    return await updater.get_version_info()


@app.get("/api/browser-cookies/detect")
async def detect_browsers():
    """检测可用的浏览器"""
    browser_cookie_supported = not (cookie_extractor.is_docker and not cookie_extractor.cookie_bridge_url)
    return {
        "supported": browser_cookie_supported,
        "reason": None if browser_cookie_supported else "Docker/NAS 环境无法直接读取你电脑浏览器里的 Cookies",
        "available_browsers": cookie_extractor.detect_available_browsers(),
        "system_info": cookie_extractor.get_system_info()
    }


@app.post("/api/browser-cookies/import")
async def import_browser_cookies(request: dict):
    """从浏览器导入cookies"""
    browser = request.get('browser', 'firefox')
    domain = request.get('domain', 'youtube.com')

    if cookie_extractor.is_docker and not cookie_extractor.cookie_bridge_url:
        return {
            "success": False,
            "message": "Docker/NAS 环境无法直接读取你电脑浏览器里的 Cookies",
            "error": "请使用手动上传 cookies.txt，或配置 CookieCloud 同步。"
        }

    result = cookie_extractor.extract_cookies_from_browser(browser, domain)

    if result:
        # Save to config
        config.update_config({
            "browser_cookies": {
                "enabled": True,
                "browser": browser,
                "auto_refresh": True,
                "refresh_interval_minutes": 25
            }
        })

        return {
            "success": True,
            "message": f"Successfully imported cookies from {browser}",
            "data": {
                "browser": result['browser'],
                "extracted_at": result['extracted_at'],
                "cookie_count": len(result['cookies'].split('\n')) - 1  # Exclude header
            }
        }
    else:
        return {
            "success": False,
            "message": f"Failed to import cookies from {browser}",
            "error": "Could not extract cookies. Make sure the browser is installed and has active YouTube session."
        }


@app.get("/api/browser-cookies/status")
async def get_browser_cookie_status():
    """获取浏览器Cookie状态"""
    browser_config = config.config.get('browser_cookies', {})
    browser_cookie_supported = not (cookie_extractor.is_docker and not cookie_extractor.cookie_bridge_url)

    # Check if cookies exist
    cookies_exist = os.path.exists(cookie_extractor.cookies_file)
    cookies_fresh = False
    cookies_age = None

    if cookies_exist and cookie_extractor.last_extraction_time:
        from datetime import datetime
        age = datetime.now() - cookie_extractor.last_extraction_time
        cookies_age = str(age)
        cookies_fresh = age.total_seconds() < (25 * 60)  # Less than 25 minutes

    return {
        "supported": browser_cookie_supported,
        "reason": None if browser_cookie_supported else "Docker/NAS 环境无法直接读取你电脑浏览器里的 Cookies，请使用手动上传 cookies.txt 或 CookieCloud。",
        "enabled": browser_config.get('enabled', False),
        "browser": browser_config.get('browser', 'firefox'),
        "auto_refresh": browser_config.get('auto_refresh', True),
        "cookies_exist": cookies_exist,
        "cookies_fresh": cookies_fresh,
        "cookies_age": cookies_age,
        "last_extraction": cookie_extractor.last_extraction_time.isoformat() if cookie_extractor.last_extraction_time else None
    }


@app.post("/api/browser-cookies/refresh")
async def refresh_browser_cookies():
    """手动刷新浏览器cookies"""
    browser_config = config.config.get('browser_cookies', {})
    browser = browser_config.get('browser', 'firefox')

    # Force refresh
    cookie_extractor.last_extraction_time = None
    result = cookie_extractor.extract_cookies_from_browser(browser)

    if result:
        return {
            "success": True,
            "message": "Cookies refreshed successfully",
            "data": {
                "browser": result['browser'],
                "extracted_at": result['extracted_at']
            }
        }
    else:
        return {
            "success": False,
            "message": "Failed to refresh cookies"
        }


@app.post("/api/config")
async def update_config(updates: dict):
    """更新配置"""
    if config.update_config(updates):
        # 不要重新加载配置，直接使用更新后的配置
        downloader.config = config
        # 更新转码器配置
        downloader.transcoder.config = config.config
        return {"message": "Config updated successfully"}
    else:
        raise HTTPException(status_code=500, detail="Failed to update config")


async def monitor_web_download(task_id: str, title: str, url: str, video_info: dict, format_id: str = None, source: str = "Web") -> None:
    """Monitor Web download task and send completion/error notifications."""
    import asyncio

    try:
        while True:
            await asyncio.sleep(5)
            status = downloader.get_download_status(task_id)

            if not status:
                continue

            current = status.get("status")

            if current == "completed":
                filepath = status.get("filepath")
                if filepath and os.path.exists(filepath):
                    try:
                        video_info["filesize"] = os.path.getsize(filepath)
                    except OSError:
                        pass

                if status.get("video_info"):
                    actual_info = status.get("video_info")
                    history_manager.update_entry(task_id, {
                        "status": "completed",
                        "title": actual_info.get("title", title),
                        "thumbnail": actual_info.get("thumbnail"),
                        "uploader": actual_info.get("uploader"),
                        "file_path": filepath,
                        "file_size": actual_info.get("filesize") or video_info.get("filesize")
                    })
                else:
                    history_manager.update_entry(task_id, {
                        "status": "completed",
                        "file_path": filepath,
                        "file_size": video_info.get("filesize")
                    })

                telegram_config = config.get_telegram_config()
                public_url = telegram_config.get("public_base_url", "").rstrip("/")
                download_link = f"{public_url}/api/download-file/{task_id}" if public_url else None

                await notify_telegram(
                    task_id=task_id,
                    title=title,
                    url=url,
                    source=source,
                    video_info=video_info,
                    status="completed",
                    download_link=download_link,
                    format_id=format_id,
                    file_path=_display_download_path(filepath)
                )
                break

            if current == "error":
                error_msg = status.get("error", "Unknown error")
                history_manager.update_entry(task_id, {
                    "status": "error",
                    "error_message": error_msg
                })

                await notify_telegram(
                    task_id=task_id,
                    title=title,
                    url=url,
                    source=source,
                    video_info=video_info,
                    status="error",
                    error_message=error_msg,
                    format_id=format_id
                )
                break

    except Exception as e:
        logger.error(f"Error monitoring web download {task_id}: {e}")
    finally:
        downloader.cleanup_task(task_id)


async def notify_telegram(
    task_id: str,
    title: str,
    url: str,
    source: str = "Web",
    video_info: dict = None,
    status: str = "started",
    error_message: str = None,
    download_link: str = None,
    format_id: str = None,
    file_path: str = None
) -> None:
    """Send Telegram notifications for Web download tasks."""
    await telegram_service.send_download_notification(
        task_id=task_id,
        title=title,
        url=url,
        source=source,
        video_info=video_info,
        status=status,
        error_message=error_message,
        download_link=download_link,
        format_id=format_id,
        file_path=file_path,
    )


@app.post("/api/telegram/test")
async def test_telegram_notification():
    """Send a Telegram test message with the current saved settings."""
    if not telegram_service.is_configured():
        return {"success": False, "message": "Please enable Telegram and fill in Bot Token and Chat ID"}

    success = await telegram_service.send_test()
    if success:
        return {"success": True, "message": "Telegram test message sent"}
    return {"success": False, "message": "Telegram test failed. Check Bot Token, Chat ID, or network access."}


@app.get("/api/config/cookies")
async def get_cookies():
    """获取cookies内容"""
    try:
        # Use Docker-compatible path
        cookies_file = "/app/config/cookies.txt" if os.path.exists("/app") else os.path.join("config", "cookies.txt")
        if os.path.exists(cookies_file):
            with open(cookies_file, 'r', encoding='utf-8') as f:
                content = f.read()
            return {"content": content}
        else:
            return {"content": ""}
    except Exception as e:
        logger.error(f"Error getting cookies: {e}")
        return {"content": ""}

@app.post("/api/config/cookies")
async def upload_cookies(data: dict):
    """上传cookies内容"""
    try:
        content = data.get('content', '')
        if not content:
            raise HTTPException(status_code=400, detail="No cookies content provided")

        # Use Docker-compatible path
        if os.path.exists("/app"):
            cookies_file = "/app/config/cookies.txt"
            config_dir = "/app/config"
        else:
            cookies_file = os.path.join("config", "cookies.txt")
            config_dir = "config"

        # 确保config目录存在
        os.makedirs(config_dir, exist_ok=True)

        with open(cookies_file, 'w', encoding='utf-8') as f:
            f.write(content)

        # 重新加载下载器配置以应用新的cookies
        downloader.config = Config()

        return {"message": "Cookies uploaded successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/cookiecloud/status")
async def get_cookiecloud_status():
    """获取CookieCloud配置状态"""
    cookiecloud_config = config.get_cookiecloud_config()

    # Test connection if configured
    if cookiecloud_config.get('enabled'):
        success, message = cookie_extractor.test_cookiecloud_connection()
        return {
            "enabled": True,
            "configured": bool(cookiecloud_config.get('server_url')),
            "server_url": cookiecloud_config.get('server_url', ''),
            "auto_sync": cookiecloud_config.get('auto_sync', True),
            "sync_interval_minutes": cookiecloud_config.get('sync_interval_minutes', 30),
            "connection_status": success,
            "connection_message": message
        }
    else:
        return {
            "enabled": False,
            "configured": False,
            "server_url": "",
            "auto_sync": False,
            "sync_interval_minutes": 30,
            "connection_status": False,
            "connection_message": "CookieCloud is not enabled"
        }


@app.post("/api/cookiecloud/sync")
async def sync_cookiecloud():
    """手动触发CookieCloud同步"""
    success, message = cookie_extractor.sync_cookiecloud()

    if success:
        # 重新加载下载器配置以应用新的cookies
        downloader.config = Config()
        return {"success": True, "message": message}
    else:
        raise HTTPException(status_code=500, detail=message)


@app.post("/api/cookiecloud/config")
async def update_cookiecloud_config(data: dict):
    """更新CookieCloud配置"""
    try:
        # Update configuration
        config.update_config({
            "cookiecloud": {
                "enabled": data.get('enabled', False),
                "server_url": data.get('server_url', ''),
                "uuid_key": data.get('uuid_key', ''),
                "password": data.get('password', ''),
                "auto_sync": data.get('auto_sync', True),
                "sync_interval_minutes": data.get('sync_interval_minutes', 30)
            }
        })

        # Re-initialize cookie extractor with new config
        global cookie_extractor
        cookie_extractor = BrowserCookieExtractor(cookiecloud_config=config.get_cookiecloud_config())

        # Test connection if enabled
        if data.get('enabled'):
            success, message = cookie_extractor.test_cookiecloud_connection()
            return {
                "message": "Configuration updated",
                "connection_test": {
                    "success": success,
                    "message": message
                }
            }
        else:
            return {"message": "CookieCloud disabled"}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/cookiecloud/test")
async def test_cookiecloud_connection(data: dict):
    """测试CookieCloud连接"""
    from ytb.cookiecloud import CookieCloud

    # Create temporary CookieCloud instance for testing
    test_config = {
        'server_url': data.get('server_url', ''),
        'uuid_key': data.get('uuid_key', ''),
        'password': data.get('password', '')
    }

    cookiecloud = CookieCloud(test_config)
    success, message = cookiecloud.test_connection()

    return {
        "success": success,
        "message": message
    }

# Serve frontend static files
frontend_dir = os.path.join(os.path.dirname(__file__), "frontend")
if os.path.exists(frontend_dir):
    app.mount("/static", StaticFiles(directory=frontend_dir), name="static")
    # Also serve CSS and JS files directly
    css_dir = os.path.join(frontend_dir, "css")
    js_dir = os.path.join(frontend_dir, "js")
    if os.path.exists(css_dir):
        app.mount("/css", StaticFiles(directory=css_dir), name="css")
    if os.path.exists(js_dir):
        app.mount("/js", StaticFiles(directory=js_dir), name="js")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=9832,
        reload=os.environ.get("DEV_RELOAD") == "1",
    )
