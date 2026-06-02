import logging
import time
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)


class TelegramService:
    """Send download notifications through Telegram Bot API."""

    def __init__(self, config_manager):
        self.config_manager = config_manager
        self._last_poll_conflict_log_at = 0.0

    @property
    def config(self) -> Dict[str, Any]:
        return self.config_manager.get_telegram_config()

    def is_configured(self) -> bool:
        cfg = self.config
        return bool(cfg.get("enabled") and cfg.get("bot_token") and cfg.get("chat_id"))

    def bot_downloads_enabled(self) -> bool:
        cfg = self.config
        return bool(self.is_configured() and cfg.get("enable_bot_downloads"))

    def allowed_chat_id(self) -> str:
        return str(self.config.get("chat_id", "")).strip()

    async def send_message(self, text: str, chat_id: Optional[str] = None) -> bool:
        cfg = self.config
        bot_token = cfg.get("bot_token", "").strip()
        target_chat_id = str(chat_id if chat_id is not None else cfg.get("chat_id", "")).strip()
        if not bot_token or not target_chat_id:
            logger.info("Telegram notification skipped: bot token or chat id is empty")
            return False

        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": target_chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
            return True
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Failed to send Telegram notification: HTTP %s %s",
                exc.response.status_code,
                self._response_excerpt(exc.response),
            )
            return False
        except Exception as exc:
            logger.error("Failed to send Telegram notification: %s", type(exc).__name__)
            return False

    async def get_updates(self, offset: Optional[int] = None, timeout: int = 25) -> Optional[list]:
        cfg = self.config
        bot_token = cfg.get("bot_token", "").strip()
        if not bot_token:
            return None

        url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
        payload: Dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message", "channel_post"],
        }
        if offset is not None:
            payload["offset"] = offset

        try:
            async with httpx.AsyncClient(timeout=timeout + 10) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                data = response.json()
            if not data.get("ok"):
                logger.error("Telegram getUpdates returned not ok: %s", data)
                return None
            return data.get("result", [])
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 409:
                now = time.monotonic()
                if now - self._last_poll_conflict_log_at >= 60:
                    logger.warning(
                        "Telegram polling conflict: another getUpdates consumer is active for this bot token. "
                        "YTB-DL will keep retrying."
                    )
                    self._last_poll_conflict_log_at = now
                return None

            logger.error(
                "Failed to poll Telegram updates: HTTP %s %s",
                exc.response.status_code,
                self._response_excerpt(exc.response),
            )
            return None
        except Exception as exc:
            logger.error("Failed to poll Telegram updates: %s", type(exc).__name__)
            return None

    @staticmethod
    def _response_excerpt(response: httpx.Response) -> str:
        text = response.text.replace("\n", " ").strip()
        return text[:200]

    async def send_download_notification(
        self,
        *,
        task_id: str,
        title: str,
        url: str,
        status: str,
        source: str = "Web",
        video_info: Optional[Dict[str, Any]] = None,
        error_message: Optional[str] = None,
        download_link: Optional[str] = None,
        format_id: Optional[str] = None,
        file_path: Optional[str] = None,
    ) -> bool:
        cfg = self.config
        if not cfg.get("enabled"):
            return False
        if status == "started" and not cfg.get("notify_on_start", False):
            return False
        if status == "completed" and not cfg.get("notify_on_success", True):
            return False
        if status == "error" and not cfg.get("notify_on_error", True):
            return False

        text = self._format_download_message(
            task_id=task_id,
            title=title,
            url=url,
            status=status,
            source=source,
            video_info=video_info,
            error_message=error_message,
            download_link=download_link,
            format_id=format_id,
            file_path=file_path,
        )
        return await self.send_message(text)

    async def send_test(self) -> bool:
        return await self.send_message("🧪 YTB-DL 测试推送成功")

    def _format_download_message(
        self,
        *,
        task_id: str,
        title: str,
        url: str,
        status: str,
        source: str,
        video_info: Optional[Dict[str, Any]],
        error_message: Optional[str],
        download_link: Optional[str],
        format_id: Optional[str] = None,
        file_path: Optional[str] = None,
    ) -> str:
        title = title or "Unknown"
        uploader = (video_info or {}).get("uploader")
        quality = self._format_quality(video_info, format_id)
        file_size = self._format_file_size(video_info)
        duration = self._format_duration((video_info or {}).get("duration"))

        status_title = {
            "started": "🚀 YTB-DL 开始下载",
            "completed": "✅ YTB-DL 下载完成",
            "error": "❌ YTB-DL 下载失败",
        }.get(status, f"ℹ️ YTB-DL {status}")

        lines = [status_title, ""]
        lines.append(f"🎬 标题：{title}")
        if uploader:
            lines.append(f"👤 作者：{uploader}")
        if quality:
            lines.append(f"🎞️ 清晰度：{quality}")
        if file_size:
            lines.append(f"📦 大小：{file_size}")
        if duration:
            lines.append(f"⏱️ 时长：{duration}")

        if file_path:
            lines.extend(["", f"📁 文件：{file_path}"])
        elif download_link:
            lines.extend(["", f"📁 文件：{download_link}"])

        lines.append(f"🔗 链接：{url}")
        lines.append(f"🧾 任务：{task_id}")
        if error_message:
            lines.append(f"⚠️ 错误：{error_message}")

        return "\n".join(lines)

    @staticmethod
    def _format_quality(video_info: Optional[Dict[str, Any]], format_id: Optional[str]) -> Optional[str]:
        if not video_info:
            return None

        formats = video_info.get("formats") or []
        selected_id = None
        if format_id:
            selected_id = str(format_id).split("/", 1)[0].split("+", 1)[0]

        selected = None
        if selected_id and selected_id not in {"best", "bestvideo"}:
            selected = next((item for item in formats if str(item.get("format_id")) == selected_id), None)

        if not selected:
            return "自动最佳"

        resolution = selected.get("resolution")
        codec = TelegramService._friendly_codec(selected.get("vcodec"))
        if resolution and codec:
            return f"{resolution} / {codec}"
        return resolution or codec

    @staticmethod
    def _friendly_codec(codec: Optional[str]) -> Optional[str]:
        if not codec or codec == "none":
            return None
        codec_lower = codec.lower()
        if "av01" in codec_lower or "av1" in codec_lower:
            return "AV1"
        if "hev" in codec_lower or "h265" in codec_lower or "h.265" in codec_lower:
            return "HEVC"
        if "avc" in codec_lower or "h264" in codec_lower or "h.264" in codec_lower:
            return "H.264"
        if "vp9" in codec_lower:
            return "VP9"
        return codec.split(".")[0].upper()

    @staticmethod
    def _format_duration(seconds: Optional[int]) -> Optional[str]:
        if not isinstance(seconds, (int, float)) or seconds <= 0:
            return None
        total = int(seconds)
        hours, remainder = divmod(total, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes}:{secs:02d}"

    @staticmethod
    def _format_file_size(video_info: Optional[Dict[str, Any]]) -> Optional[str]:
        if not video_info:
            return None

        size = (
            video_info.get("filesize")
            or video_info.get("filesize_approx")
            or video_info.get("estimated_filesize")
        )
        if not isinstance(size, (int, float)) or size <= 0:
            return None

        units = ["B", "KB", "MB", "GB"]
        value = float(size)
        unit = 0
        while value >= 1024 and unit < len(units) - 1:
            value /= 1024
            unit += 1
        suffix = " (预估)" if video_info.get("estimated_filesize") and not video_info.get("filesize") else ""
        return f"{value:.1f} {units[unit]}{suffix}"
