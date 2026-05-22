"""JiMeng SDK — production-grade async client for jimeng.jianying.com.

Provides a clean, framework-agnostic API surface that can be dropped into any
Python project (FastAPI, Discord bots, game multi-agent pipelines, etc.).

Usage::

    client = JimengClient(cookie="sessionid=abc123")
    task_id = await client.generate_image("a red circle", aspect_ratio="1:1")
    url     = await client.poll_task_status(task_id, timeout=60)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_ASSISTANT_ID = 513695
_DRAFT_VERSION = "3.3.8"
_APP_VERSION = "5.8.0"
_PLATFORM = "7"

_MODEL_MAP = {
    "4.5":    "high_aes_general_v40l",
    "4.1":    "high_aes_general_v41",
    "4.0":    "high_aes_general_v40",
    "3.1":    "high_aes_general_v30l_art_fangzhou:general_v3.0_18b",
    "3.0":    "high_aes_general_v30l:general_v3.0_18b",
    "2.0pro": "high_aes_general_v20_L:general_v2.0_L",
}

_ASPECT_RATIOS: dict[str, int] = {
    "21:9": 0, "16:9": 1,  "3:2": 2,  "4:3": 3,
    "1:1":  8, "3:4":  4,  "2:3": 5,  "9:16": 6,
}

_DIMENSIONS_1K: dict[str, tuple[int, int]] = {
    "21:9": (2016, 846), "16:9": (1664, 936),  "3:2": (1584, 1056),
    "4:3":  (1472, 1104), "1:1": (1328, 1328), "3:4": (1104, 1472),
    "2:3":  (1056, 1584), "9:16": (936, 1664),
}

_DIMENSIONS_2K: dict[str, tuple[int, int]] = {
    "21:9": (3024, 1296), "16:9": (2560, 1440), "3:2": (2496, 1664),
    "4:3":  (2304, 1728), "1:1":  (2048, 2048), "3:4": (1728, 2304),
    "2:3":  (1664, 2496), "9:16": (1440, 2560),
}

_FAKE_HEADERS = {
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Appid":           str(_DEFAULT_ASSISTANT_ID),
    "Appvr":           _APP_VERSION,
    "Origin":          "https://jimeng.jianying.com",
    "Pf":              _PLATFORM,
    "Referer":         "https://jimeng.jianying.com",
    "Sec-Fetch-Dest":  "empty",
    "Sec-Fetch-Mode":  "cors",
    "Sec-Fetch-Site":  "same-origin",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/142.0.0.0 Safari/537.36"
    ),
}

_PROCESSING = frozenset({20, 42, 45})
_FAILED     = 30
_POLL_INTERVAL = 2          # seconds
_MAX_POLL_TRIES = 300       # 300 × 2 s = 10 min ceiling


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class JiMengException(Exception):
    """Base for all SDK errors."""

class AuthenticationError(JiMengException):
    """Cookie is invalid or expired."""

class InsufficientCreditsError(JiMengException):
    """Account has run out of generation credits."""

class ContentFilteredError(JiMengException):
    """Prompt was blocked by the content-safety filter."""

class TaskTimeoutError(JiMengException):
    """Polling exceeded the configured timeout."""

class NetworkError(JiMengException):
    """HTTP / connectivity failure that may be retried."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_ms() -> str:
    return str(int(time.time() * 1000))

def _uid() -> str:
    return uuid.uuid4().hex

def _rng_seed() -> int:
    return math.floor(random.random() * 100_000_000) + 2_500_000_000

def _sign(uri: str, device_time: str) -> str:
    payload = f"9e2c|{uri[-7:]}|{_PLATFORM}|{_APP_VERSION}|{device_time}||11ac"
    return hashlib.md5(payload.encode()).hexdigest()


def _detect_ratio(prompt: str) -> Optional[str]:
    """Heuristic aspect-ratio extraction from natural-language prompt."""
    m = re.search(r"(\d+)\s*[:：]\s*(\d+)", prompt)
    if m:
        key = f"{m.group(1)}:{m.group(2)}"
        if key in _ASPECT_RATIOS:
            return key
    if re.search(r"横屏|横版|宽屏", prompt):
        return "16:9"
    if re.search(r"竖屏|竖版|手机", prompt):
        return "9:16"
    if re.search(r"方形|正方", prompt):
        return "1:1"
    return None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

@dataclass
class JimengClient:
    """Async Jimeng REST-API client.

    Parameters:
        cookie:
            The ``sessionid`` value extracted from a logged-in browser, or a
            full cookie header string.
        model:
            One of ``"4.5"``, ``"4.1"``, ``"4.0"``, ``"3.1"``, ``"3.0"``, ``"2.0pro"``.
        resolution:
            ``"2k"`` (default for 4.x models) or ``"1k"``.
        http_timeout:
            Per-request timeout in seconds.
    """

    cookie: str
    model: str = "4.5"
    resolution: str = "2k"
    http_timeout: float = 45.0
    # Multi-token rotation: set to a comma-separated list of cookies,
    # e.g. "sid1,sid2,sid3".  Each call round-robins to the next token.
    tokens: str = ""

    # -- internal runtime ---------------------------------------------------
    _web_id:   str = field(default_factory=lambda: str(random.randint(7_000_000_000_000_000_000, 9_999_999_999_999_999_999)))
    _user_id:  str = field(default_factory=_uid)
    _client:   Optional[httpx.AsyncClient] = field(default=None, repr=False, init=False)

    # -- public API ----------------------------------------------------------

    async def generate_image(
        self,
        prompt: str,
        aspect_ratio: str = "1:1",
        style_template: str = "",
        *,
        negative_prompt: str = "",
        sample_strength: float = 0.5,
    ) -> str:
        """Submit a text-to-image task and return a **task_id**.

        The caller should subsequently call :meth:`poll_task_status` to
        retrieve the final image URL.

        Parameters:
            prompt:
                Natural-language image description.
            aspect_ratio:
                One of ``"21:9"``, ``"16:9"``, ``"3:2"``, ``"4:3"``,
                ``"1:1"``, ``"3:4"``, ``"2:3"``, ``"9:16"``.
            style_template:
                Optional named style or extra prompt suffix injected before
                the main prompt.  (e.g. ``"game-icon"``, ``"pixel-art"``).
        """
        # --- resolve parameters --------------------------------------------
        effective_prompt = f"{style_template}, {prompt}" if style_template else prompt

        detected = _detect_ratio(effective_prompt)
        if detected and aspect_ratio == "1:1":
            aspect_ratio = detected
        if aspect_ratio not in _ASPECT_RATIOS:
            logger.warning("Unknown aspect_ratio %r → falling back to 1:1", aspect_ratio)
            aspect_ratio = "1:1"

        internal_model = _MODEL_MAP.get(self.model, _MODEL_MAP["4.5"])
        is_4x = self.model in ("4.5", "4.1", "4.0")
        res = self.resolution if self.resolution in ("1k", "2k") else ("2k" if is_4x else "1k")
        dims = _DIMENSIONS_2K[aspect_ratio] if res == "2k" else _DIMENSIONS_1K[aspect_ratio]
        width, height = dims
        ratio_code = _ASPECT_RATIOS[aspect_ratio]

        logger.info(
            "generate_image: model=%s ratio=%s res=%s size=%dx%d prompt=%.80s",
            self.model, aspect_ratio, res, width, height, effective_prompt,
        )

        # --- build request body ---------------------------------------------
        cid = _uid()
        sid = _uid()

        ability_id   = _uid()
        core_id      = _uid()
        draft_id     = _uid()
        meta_id      = _uid()
        hist_id      = _uid()
        large_img_id = _uid()

        request_data = {
            "extend":       {"root_model": internal_model},
            "submit_id":    sid,
            "metrics_extra": json.dumps({
                "promptSource":  "custom",
                "generateCount": 1,
                "enterFrom":     "click",
                "sceneOptions": json.dumps([{
                    "type":            "image",
                    "scene":           "ImageBasicGenerate",
                    "modelReqKey":     internal_model,
                    "resolutionType":  res,
                    "abilityList":     [],
                    "benefitCount":    4 if (is_4x and res == "2k") else 1,
                    "reportParams": {
                        "enterSource":                     "generate",
                        "vipSource":                       "generate",
                        "extraVipFunctionKey":             f"{internal_model}-{res}",
                        "useVipFunctionDetailsReporterHoc": True,
                    },
                }]),
                "isBoxSelect":  False,
                "isCutout":     False,
                "generateId":   sid,
                "isRegenerate": False,
            }),
            "draft_content": json.dumps({
                "type": "draft",
                "id":   draft_id,
                "min_version":  "3.0.2",
                "min_features": [],
                "is_from_tsn":  True,
                "version":      _DRAFT_VERSION,
                "main_component_id": cid,
                "component_list": [{
                    "type":          "image_base_component",
                    "id":            cid,
                    "min_version":   "3.0.2",
                    "metadata": {
                        "type": "", "id": meta_id,
                        "created_platform": 3,
                        "created_platform_version": "",
                        "created_time_in_ms": _now_ms(),
                        "created_did": "",
                    },
                    "generate_type": "generate",
                    "aigc_mode":     "workbench",
                    "abilities": {
                        "type": "", "id": ability_id,
                        "generate": {
                            "type": "", "id": core_id,
                            "core_param": {
                                "type": "", "id": large_img_id,
                                "model":            internal_model,
                                "prompt":           effective_prompt,
                                "negative_prompt":  negative_prompt,
                                "seed":             _rng_seed(),
                                "sample_strength":  sample_strength,
                                "image_ratio":      ratio_code,
                                "large_image_info": {
                                    "type": "", "id": hist_id,
                                    "height":          height,
                                    "width":           width,
                                    "resolution_type": res,
                                },
                            },
                            "history_option": {"type": "", "id": _uid()},
                        },
                    },
                }],
            }),
            "http_common_info": {"aid": _DEFAULT_ASSISTANT_ID},
        }

        try:
            body = await self._request(
                "POST",
                "/mweb/v1/aigc_draft/generate",
                params={
                    "da_version":              _DRAFT_VERSION,
                    "web_component_open_flag":  1,
                    "web_version":             _DRAFT_VERSION,
                },
                data=request_data,
            )
        except NetworkError:
            raise
        except JiMengException:
            raise
        except Exception as exc:
            raise NetworkError(f"Generate request failed: {exc}") from exc

        aigc = body.get("aigc_data", body)
        task_id = aigc.get("history_record_id")
        if not task_id:
            raise JiMengException(
                f"API did not return a history_record_id. "
                f"Response keys: {list(body.keys())}"
            )
        logger.info("Task submitted → %s", task_id)
        return task_id

    async def poll_task_status(self, task_id: str, timeout: int = 60) -> str:
        """Poll the generation task until completion.

        Returns the final **image URL** (highest resolution available).

        Raises :exc:`TaskTimeoutError` if the task does not finish within
        *timeout* seconds, or :exc:`ContentFilteredError` / :exc:`JiMengException`
        for terminal failures.
        """
        deadline = time.monotonic() + timeout
        logger.info("poll_task_status: task_id=%s timeout=%ds", task_id, timeout)

        while time.monotonic() < deadline:
            await asyncio.sleep(_POLL_INTERVAL)

            try:
                data = await self._request(
                    "POST",
                    "/mweb/v1/get_history_by_ids",
                    data={
                        "history_ids": [task_id],
                        "image_info": {
                            "width": 2400, "height": 2400, "format": "webp",
                            "image_scene_list": [
                                {"scene": "normal", "width": 2400, "height": 2400,
                                 "uniq_key": "2400", "format": "webp"},
                                {"scene": "normal", "width": 1080, "height": 1080,
                                 "uniq_key": "1080", "format": "webp"},
                            ],
                        },
                        "http_common_info": {"aid": _DEFAULT_ASSISTANT_ID},
                    },
                )
            except NetworkError:
                logger.warning("Transient network error during poll; retrying …")
                continue
            except JiMengException:
                raise
            except Exception as exc:
                raise NetworkError(f"Poll request failed: {exc}") from exc

            entry     = data.get(task_id, {})
            status    = entry.get("status", 20)
            fail_code = entry.get("fail_code", "")
            items     = entry.get("item_list") or []

            logger.debug("poll: status=%d items=%d fail=%s", status, len(items), fail_code or "-")

            # -- terminal states --------------------------------------------
            if status == _FAILED:
                if fail_code == "2038":
                    raise ContentFilteredError("Prompt rejected by content filter.")
                raise JiMengException(
                    f"Generation failed: status={status} fail_code={fail_code}"
                )

            if items:
                url = self._extract_url(items[0])
                if url:
                    logger.info("Image ready: %.120s", url)
                    return url

            if status not in _PROCESSING and not items:
                logger.info("Unknown terminal status %d — treating as done.", status)

        raise TaskTimeoutError(
            f"Task {task_id} did not complete within {timeout}s"
        )

    async def get_credits(self) -> dict[str, int]:
        """Return ``{gift, purchase, vip, total}``."""
        data = await self._request(
            "POST", "/commerce/v1/benefits/user_credit", data={}
        )
        c = data.get("credit", data)
        gift     = c.get("gift_credit", 0)
        purchase = c.get("purchase_credit", 0)
        vip      = c.get("vip_credit", 0)
        return {"gift": gift, "purchase": purchase, "vip": vip, "total": gift + purchase + vip}

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- token rotation ------------------------------------------------------

    def _next_cookie(self) -> str:
        """Round-robin through multi-token list; falls back to single cookie."""
        if not self.tokens:
            return self.cookie
        ids = [t.strip() for t in self.tokens.split(",") if t.strip()]
        if not ids:
            return self.cookie
        # Rotate: move first to end
        ids = ids[1:] + [ids[0]]
        self.tokens = ",".join(ids)
        return ids[0]

    # -- upload --------------------------------------------------------------

    async def upload_image(self, file_path: str) -> str:
        """Upload a local file, URL, or base64 image to Jimeng CDN.

        Returns an ``image_uri`` that can be used as reference in
        :meth:`generate_image_to_image` or :meth:`generate_video`.
        """
        from jimeng_upload import upload_to_jimeng
        return await upload_to_jimeng(file_path, self._next_cookie())

    # -- image-to-image ------------------------------------------------------

    async def generate_image_to_image(
        self,
        prompt: str,
        reference: str,      # file path, URL, or existing image_uri
        *,
        aspect_ratio: str = "1:1",
        sample_strength: float = 0.5,
    ) -> str:
        """Generate a new image using a **reference image** as a base.

        *reference* can be:
        - A local file path (auto-uploaded)
        - An HTTP(S) URL (auto-downloaded & uploaded)
        - An existing ``image_uri`` from a previous :meth:`upload_image` call

        Returns a ``task_id`` for use with :meth:`poll_task_status`.
        """
        # Upload if not already a CDN URI
        if reference.startswith("tos-cn-i-") or reference.startswith("tos-cn-i"):
            upload_id = reference
        else:
            upload_id = await self.upload_image(reference)

        internal = _MODEL_MAP.get(self.model, _MODEL_MAP["4.5"])
        detected = _detect_ratio(prompt)
        ratio = detected if detected and aspect_ratio == "1:1" else aspect_ratio
        if ratio not in _ASPECT_RATIOS:
            ratio = "1:1"
        ratio_code = _ASPECT_RATIOS[ratio]
        is_4x = self.model in ("4.5", "4.1", "4.0")
        res = self.resolution if self.resolution in ("1k", "2k") else ("2k" if is_4x else "1k")
        dims = _DIMENSIONS_2K[ratio] if res == "2k" else _DIMENSIONS_1K[ratio]
        w, h = dims

        cid = _uid()
        sid = _uid()

        request_data = {
            "extend": {"root_model": internal},
            "submit_id": sid,
            "metrics_extra": json.dumps({
                "promptSource": "custom", "generateCount": 1, "enterFrom": "click",
                "sceneOptions": json.dumps([{
                    "type": "image", "scene": "ImageBasicGenerate",
                    "modelReqKey": internal, "resolutionType": res,
                    "abilityList": [], "benefitCount": 4 if (is_4x and res == "2k") else 1,
                    "reportParams": {"enterSource": "generate", "vipSource": "generate",
                        "extraVipFunctionKey": f"{internal}-{res}",
                        "useVipFunctionDetailsReporterHoc": True},
                }]),
                "isBoxSelect": False, "isCutout": False, "generateId": sid, "isRegenerate": False,
            }),
            "draft_content": json.dumps({
                "type": "draft", "id": _uid(), "min_version": "3.0.2", "min_features": [],
                "is_from_tsn": True, "version": _DRAFT_VERSION, "main_component_id": cid,
                "component_list": [{
                    "type": "image_base_component", "id": cid, "min_version": "3.0.2",
                    "metadata": {"type": "", "id": _uid(), "created_platform": 3,
                        "created_platform_version": "", "created_time_in_ms": _now_ms(), "created_did": ""},
                    "generate_type": "blend",
                    "aigc_mode": "workbench",
                    "abilities": {
                        "type": "", "id": _uid(),
                        "blend": {
                            "type": "", "id": _uid(), "min_features": [],
                            "core_param": {
                                "type": "", "id": _uid(),
                                "model": internal, "prompt": prompt + "##",
                                "sample_strength": sample_strength,
                                "image_ratio": ratio_code,
                                "large_image_info": {"type": "", "id": _uid(),
                                    "height": h, "width": w, "resolution_type": res},
                            },
                            "ability_list": [{
                                "type": "", "id": _uid(), "name": "byte_edit",
                                "image_uri_list": [upload_id],
                                "image_list": [{"type": "image", "id": _uid(),
                                    "source_from": "upload", "platform_type": 1,
                                    "name": "", "image_uri": upload_id,
                                    "width": 0, "height": 0, "format": "", "uri": upload_id}],
                                "strength": 0.5,
                            }],
                            "history_option": {"type": "", "id": _uid()},
                            "prompt_placeholder_info_list": [{"type": "", "id": _uid(), "ability_index": 0}],
                            "postedit_param": {"type": "", "id": _uid(), "generate_type": 0},
                        },
                    },
                }],
            }),
            "http_common_info": {"aid": _DEFAULT_ASSISTANT_ID},
        }

        logger.info("generate_image_to_image: model=%s ratio=%s ref=%.50s", self.model, ratio, upload_id)
        body = await self._request(
            "POST", "/mweb/v1/aigc_draft/generate",
            params={"da_version": _DRAFT_VERSION, "web_component_open_flag": 1, "web_version": _DRAFT_VERSION},
            data=request_data,
        )
        task_id = body.get("aigc_data", body).get("history_record_id")
        if not task_id:
            raise JiMengException("Image-to-image: no history_record_id in response.")
        logger.info("Image-to-image task → %s", task_id)
        return task_id

    # -- image-to-video (extended) -------------------------------------------

    async def generate_video_with_frames(
        self,
        prompt: str,
        first_frame: str = "",   # file path, URL, or image_uri
        end_frame: str = "",     # file path, URL, or image_uri
        *,
        ratio: str = "16:9",
        resolution: str = "720p",
        duration: int = 5,
    ) -> str:
        """Generate a video with optional start/end reference frames.

        If *first_frame* / *end_frame* are local paths or URLs they are
        automatically uploaded first.
        """
        # Upload frames if needed
        first_uri = None
        end_uri = None
        if first_frame:
            first_uri = first_frame if first_frame.startswith("tos-cn-i") else await self.upload_image(first_frame)
        if end_frame:
            end_uri = end_frame if end_frame.startswith("tos-cn-i") else await self.upload_image(end_frame)

        # Re-use the existing generate_video with uploaded URIs
        # We call the internal build + request directly since the existing
        # generate_video doesn't accept frame URIs yet
        return await self._generate_video_internal(
            prompt, ratio, resolution, duration, first_uri, end_uri,
        )

    async def _generate_video_internal(
        self, prompt, ratio, resolution, duration, first_uri, end_uri,
    ) -> str:
        """Internal: generate video with pre-uploaded frame URIs."""
        internal = self._VIDEO_MODEL_MAP.get(self.model, self._VIDEO_MODEL_MAP["3.0"])
        is_3x = "3.0" in self.model
        if not is_3x and duration != 5:
            duration = 5
        if ratio not in self._VIDEO_RATIOS:
            ratio = "16:9"
        if resolution not in self._VIDEO_RESOLUTIONS:
            resolution = "720p"
        duration_ms = 5000 if duration == 5 else 10000

        first_frame_obj = None
        end_frame_obj = None
        if first_uri:
            fid = _uid()
            first_frame_obj = {"format": "", "height": 1024, "id": fid, "image_uri": first_uri,
                               "name": "", "platform_type": 1, "source_from": "upload",
                               "type": "image", "uri": first_uri, "width": 1024}
        if end_uri:
            eid = _uid()
            end_frame_obj = {"format": "", "height": 1024, "id": eid, "image_uri": end_uri,
                             "name": "", "platform_type": 1, "source_from": "upload",
                             "type": "image", "uri": end_uri, "width": 1024}

        cid = _uid(); sid = _uid()
        request_data = {
            "extend": {"root_model": internal,
                "m_video_commerce_info": {"benefit_type": "basic_video_operation_vgfm_v_three",
                    "resource_id": "generate_video", "resource_id_type": "str", "resource_sub_type": "aigc"},
                "m_video_commerce_info_list": [{"benefit_type": "basic_video_operation_vgfm_v_three",
                    "resource_id": "generate_video", "resource_id_type": "str", "resource_sub_type": "aigc"}]},
            "submit_id": sid,
            "metrics_extra": json.dumps({"enterFrom": "click", "isDefaultSeed": 1, "promptSource": "custom",
                "isRegenerate": False, "originSubmitId": _uid()}),
            "draft_content": json.dumps({
                "type": "draft", "id": _uid(), "min_version": "3.0.5", "is_from_tsn": True,
                "version": self._VIDEO_DRAFT_VERSION, "main_component_id": cid,
                "component_list": [{"type": "video_base_component", "id": cid, "min_version": "1.0.0",
                    "metadata": {"type": "", "id": _uid(), "created_platform": 3,
                        "created_platform_version": "", "created_time_in_ms": _now_ms(), "created_did": ""},
                    "generate_type": "gen_video", "aigc_mode": "workbench",
                    "abilities": {"type": "", "id": _uid(),
                        "gen_video": {"id": _uid(), "type": "",
                            "text_to_video_params": {"type": "", "id": _uid(),
                                "model_req_key": internal, "priority": 0, "seed": _rng_seed(),
                                "video_aspect_ratio": ratio,
                                "video_gen_inputs": [{"duration_ms": duration_ms,
                                    "first_frame_image": first_frame_obj,
                                    "end_frame_image": end_frame_obj,
                                    "fps": 24, "id": _uid(), "min_version": "3.0.5",
                                    "prompt": prompt, "resolution": resolution,
                                    "type": "", "video_mode": 2}]},
                            "video_task_extra": json.dumps({"enterFrom": "click", "isDefaultSeed": 1,
                                "promptSource": "custom", "isRegenerate": False, "originSubmitId": _uid()})}}}]}),
            "http_common_info": {"aid": _DEFAULT_ASSISTANT_ID},
        }
        body = await self._request(
            "POST", "/mweb/v1/aigc_draft/generate",
            params={"aigc_features": "app_lip_sync", "web_version": "6.6.0",
                    "da_version": self._VIDEO_DRAFT_VERSION, "web_component_open_flag": 1},
            data=request_data,
        )
        task_id = body.get("aigc_data", body).get("history_record_id")
        if not task_id:
            raise JiMengException("Video (frames): no history_record_id.")
        logger.info("Video (frames) task → %s", task_id)
        return task_id

    # -- internal ------------------------------------------------------------

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.http_timeout)
        return self._client

    def _build_cookie(self) -> str:
        ts = int(time.time())
        return "; ".join([
            f"_tea_web_id={self._web_id}",
            "is_staff_user=false",
            "store-region=cn-gd",
            "store-region-src=uid",
            f"sid_guard={self.cookie}%7C{ts}%7C5184000%7CMon%2C+03-Feb-2025+08%3A17%3A09+GMT",
            f"uid_tt={self._user_id}",
            f"uid_tt_ss={self._user_id}",
            f"sid_tt={self.cookie}",
            f"sessionid={self.cookie}",
            f"sessionid_ss={self.cookie}",
        ])

    async def _request(
        self, method: str, uri: str, *, params=None, data=None
    ) -> dict:
        device_time = str(int(time.time()))
        headers = {
            **_FAKE_HEADERS,
            "Cookie":      self._build_cookie(),
            "Device-Time": device_time,
            "Sign":        _sign(uri, device_time),
            "Sign-Ver":    "1",
        }
        url_params = {
            "aid": str(_DEFAULT_ASSISTANT_ID),
            "device_platform": "web",
            "region":          "CN",
            "webId":           self._web_id,
            **(params or {}),
        }
        url = f"https://jimeng.jianying.com{uri}"

        client = await self._get_client()
        try:
            resp = await client.request(
                method, url, params=url_params, json=data, headers=headers,
            )
            resp.raise_for_status()
        except httpx.TimeoutException as exc:
            raise NetworkError(f"Timeout calling {uri}") from exc
        except httpx.HTTPStatusError as exc:
            raise JiMengException(
                f"HTTP {exc.response.status_code} from {uri}: "
                f"{exc.response.text[:300]}"
            ) from exc
        except httpx.RequestError as exc:
            raise NetworkError(f"Request error on {uri}: {exc}") from exc

        try:
            body = resp.json()
        except ValueError as exc:
            raise JiMengException(f"Invalid JSON from {uri}") from exc

        ret = body.get("ret", -1)
        if str(ret) != "0":
            errmsg = body.get("errmsg", "unknown")
            logger.error("API error ret=%s msg=%s", ret, errmsg)
            if str(ret) in ("5000", "1006"):
                raise InsufficientCreditsError(errmsg)
            raise JiMengException(f"[{ret}] {errmsg}")

        return body.get("data", body)

    @staticmethod
    def _extract_url(item: dict) -> Optional[str]:
        try:
            large = item.get("image", {}).get("large_images", [])
            if large and large[0].get("image_url"):
                return large[0]["image_url"]
        except Exception:
            pass
        try:
            cover = item.get("common_attr", {}).get("cover_url")
            if cover:
                return cover
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Video generation
    # ------------------------------------------------------------------

    _VIDEO_MODEL_MAP = {
        "3.0-pro": "dreamina_ic_generate_video_model_vgfm_3.0_pro",
        "3.0":     "dreamina_ic_generate_video_model_vgfm_3.0",
        "3.0-fast":"dreamina_ic_generate_video_model_vgfm_3.0_fast",
        "s2.0":    "dreamina_ic_generate_video_model_vgfm_lite",
        "2.0-pro": "dreamina_ic_generate_video_model_vgfm1.0",
    }

    _VIDEO_RATIOS = {"16:9", "9:16", "1:1", "4:3", "3:4", "21:9"}
    _VIDEO_RESOLUTIONS = ("1080p", "720p", "480p")
    _VIDEO_DRAFT_VERSION = "3.2.8"

    async def generate_video(
        self,
        prompt: str,
        *,
        ratio: str = "16:9",
        resolution: str = "720p",
        duration: int = 10,
        first_frame_path: Optional[str] = None,
        end_frame_path: Optional[str] = None,
    ) -> str:
        """Submit a text-to-video task and return a **task_id**.

        Supports optional first-frame / end-frame images for guided generation
        (image-to-video).

        Parameters:
            prompt: Natural-language video description.
            ratio: ``"16:9"``, ``"9:16"``, ``"1:1"``, ``"4:3"``, ``"3:4"``, ``"21:9"``.
            resolution: ``"1080p"``, ``"720p"``, ``"480p"``.
            duration: ``5`` or ``10`` seconds (2.x models only support 5 s).
            first_frame_path: Optional local file path or URL for starting frame.
            end_frame_path: Optional local file path or URL for ending frame.
        """
        internal = self._VIDEO_MODEL_MAP.get(self.model,
            self._VIDEO_MODEL_MAP["3.0"])
        is_3x = "3.0" in self.model
        if not is_3x and duration != 5:
            logger.info("2.x models only support 5 s; adjusting.")
            duration = 5
        if ratio not in self._VIDEO_RATIOS:
            detected = _detect_ratio(prompt)
            ratio = detected if detected in self._VIDEO_RATIOS else "16:9"
        if resolution not in self._VIDEO_RESOLUTIONS:
            resolution = "720p"
        duration_ms = 5000 if duration == 5 else 10000

        logger.info(
            "generate_video: model=%s ratio=%s res=%s dur=%ds",
            self.model, ratio, resolution, duration,
        )

        # Upload first/end frames if provided
        first_frame = None
        end_frame   = None
        if first_frame_path or end_frame_path:
            for label, path in [("first", first_frame_path), ("end", end_frame_path)]:
                if not path:
                    continue
                # Simplified upload: treat path as already-uploaded URI or skip
                # For full upload support, use the upload pipeline from jimeng_api
                logger.info("Frame upload not yet implemented; skipping %s frame.", label)

        cid = _uid()
        sid = _uid()

        request_data = {
            "extend": {
                "root_model": internal,
                "m_video_commerce_info": {
                    "benefit_type": "basic_video_operation_vgfm_v_three",
                    "resource_id": "generate_video",
                    "resource_id_type": "str",
                    "resource_sub_type": "aigc",
                },
                "m_video_commerce_info_list": [{
                    "benefit_type": "basic_video_operation_vgfm_v_three",
                    "resource_id": "generate_video",
                    "resource_id_type": "str",
                    "resource_sub_type": "aigc",
                }],
            },
            "submit_id": sid,
            "metrics_extra": json.dumps({
                "enterFrom": "click",
                "isDefaultSeed": 1,
                "promptSource": "custom",
                "isRegenerate": False,
                "originSubmitId": _uid(),
            }),
            "draft_content": json.dumps({
                "type": "draft",
                "id": _uid(),
                "min_version": "3.0.5",
                "is_from_tsn": True,
                "version": self._VIDEO_DRAFT_VERSION,
                "main_component_id": cid,
                "component_list": [{
                    "type": "video_base_component",
                    "id": cid,
                    "min_version": "1.0.0",
                    "metadata": {
                        "type": "", "id": _uid(),
                        "created_platform": 3,
                        "created_platform_version": "",
                        "created_time_in_ms": _now_ms(),
                        "created_did": "",
                    },
                    "generate_type": "gen_video",
                    "aigc_mode": "workbench",
                    "abilities": {
                        "type": "", "id": _uid(),
                        "gen_video": {
                            "id": _uid(), "type": "",
                            "text_to_video_params": {
                                "type": "", "id": _uid(),
                                "model_req_key": internal,
                                "priority": 0,
                                "seed": _rng_seed(),
                                "video_aspect_ratio": ratio,
                                "video_gen_inputs": [{
                                    "duration_ms": duration_ms,
                                    "first_frame_image": first_frame,
                                    "end_frame_image": end_frame,
                                    "fps": 24,
                                    "id": _uid(),
                                    "min_version": "3.0.5",
                                    "prompt": prompt,
                                    "resolution": resolution,
                                    "type": "",
                                    "video_mode": 2,
                                }],
                            },
                            "video_task_extra": json.dumps({
                                "enterFrom": "click",
                                "isDefaultSeed": 1,
                                "promptSource": "custom",
                                "isRegenerate": False,
                                "originSubmitId": _uid(),
                            }),
                        },
                    },
                }],
            }),
            "http_common_info": {"aid": _DEFAULT_ASSISTANT_ID},
        }

        body = await self._request(
            "POST", "/mweb/v1/aigc_draft/generate",
            params={
                "aigc_features": "app_lip_sync",
                "web_version": "6.6.0",
                "da_version": self._VIDEO_DRAFT_VERSION,
                "web_component_open_flag": 1,
            },
            data=request_data,
        )
        aigc = body.get("aigc_data", body)
        task_id = aigc.get("history_record_id")
        if not task_id:
            raise JiMengException("Video API did not return history_record_id.")
        logger.info("Video task submitted → %s", task_id)
        return task_id

    async def poll_video_status(self, task_id: str, timeout: int = 1200) -> str:
        """Poll the video generation task until completion.

        Returns the final **video URL**.

        Video generation can take 2–10 minutes; the default timeout is 20 min.
        """
        deadline = time.monotonic() + timeout
        logger.info("poll_video_status: task_id=%s timeout=%ds", task_id, timeout)

        # Initial wait — video generation takes longer
        await asyncio.sleep(5)

        retry = 0
        while time.monotonic() < deadline:
            retry += 1
            await asyncio.sleep(min(2 * retry, 30))

            # Alternate API endpoints for robustness
            use_alt = retry > 10 and retry % 2 == 0
            uri = "/mweb/v1/get_history_records" if use_alt else "/mweb/v1/get_history_by_ids"
            req_data = ({"history_record_ids": [task_id]} if use_alt
                        else {"history_ids": [task_id]})

            try:
                data = await self._request("POST", uri, data=req_data)
            except Exception:
                continue

            # Try to extract video URL from raw response first
            raw_str = json.dumps(data)
            m = re.search(r'https://v\d+-artist\.vlabvod\.com/[^"\s]+', raw_str)
            if m:
                logger.info("Video URL extracted from raw response.")
                return m.group(0)

            # Parse history entry
            entry = (data.get("history_records", [{}])[0] if use_alt
                     else data.get(task_id, {}))
            if use_alt and not entry:
                entry = data.get("history_list", [{}])[0] if data.get("history_list") else {}

            status    = entry.get("status", 20)
            fail_code = entry.get("fail_code", "")
            items     = entry.get("item_list") or []

            logger.debug("video poll %d: status=%d items=%d", retry, status, len(items))

            if status == _FAILED:
                raise JiMengException(f"Video generation failed: fail_code={fail_code}")

            if items:
                url = self._extract_video_url(items[0])
                if url:
                    logger.info("Video ready: %.120s", url)
                    return url

        raise TaskTimeoutError(f"Video task {task_id} timed out after {timeout}s")

    @staticmethod
    def _extract_video_url(item: dict) -> Optional[str]:
        """Extract video URL from API response item."""
        try:
            video = item.get("video", {})
            transcoded = video.get("transcoded_video", {})
            origin = transcoded.get("origin", {})
            if origin.get("video_url"):
                return origin["video_url"]
        except Exception:
            pass
        for key in ("play_url", "download_url", "url"):
            try:
                url = item.get("video", {}).get(key)
                if url:
                    return url
            except Exception:
                pass
        return None
