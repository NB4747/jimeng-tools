import asyncio
import json
import logging
import os
import re
from typing import Optional

from playwright.async_api import async_playwright, Page

logger = logging.getLogger(__name__)


class AuthRequiredException(Exception):
    """Raised when the user is not logged in to jimeng.jianying.com."""


class JiMengClient:
    """Headless control of a Chrome instance via CDP for text-to-image
    generation on jimeng.jianying.com."""

    _JIMENG_DOMAIN = "jimeng.jianying.com"
    _AUTH_SELECTORS = [
        'text="登录"',
        'text="注册"',
        'text="请登录"',
        'button:has-text("登录")',
        'a:has-text("登录")',
    ]
    # ProseMirror rich-text editor
    _EDITOR_SELECTOR = ".prompt-editor-aDwTfA .tiptap.ProseMirror"
    # Generate button in toolbar (step 9)
    _GENERATE_SELECTOR = ".toolbar-actions-pDJQS6 button.lv-btn-primary"
    # Mode switch (step 1): .lv-select-view-value containing mode names
    _MODE_TARGET_TEXT = "图片生成"
    # Model selector after mode switch (step 3)
    _MODEL_SELECTOR = ".content-Rvn0mS .lv-select.lv-select-single"
    # Post-generation download (steps 10-13)
    _DOWNLOAD_ICON = ".operation-button-JLhdr3 .icon-sDXMeC"
    _HD_SWITCH = ".switch-row-FCPdlJ button.lv-switch"
    _DOWNLOAD_BTN = ".footer-gb5SFI button.lv-btn-primary"

    def __init__(
        self,
        cdp_url: str = "http://localhost:9222",
        api_url_patterns: Optional[list[str]] = None,
        task_timeout: float = 60.0,
        poll_interval: float = 1.0,
    ):
        self._cdp_url = cdp_url
        self._api_patterns = api_url_patterns or [
            r"api/v1/task",
            r"text2img/action",
            r"api/task",
            r"aigc_dream",
            r"aigc/v1",
            r"mweb/v1",
            r"dreamina",
        ]
        self._task_timeout = task_timeout
        self._poll_interval = poll_interval

        # Runtime state
        self._captured_image_url: Optional[str] = None
        self._url_event: Optional[asyncio.Event] = None
        self._browser = None
        self._context = None
        self._page: Optional[Page] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def generate(self, prompt: str) -> str:
        """Run the full text-to-image flow and return the image URL.

        Raises AuthRequiredException when the user needs to log in.
        """
        self._captured_image_url = None
        self._url_event = asyncio.Event()

        try:
            await self._connect()
            await self._navigate_to_jimeng()
            await self._check_auth()
            await self._attach_network_listener()
            await self._ensure_image_gen_mode()
            await self._ensure_model_selection()
            await self._fill_prompt(prompt)
            await self._click_generate()
            # Wait for generation to complete, then download via UI
            image_url = await self._wait_and_download()
            return image_url
        finally:
            self._url_event = None

    async def close(self):
        """Dispose Playwright resources (keep the browser open)."""
        if self._context:
            # We don't close the browser — the user owns the process.
            pass
        self._page = None
        self._context = None
        self._browser = None

    # ------------------------------------------------------------------
    # Internal: connection & navigation
    # ------------------------------------------------------------------

    async def _connect(self):
        logger.info("Connecting to Chrome CDP at %s", self._cdp_url)
        try:
            playwright = await async_playwright().start()
            self._browser = await playwright.chromium.connect_over_cdp(self._cdp_url)
        except Exception as exc:
            raise RuntimeError(
                "Cannot connect to Chrome at %s. "
                "Is it running with --remote-debugging-port?"
                % self._cdp_url
            ) from exc

    async def _navigate_to_jimeng(self):
        """Reuse an existing jimeng tab, navigating to the home page."""
        self._context = self._browser.contexts[0]
        for page in self._context.pages:
            if self._JIMENG_DOMAIN in (page.url or ""):
                logger.info("Reusing existing jimeng tab: %s", page.url)
                await page.bring_to_front()
                # Always navigate to home for a consistent start
                if "/ai-tool/home/" not in (page.url or ""):
                    await page.goto(
                        "https://jimeng.jianying.com/ai-tool/home/",
                        wait_until="domcontentloaded",
                    )
                    await page.wait_for_timeout(2000)
                self._page = page
                return

        logger.info("Opening new jimeng tab …")
        self._page = await self._context.new_page()
        await self._page.goto("https://jimeng.jianying.com/ai-tool/home/", wait_until="domcontentloaded")

    # ------------------------------------------------------------------
    # Internal: auth check
    # ------------------------------------------------------------------

    async def _check_auth(self):
        for selector in self._AUTH_SELECTORS:
            try:
                el = await self._page.wait_for_selector(
                    selector, timeout=3000, state="attached"
                )
                if el:
                    raise AuthRequiredException(
                        "登录状态已失效，请在 Chrome 中完成扫码登录后重试。"
                    )
            except AuthRequiredException:
                raise
            except Exception:
                continue

    # ------------------------------------------------------------------
    # Internal: network interception
    # ------------------------------------------------------------------

    async def _attach_network_listener(self):
        compiled = [re.compile(p, re.IGNORECASE) for p in self._api_patterns]

        async def _on_response(response):
            url = response.url
            # Quick filter: skip obvious static resources
            if any(x in url for x in (".js", ".css", ".svg", ".ico", ".woff", ".png", ".jpg", ".webp")):
                return
            # Match API patterns OR look for JSON with dreamina in URL
            matches = any(p.search(url) for p in compiled)
            if not matches and "dreamina" not in url and "aigc" not in url:
                return
            try:
                body = await response.json()
            except Exception:
                return

            logger.info("Intercepted API response: %s", url)
            image_url = self._extract_image_url(body)
            if image_url:
                logger.info("Captured image URL via API: %s", image_url[:150])
                self._captured_image_url = image_url
                if self._url_event:
                    self._url_event.set()
            else:
                # Log the response body for debugging
                body_str = json.dumps(body, ensure_ascii=False)[:500]
                logger.debug("API response without image URL: %s", body_str)

        self._page.on("response", _on_response)

    @staticmethod
    def _extract_image_url(body) -> Optional[str]:
        """Walk common JSON shapes to find an image URL."""
        candidates = [
            # data.image_url
            (lambda b: isinstance(b, dict) and b.get("data", {}).get("image_url")),
            # data.result.image_url
            (lambda b: isinstance(b, dict)
             and isinstance(b.get("data"), dict)
             and b["data"].get("result", {}).get("image_url")),
            # response.items[0].url
            (lambda b: isinstance(b, dict)
             and isinstance(b.get("response"), dict)
             and isinstance(b["response"].get("items"), list)
             and len(b["response"]["items"]) > 0
             and b["response"]["items"][0].get("url")),
            # data.images[0].url
            (lambda b: isinstance(b, dict)
             and isinstance(b.get("data"), dict)
             and isinstance(b["data"].get("images"), list)
             and len(b["data"]["images"]) > 0
             and b["data"]["images"][0].get("url")),
            # bare "url" key at any level (last resort)
            (lambda b: isinstance(b, dict) and b.get("url") if isinstance(b.get("url"), str) and b["url"].startswith("http") else None),
        ]

        for fn in candidates:
            try:
                result = fn(body)
                if result:
                    return result
            except Exception:
                continue

        # Deep walk: find all dreamina URLs, prefer original resolution
        def _deep_search(obj, depth=0, found=None):
            if found is None:
                found = []
            if depth > 10:
                return found
            if isinstance(obj, str):
                if ("dreamina-sign" in obj or ("byteimg.com" in obj and "tos-cn-i" in obj)) and obj.startswith("http"):
                    found.append(obj)
            if isinstance(obj, dict):
                for v in obj.values():
                    _deep_search(v, depth + 1, found)
            if isinstance(obj, list):
                for item in obj:
                    _deep_search(item, depth + 1, found)
            return found

        urls = _deep_search(body)
        if not urls:
            return None
        # Prefer original size (resize:0:0 or no resize suffix)
        for url in urls:
            if "resize:0:0" in url or "resize%3A0%3A0" in url:
                return url
        # Prefer larger thumbnails
        for url in urls:
            if "aigc_resize_loss" in url:
                return url  # lossless resize
        return urls[0]

    # ------------------------------------------------------------------
    # Internal: UI interaction
    # ------------------------------------------------------------------

    async def _ensure_image_gen_mode(self):
        """Step 1-2: Switch mode dropdown from Agent to 图片生成."""
        logger.info("Step 1-2: Checking mode selector …")
        # The mode switcher shows current mode text like "Agent 模式" or "图片生成"
        mode_values = self._page.locator(".lv-select-view-value")
        for i in range(await mode_values.count()):
            el = mode_values.nth(i)
            if not await el.is_visible():
                continue
            text = (await el.text_content() or "").strip()
            if not any(kw in text for kw in ("Agent", "图片生成", "视频生成")):
                continue
            if "图片生成" in text:
                logger.info("Already in 图片生成 mode.")
                return
            # Click to open dropdown and select 图片生成
            logger.info("Switching mode from '%s' to 图片生成 …", text)
            await el.click()
            await self._page.wait_for_timeout(800)
            # Step 2: click 图片生成 option (use .select-option-label-content)
            option = self._page.locator(".select-option-label-content").filter(has_text="图片生成").first
            await option.click()
            logger.info("Mode switched to 图片生成.")
            await self._page.wait_for_timeout(1000)
            return
        logger.info("Mode selector not found, proceeding anyway.")

    async def _ensure_model_selection(self):
        """Step 3-4: Select the image model (e.g. 图片5.0 Lite) if not already."""
        logger.info("Step 3-4: Checking model selection …")
        await self._page.wait_for_timeout(500)
        model_select = self._page.locator(self._MODEL_SELECTOR).nth(1)
        if not await model_select.is_visible():
            logger.info("Model selector not visible, skipping.")
            return
        await model_select.click()
        await self._page.wait_for_timeout(500)
        # Click the selected/first model option
        option = self._page.locator(".lv-select-option-wrapper-selected .select-option-label-content").first
        if not await option.is_visible():
            option = self._page.locator(".select-option-label-content").first
        await option.click()
        logger.info("Model selected.")
        await self._page.wait_for_timeout(500)

    async def _fill_prompt(self, prompt: str):
        """Step 5-8: Click the ProseMirror editor and type the prompt."""
        logger.info("Step 5-8: Filling prompt …")
        editor = self._page.locator(self._EDITOR_SELECTOR).first
        await editor.wait_for(state="visible", timeout=5000)
        # Click the empty placeholder to focus
        placeholder = editor.locator("p.is-editor-empty").first
        if await placeholder.is_visible():
            await placeholder.click()
        else:
            await editor.click()
        await self._page.wait_for_timeout(300)
        # Clear existing content and type
        await editor.press("Control+a")
        await editor.press("Backspace")
        await self._page.keyboard.type(prompt, delay=20)
        logger.info("Prompt filled.")

    async def _click_generate(self):
        """Step 9: Click the generate button in toolbar."""
        logger.info("Step 9: Clicking generate button …")
        btn = self._page.locator(self._GENERATE_SELECTOR).first
        # Wait for button to be enabled (not disabled by empty prompt)
        for _ in range(30):
            disabled = await btn.get_attribute("disabled")
            if disabled is None:
                break
            await self._page.wait_for_timeout(500)
        await btn.click()
        logger.info("Generate button clicked.")

    # ------------------------------------------------------------------
    # Internal: post-generation download (steps 10-13)
    # ------------------------------------------------------------------

    async def _wait_and_download(self) -> str:
        """Wait for generation, then click through download UI to get image URL."""
        logger.info("Waiting for generation to complete …")
        # Wait for page to navigate to generate workspace
        for _ in range(60):
            if "/ai-tool/generate" in (self._page.url or ""):
                break
            await self._page.wait_for_timeout(1000)
        # Let result images render
        await self._page.wait_for_timeout(3000)

        # Try network interception URL first
        if self._captured_image_url:
            logger.info("Got URL from network interception: %s", self._captured_image_url[:120])
            return self._captured_image_url

        # Fall back: click download icon → HD toggle → download button
        logger.info("Trying UI download flow …")
        return await self._ui_download()

    async def _ui_download(self) -> str:
        """Steps 10-13: Click download icon, toggle HD, click download."""
        # Step 10: Click download icon on the first result card
        icon = self._page.locator(self._DOWNLOAD_ICON).first
        if await icon.is_visible():
            await icon.click()
            logger.info("Step 10: Download icon clicked.")
            await self._page.wait_for_timeout(800)
        else:
            logger.warning("Download icon not visible.")

        # Step 11: Toggle HD switch if present
        hd_switch = self._page.locator(self._HD_SWITCH).first
        if await hd_switch.is_visible():
            # Check if it's already on; if not, toggle it
            checked = await hd_switch.get_attribute("aria-checked")
            if checked != "true":
                await hd_switch.click()
                logger.info("Step 11: HD switch toggled.")
                await self._page.wait_for_timeout(300)
        else:
            logger.info("Step 11: HD switch not present, skipping.")

        # Set up a one-shot network listener for the actual download
        download_url_event = asyncio.Event()
        download_url = []

        async def _capture_download(response):
            url = response.url
            # Look for image download responses
            if any(x in url for x in ("dreamina-sign", "byteimg.com")) and \
               any(x in url for x in ("resize", ".image", ".png", ".jpeg", ".webp")):
                download_url.append(url)
                download_url_event.set()

        self._page.on("response", _capture_download)

        # Step 12: Click final download button
        download_btn = self._page.locator(self._DOWNLOAD_BTN).first
        if await download_btn.is_visible():
            await download_btn.click()
            logger.info("Step 12: Download button clicked.")
        else:
            logger.warning("Download button not visible.")

        # Wait for download URL capture
        try:
            await asyncio.wait_for(download_url_event.wait(), timeout=10)
        except asyncio.TimeoutError:
            pass

        if download_url:
            url = download_url[0]
            logger.info("Captured download URL: %s", url[:120])
            return url

        # Last resort: extract from DOM
        logger.info("Trying DOM extraction as last resort …")
        img_urls = await self._page.eval_on_selector_all(
            "img[src*='dreamina-sign']",
            "els => els.filter(el => el.naturalWidth > 200).map(el => el.src)"
        )
        if img_urls:
            for url in img_urls:
                if "resize:0:0" in url:
                    return url
            return img_urls[0]

        raise RuntimeError("Failed to get image URL from any source.")


# ------------------------------------------------------------------
# Convenience: load config from json
# ------------------------------------------------------------------

def load_config(config_path: str = "config.json") -> dict:
    """Load config.json from the project root (relative to this file)."""
    if not os.path.isabs(config_path):
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        config_path = os.path.join(base, config_path)
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)
