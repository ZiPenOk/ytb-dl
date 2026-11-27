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
    PYPI_JSON_URL = "https://pypi.org/pypi/yt-dlp/json"

    def __init__(self):
        self.current_version = self._get_current_version()

    def _get_current_version(self) -> str:
        return yt_dlp.version.__version__

    async def check_for_updates(self) -> Dict[str, Any]:
        """Check PyPI for the latest yt-dlp release, falling back to GitHub."""
        errors = []
        async with httpx.AsyncClient(
            timeout=15.0,
            headers={"User-Agent": "ytb-dl-updater"},
            follow_redirects=True,
        ) as client:
            # PyPI is the same source the installer uses and has no strict
            # anonymous request limit, unlike the GitHub API.
            try:
                response = await client.get(self.PYPI_JSON_URL)
                response.raise_for_status()
                data = response.json()
                latest_version = str(data.get("info", {}).get("version", "")).strip()
                if not latest_version:
                    raise ValueError("PyPI response did not include a version")
                files = data.get("releases", {}).get(latest_version) or []
                release_date = files[0].get("upload_time_iso_8601", "") if files else ""
                return self._build_update_result(
                    latest_version,
                    "",
                    release_date,
                    f"https://pypi.org/project/yt-dlp/{latest_version}/",
                )
            except Exception as error:
                errors.append(f"PyPI: {error}")

            try:
                response = await client.get(self.GITHUB_API_URL)
                response.raise_for_status()
                latest_release = response.json()
                latest_version = latest_release.get("tag_name", "")
                if latest_version.startswith("v"):
                    latest_version = latest_version[1:]
                return self._build_update_result(
                    latest_version,
                    latest_release.get("body", ""),
                    latest_release.get("published_at", ""),
                    latest_release.get("html_url", ""),
                )
            except Exception as error:
                errors.append(f"GitHub: {error}")

        message = "; ".join(errors)
        logger.error(f"Failed to check for updates: {message}")
        return {
            "current_version": self._get_current_version(),
            "error": f"Failed to check for updates: {message}",
            "update_available": False,
        }

    def _build_update_result(
        self,
        latest_version: str,
        release_notes: str,
        release_date: str,
        download_url: str,
    ) -> Dict[str, Any]:
        current_version = self._get_current_version()
        return {
            "current_version": current_version,
            "latest_version": latest_version,
            "update_available": version.parse(latest_version) > version.parse(current_version),
            "release_notes": release_notes,
            "release_date": release_date,
            "download_url": download_url,
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
