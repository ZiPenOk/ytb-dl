import subprocess
import sys
import json
import logging
import os
from typing import Dict, Any, Optional
import httpx
import yt_dlp
from packaging import version

logger = logging.getLogger(__name__)


def get_external_package_dir() -> Optional[str]:
    """Return the persistent package dir used for Docker-side hot updates."""
    configured = os.environ.get("YTDLP_UPDATE_DIR")
    if configured:
        return configured
    if os.path.exists("/app"):
        return "/app/config/python-packages"
    return None


def ensure_external_package_dir() -> Optional[str]:
    """Add the persistent package dir to sys.path before site-packages."""
    package_dir = get_external_package_dir()
    if package_dir and package_dir not in sys.path:
        sys.path.insert(0, package_dir)
    return package_dir


ensure_external_package_dir()


class YtDlpUpdater:
    """Handle yt-dlp version checking and updates"""

    GITHUB_API_URL = "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"

    def __init__(self):
        self.current_version = self._get_current_version()

    def _get_current_version(self) -> str:
        return yt_dlp.version.__version__

    async def check_for_updates(self) -> Dict[str, Any]:
        """Check if a new version of yt-dlp is available"""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(self.GITHUB_API_URL)
                response.raise_for_status()

                latest_release = response.json()
                latest_version = latest_release['tag_name']

                # Remove 'v' prefix if present
                if latest_version.startswith('v'):
                    latest_version = latest_version[1:]

                # Compare versions
                current_version = self._get_current_version()
                current = version.parse(current_version)
                latest = version.parse(latest_version)

                update_available = latest > current

                return {
                    "current_version": current_version,
                    "latest_version": latest_version,
                    "update_available": update_available,
                    "release_notes": latest_release.get('body', ''),
                    "release_date": latest_release.get('published_at', ''),
                    "download_url": latest_release.get('html_url', '')
                }

        except httpx.HTTPError as e:
            logger.error(f"Failed to check for updates: {e}")
            return {
                "current_version": self._get_current_version(),
                "error": f"Failed to check for updates: {str(e)}",
                "update_available": False
            }
        except Exception as e:
            logger.error(f"Unexpected error checking for updates: {e}")
            return {
                "current_version": self._get_current_version(),
                "error": f"Unexpected error: {str(e)}",
                "update_available": False
            }

    def update_yt_dlp(self) -> Dict[str, Any]:
        """Update yt-dlp to the latest version"""
        try:
            old_version = self._get_current_version()
            # Check if running in Docker
            is_docker = os.path.exists("/app")

            package_dir = ensure_external_package_dir()

            if is_docker:
                if not package_dir:
                    raise RuntimeError("Persistent Python package directory is not configured")

                os.makedirs(package_dir, exist_ok=True)
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "pip",
                        "install",
                        "--upgrade",
                        "--target",
                        package_dir,
                        "yt-dlp",
                        "yt-dlp-ejs",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=300
                )
            else:
                # Normal pip upgrade
                result = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"],
                    capture_output=True,
                    text=True,
                    timeout=300
                )

            if result.returncode == 0:
                new_version = self._reload_yt_dlp()

                return {
                    "success": True,
                    "message": "yt-dlp updated successfully",
                    "old_version": old_version,
                    "new_version": new_version,
                    "output": result.stdout,
                    "package_dir": package_dir,
                    "restart_recommended": is_docker
                }
            else:
                error_message = result.stderr
                if is_docker:
                    error_message += (
                        "\n\nDocker update target: "
                        f"{package_dir}. Make sure /app/config is writable."
                    )

                logger.error(f"Failed to update yt-dlp: {error_message}")

                return {
                    "success": False,
                    "message": "Failed to update yt-dlp",
                    "error": error_message,
                    "output": result.stdout,
                    "is_docker": is_docker
                }

        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "message": "Update operation timed out",
                "error": "The update process took too long and was terminated"
            }
        except Exception as e:
            logger.error(f"Failed to update yt-dlp: {e}")
            return {
                "success": False,
                "message": "Failed to update yt-dlp",
                "error": str(e)
            }

    def _reload_yt_dlp(self) -> str:
        """Reload yt-dlp from the newest available sys.path location."""
        import importlib

        ensure_external_package_dir()
        importlib.invalidate_caches()

        for module_name in list(sys.modules):
            if module_name == "yt_dlp" or module_name.startswith("yt_dlp."):
                del sys.modules[module_name]

        fresh_yt_dlp = importlib.import_module("yt_dlp")
        globals()["yt_dlp"] = fresh_yt_dlp

        try:
            import ytb.downloader as downloader_module
            downloader_module.yt_dlp = fresh_yt_dlp
        except Exception as e:
            logger.warning(f"Could not refresh downloader yt_dlp reference: {e}")

        self.current_version = fresh_yt_dlp.version.__version__
        return self.current_version

    async def get_version_info(self) -> Dict[str, Any]:
        """Get detailed version information"""
        try:
            # Get Python version
            python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

            # Get pip version
            pip_result = subprocess.run(
                [sys.executable, "-m", "pip", "--version"],
                capture_output=True,
                text=True
            )
            pip_version = pip_result.stdout.split()[1] if pip_result.returncode == 0 else "Unknown"

            # Check for updates
            update_info = await self.check_for_updates()

            return {
                "yt_dlp_version": self._get_current_version(),
                "python_version": python_version,
                "pip_version": pip_version,
                "update_info": update_info
            }

        except Exception as e:
            logger.error(f"Failed to get version info: {e}")
            return {
                "yt_dlp_version": self.current_version,
                "error": str(e)
            }
