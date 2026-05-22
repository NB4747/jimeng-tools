import logging
import os
import subprocess
import sys
import time

import httpx
from mcp.server.fastmcp import FastMCP

from jimeng_client import AuthRequiredException, JiMengClient, load_config
from jimeng_api import (
    JiMengAPIClient,
    JiMengAPIError,
    InsufficientCreditsError,
    ContentFilteredError,
)
from utils import download_image

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("jimeng_mcp")

mcp = FastMCP("jimeng_tools")
_config = load_config()

# Auth state populated by init_auth_and_chrome()
_auth_state: dict = {}

# ------------------------------------------------------------------
# Auth & Chrome auto-launch
# ------------------------------------------------------------------


def init_auth_and_chrome() -> None:
    """Determine authentication mode and ensure a browser is available.

    Two modes (checked in order):

    1. **Cookie injection mode** (JIMENG_COOKIE env var is set):
       - Headless browser, no window pops up.
       - The cookie value is injected into the browser context so
         jimeng treats the session as already logged-in.

    2. **Auto-hosted Chrome mode** (no JIMENG_COOKIE):
       - Checks whether Chrome is already listening on 9222.
       - If not, searches for chrome.exe in standard Windows paths
         and launches it with --remote-debugging-port=9222, using
         %LOCALAPPDATA%\\jimeng_mcp_chrome_profile as the user-data
         directory, and opens jimeng.jianying.com.
    """
    global _auth_state, _config

    cookie = os.environ.get("JIMENG_COOKIE", "").strip()
    if cookie:
        logger.info("JIMENG_COOKIE found — entering pure-headless cookie mode.")
        _auth_state = {"mode": "cookie", "cookie": cookie, "headless": True}
        _config["headless"] = True
        _config["cookie"] = cookie
        return

    # ---- Auto-hosted Chrome mode ----
    logger.info("No JIMENG_COOKIE; entering auto-hosted Chrome mode …")
    try:
        r = httpx.get("http://127.0.0.1:9222/json/version", timeout=2)
        if r.status_code == 200:
            logger.info("Chrome CDP already listening on 9222.")
            _auth_state = {"mode": "cdp", "headless": False}
            return
    except Exception:
        logger.info("No Chrome on 9222, attempting auto-launch …")

    # Find Chrome
    chrome_candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.join(
            os.environ.get("LOCALAPPDATA", ""),
            r"Google\Chrome\Application\chrome.exe",
        ),
        os.path.join(
            os.environ.get("PROGRAMFILES", r"C:\Program Files"),
            r"Google\Chrome\Application\chrome.exe",
        ),
    ]
    chrome_exe = None
    for path in chrome_candidates:
        if os.path.isfile(path):
            chrome_exe = path
            break
    if not chrome_exe:
        raise RuntimeError(
            "Cannot find Chrome. "
            "Set JIMENG_COOKIE for headless mode, or install Chrome."
        )

    user_data = os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
        "jimeng_mcp_chrome_profile",
    )
    logger.info("Launching: %s", chrome_exe)
    subprocess.Popen(
        [
            chrome_exe,
            "--remote-debugging-port=9222",
            f"--user-data-dir={user_data}",
            "https://jimeng.jianying.com/",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(2)
    logger.info("Chrome launched — please log in to jimeng if needed.")
    _auth_state = {"mode": "cdp", "headless": False}


# ------------------------------------------------------------------
# MCP Tool
# ------------------------------------------------------------------


@mcp.tool()
async def generate_game_asset(prompt: str, output_path: str | None = None) -> str:
    """Generate an AI image on jimeng.jianying.com（即梦AI）and download it.

    **When to call this tool (CRITICAL):**
    You should invoke this tool automatically whenever the user expresses
    intent to create, generate, or design any kind of image or visual
    asset.  Trigger phrases include (but are not limited to):

      - 画图 / 画画 / 画一个 / 帮我画 …
      - 生成图片 / 生成图像 / 生成一张 / 做一张图 …
      - 创作头像 / 设计头像 / 做一个头像 …
      - 游戏素材 / 游戏背景 / 游戏场景 / 游戏角色 …
      - UI 素材 / 图标 / 插画 / 海报 / 壁纸 …
      - game asset / sprite / background / character art …
      - cyberpunk / fantasy / sci-fi + 图 / 场景 …
      - 任何包含“图片”“图像”“素材”“背景”“头像”“海报”的请求

    **Parameters:**
        prompt (str):
            The image description in natural language.  You should
            write a detailed, vivid prompt that describes the subject,
            style, lighting, composition, colour palette, and mood.
            Write in the same language the user used for their request.
        output_path (str | None):
            Optional save path.  Defaults to ./downloads/<prompt>.png.

    **Returns:**
        A status message with the local file path and the source URL.
    """
    if not output_path:
        safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in prompt)
        safe_name = safe_name.strip()[:80] or "image"
        output_path = os.path.join(
            _config.get("default_output_dir", "./downloads"),
            safe_name + ".png",
        )

    cookie = _config.get("cookie")

    # ── Strategy 1: Direct API (fast, reliable, no browser) ──
    if cookie:
        try:
            logger.info("API mode: generating via direct REST API …")
            api_client = JiMengAPIClient(cookie=cookie)
            image_url = await api_client.generate(prompt)
            logger.info("API image URL: %s", image_url)

            success = await download_image(image_url, output_path)
            if not success:
                return f"【错误】图片下载失败，请检查网络后重试。图片 URL: {image_url}"

            return f"【成功】图片已生成并保存至: {output_path}\n图片 URL: {image_url}"

        except InsufficientCreditsError:
            return "【错误】即梦积分不足，请前往 https://jimeng.jianying.com 充值或领取每日积分。"
        except ContentFilteredError:
            return "【错误】提示词被内容过滤器拦截，请修改后重试。"
        except JiMengAPIError as e:
            logger.warning("API generation failed: %s — falling back to browser mode.", e)
            # Fall through to browser automation

    # ── Strategy 2: Browser automation (robust fallback) ──
    cdp_url = _config.get("cdp_url", "http://localhost:9222")
    task_timeout = _config.get("task_timeout", 60)
    headless = _config.get("headless", False)

    client = JiMengClient(
        cdp_url=cdp_url,
        api_url_patterns=_config.get("api_patterns"),
        task_timeout=task_timeout,
        poll_interval=_config.get("poll_interval", 1.0),
        headless=headless,
        cookie=cookie,
    )

    try:
        logger.info("Browser mode: generating via Playwright UI automation …")
        image_url = await client.generate(prompt)
        logger.info("Browser image URL: %s", image_url)

        success = await download_image(image_url, output_path)
        if not success:
            return f"【错误】图片下载失败，请检查网络后重试。图片 URL: {image_url}"

        return f"【成功】图片已生成并保存至: {output_path}\n图片 URL: {image_url}"

    except AuthRequiredException:
        return "【错误】您的宿主浏览器即梦登录状态已失效，请在 Chrome 中完成扫码登录后重试。"
    except RuntimeError as e:
        msg = str(e)
        if "Cannot connect to Chrome" in msg:
            return "【错误】无法连接到 Chrome，请确保已通过命令行开启 9222 端口。"
        return f"【错误】{msg}"
    except Exception as e:
        logger.exception("Unexpected error during image generation.")
        return f"【错误】发生未知异常: {e}"
    finally:
        await client.close()


@mcp.tool()
async def generate_video_asset(
    prompt: str,
    duration: int = 5,
    ratio: str = "16:9",
    resolution: str = "720p",
    output_path: str | None = None,
) -> str:
    """Generate an AI video on jimeng.jianying.com（即梦AI）and download it.

    **When to call this tool:**
    Use when the user asks for video generation — cutscenes, animated backgrounds,
    skill effect videos, motion graphics, short clips.

    Trigger phrases: 生成视频 / 做一段动画 / 过场 / 动态背景 / cutscene / animation

    Parameters:
        prompt: Video description in natural language.
        duration: 5 or 10 seconds (2.x models only 5 s).
        ratio: "16:9" / "9:16" / "1:1" / "4:3" / "3:4" / "21:9".
        resolution: "1080p" / "720p" / "480p".
        output_path: Save path. Defaults to ./downloads/<prompt>.mp4.
    """
    cookie = _config.get("cookie")
    if not cookie:
        return "【错误】视频生成需要 JIMENG_COOKIE 环境变量。请设置后重试。"

    if not output_path:
        safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in prompt)
        safe_name = safe_name.strip()[:60] or "video"
        output_path = os.path.join(
            _config.get("default_output_dir", "./downloads"),
            safe_name + ".mp4",
        )

    try:
        from jimeng_sdk import JimengClient as SDKClient
        sdk = SDKClient(cookie=cookie, model="3.0")
        task_id = await sdk.generate_video(
            prompt, ratio=ratio, resolution=resolution, duration=duration,
        )
        video_url = await sdk.poll_video_status(task_id, timeout=1200)
        await sdk.close()

        success = await download_image(video_url, output_path)
        if not success:
            return f"【错误】视频下载失败。URL: {video_url}"

        return f"【成功】视频已生成并保存至: {output_path}\n视频 URL: {video_url}"

    except Exception as e:
        logger.exception("Video generation failed.")
        return f"【错误】视频生成失败: {e}"


@mcp.tool()
async def generate_image_variation(
    prompt: str,
    reference_path: str,
    output_path: str | None = None,
) -> str:
    """Generate a new image based on a reference image (image-to-image/blend mode).

    Upload the reference, then use Jimeng blend mode to create a variation.

    Args:
        prompt: Description of the desired output.
        reference_path: Local file path or URL to the reference image.
        output_path: Save path. Defaults to ./downloads/<prompt>.png.
    """
    cookie = _config.get("cookie")
    if not cookie:
        return "【错误】需要 JIMENG_COOKIE。"

    if not output_path:
        safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in prompt)
        safe_name = safe_name.strip()[:60] or "variation"
        output_path = os.path.join(_config.get("default_output_dir", "./downloads"), safe_name + ".png")

    try:
        from jimeng_sdk import JimengClient as SDKClient
        sdk = SDKClient(cookie=cookie)
        task_id = await sdk.generate_image_to_image(prompt, reference_path)
        image_url = await sdk.poll_task_status(task_id, timeout=180)
        await sdk.close()

        success = await download_image(image_url, output_path)
        if not success:
            return f"【错误】图片下载失败。URL: {image_url}"
        return f"【成功】图片变体已保存至: {output_path}\n图片 URL: {image_url}"
    except Exception as e:
        logger.exception("Image variation failed.")
        return f"【错误】{e}"


@mcp.tool()
async def generate_video_with_frames(
    prompt: str,
    first_frame: str = "",
    end_frame: str = "",
    duration: int = 5,
    ratio: str = "16:9",
    output_path: str | None = None,
) -> str:
    """Generate a video with optional start/end frame images (image-to-video).

    Args:
        prompt: Video description.
        first_frame: Local path or URL for the starting frame image.
        end_frame: Local path or URL for the ending frame image.
        duration: 5 or 10 seconds.
        ratio: "16:9" / "9:16" / "1:1" / "4:3" / "3:4" / "21:9".
        output_path: Save path. Defaults to ./downloads/<prompt>.mp4.
    """
    cookie = _config.get("cookie")
    if not cookie:
        return "【错误】需要 JIMENG_COOKIE。"

    if not output_path:
        safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in prompt)
        safe_name = safe_name.strip()[:60] or "video"
        output_path = os.path.join(_config.get("default_output_dir", "./downloads"), safe_name + ".mp4")

    try:
        from jimeng_sdk import JimengClient as SDKClient
        sdk = SDKClient(cookie=cookie, model="3.0")
        task_id = await sdk.generate_video_with_frames(
            prompt, first_frame=first_frame, end_frame=end_frame,
            ratio=ratio, duration=duration,
        )
        video_url = await sdk.poll_video_status(task_id, timeout=1200)
        await sdk.close()

        success = await download_image(video_url, output_path)
        if not success:
            return f"【错误】视频下载失败。URL: {video_url}"
        return f"【成功】视频已保存至: {output_path}\n视频 URL: {video_url}"
    except Exception as e:
        logger.exception("Video with frames failed.")
        return f"【错误】{e}"


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------


def main():
    """Console-script entry point for the jimeng MCP server."""
    init_auth_and_chrome()
    mcp.run()


if __name__ == "__main__":
    main()
