"""JiMeng file-upload engine — upload images to ByteDance CDN.

The upload flow (reverse-engineered from jimeng-free-api-all):

1. POST /mweb/v1/get_upload_token      → obtain STS credentials
2. GET  imagex.bytedanceapi.com         → ApplyImageUpload (AWS V4 signed)
3. POST <upload-host>/upload/v1/<uri>   → raw binary upload
4. POST imagex.bytedanceapi.com         → CommitImageUpload
5. Returns image_uri for use in blend / video generation
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import random
import string
import struct
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ALNUM = string.ascii_lowercase + string.digits


def _rand_str(n: int = 11) -> str:
    return "".join(random.choice(_ALNUM) for _ in range(n))


def _crc32(data: bytes) -> int:
    """CRC-32 (IEEE) returning an unsigned 32-bit integer."""
    c = 0xFFFFFFFF
    for b in data:
        c ^= b
        for _ in range(8):
            if c & 1:
                c = (c >> 1) ^ 0xEDB88320
            else:
                c >>= 1
    return c ^ 0xFFFFFFFF


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac_sha256(key: bytes, msg: bytes) -> bytes:
    return hmac.new(key, msg, hashlib.sha256).digest()


def _amz_date() -> tuple[str, str]:
    """Return ``(amz_date, amz_day)`` in ISO-8601 basic format."""
    now = datetime.now(timezone.utc)
    d = now.strftime("%Y%m%dT%H%M%SZ")
    day = now.strftime("%Y%m%d")
    return d, day


# ---------------------------------------------------------------------------
# AWS V4 signature (for imagex.bytedanceapi.com)
# ---------------------------------------------------------------------------

def _aws_sign(
    access_key: str,
    secret_key: str,
    session_token: str,
    region: str,
    service: str,
    method: str,
    params: dict,
    body: dict | None = None,
) -> dict[str, str]:
    """Build AWS V4 Signature headers for ByteDance imagex API."""
    body_bytes = json.dumps(body).encode() if body else b""
    amz_date, amz_day = _amz_date()

    headers: dict[str, str] = {
        "X-Amz-Date": amz_date,
        "X-Amz-Security-Token": session_token,
    }
    if body_bytes:
        headers["X-Amz-Content-Sha256"] = _sha256(body_bytes)

    signed_headers = ";".join(sorted(k.lower() for k in headers))
    canonical_headers = "".join(
        f"{k.lower()}:{v}\n" for k, v in sorted(headers.items())
    )
    body_hash = _sha256(body_bytes) if body_bytes else _sha256(b"")

    canonical_request = "\n".join([
        method.upper(),
        "/",
        urlencode(params),
        canonical_headers,
        signed_headers,
        body_hash,
    ])

    credential = f"{amz_day}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amz_date,
        credential,
        _sha256(canonical_request.encode()),
    ])

    k_date   = _hmac_sha256(f"AWS4{secret_key}".encode(), amz_day.encode())
    k_region = _hmac_sha256(k_date,   region.encode())
    k_svc    = _hmac_sha256(k_region, service.encode())
    k_sign   = _hmac_sha256(k_svc,    b"aws4_request")
    signature = _hmac_sha256(k_sign, string_to_sign.encode()).hex()

    headers["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{credential}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return headers


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def upload_to_jimeng(
    file_path: str,
    cookie: str,
    *,
    scene: int = 2,
) -> str:
    """Upload a local file, URL, or base64 image to Jimeng CDN.

    Returns the ``image_uri`` that can be passed to ``generate_image``
    or ``generate_video`` as a reference frame.

    Parameters:
        file_path:
            - Local absolute path (e.g. ``"C:/art/hero.png"``)
            - HTTP(S) URL to an image
            - Base64 data-URI string
        cookie:
            Jimeng session cookie.
        scene:
            2 = general image; use 1 for video frames.

    Returns:
        CDN ``image_uri`` string.
    """
    # 1. Load file bytes ----------------------------------------------------
    if file_path.startswith("data:"):
        # base64 data URI
        header, b64 = file_path.split(",", 1)
        data = __import__("base64").b64decode(b64)
        fname = f"{uuid.uuid4().hex}.png"
    elif file_path.startswith(("http://", "https://")):
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(file_path)
            resp.raise_for_status()
            data = resp.content
        fname = os.path.basename(file_path.split("?")[0]) or f"{uuid.uuid4().hex}.jpg"
    else:
        p = Path(file_path)
        if not p.is_file():
            raise FileNotFoundError(f"File not found: {file_path}")
        data = p.read_bytes()
        fname = p.name

    logger.info("Upload: %d bytes, name=%s", len(data), fname)
    if len(data) > 100 * 1024 * 1024:
        raise ValueError("File exceeds 100 MB limit.")

    # 2. Obtain upload token -------------------------------------------------
    token = await _jimeng_request(
        cookie,
        "POST",
        "/mweb/v1/get_upload_token",
        params={"aid": "513695", "da_version": "3.2.2", "aigc_features": "app_lip_sync"},
        data={"scene": scene},
    )
    access_key = token.get("access_key_id")
    if not access_key:
        raise RuntimeError(f"Failed to get upload token: {token}")

    # 3. ApplyImageUpload (imagex) -------------------------------------------
    apply_params = {
        "Action":    "ApplyImageUpload",
        "FileSize":  len(data),
        "ServiceId": "tb4s082cfz",
        "Version":   "2018-08-01",
        "s":         _rand_str(11),
    }
    auth_headers = _aws_sign(
        access_key,
        token["secret_access_key"],
        token["session_token"],
        "cn-north-1",
        "imagex",
        "GET",
        apply_params,
    )
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            "https://imagex.bytedanceapi.com/",
            params=apply_params,
            headers=auth_headers,
        )
    result = r.json()
    if "Error" in result.get("Response ", {}):
        raise RuntimeError(result["Response "]["Error"].get("Message", "Upload apply failed"))
    ua = result["Result"]["UploadAddress"]

    # 4. Upload binary -------------------------------------------------------
    upload_url = f"https://{ua['UploadHosts'][0]}/upload/v1/{ua['StoreInfos'][0]['StoreUri']}"
    crc_hex = format(_crc32(data), "x")
    async with httpx.AsyncClient(timeout=60) as client:
        r2 = await client.post(
            upload_url,
            content=data,
            headers={
                "Authorization":  ua["StoreInfos"][0]["Auth"],
                "Content-Crc32":  crc_hex,
                "Content-Type":   "application/octet-stream",
            },
        )
    up_res = r2.json()
    if up_res.get("code") != 2000:
        raise RuntimeError(up_res.get("message", "Binary upload failed"))

    # 5. CommitImageUpload ---------------------------------------------------
    commit_params = {
        "Action":    "CommitImageUpload",
        "FileSize":  len(data),
        "ServiceId": "tb4s082cfz",
        "Version":   "2018-08-01",
    }
    commit_body = {"SessionKey": ua["SessionKey"]}
    commit_headers = _aws_sign(
        access_key,
        token["secret_access_key"],
        token["session_token"],
        "cn-north-1",
        "imagex",
        "POST",
        commit_params,
        commit_body,
    )
    commit_headers["Content-Type"] = "application/json"
    async with httpx.AsyncClient(timeout=30) as client:
        r3 = await client.post(
            "https://imagex.bytedanceapi.com/",
            params=commit_params,
            json=commit_body,
            headers=commit_headers,
        )
    commit_res = r3.json()
    if "Error" in commit_res.get("Response ", {}):
        raise RuntimeError(commit_res["Response "]["Error"].get("Message", "Commit failed"))
    uri = commit_res["Result"]["Results"][0]["Uri"]
    logger.info("Upload complete → image_uri=%s", uri)
    return uri


# ---------------------------------------------------------------------------
# Internal Jimeng HTTP helper (simplified)
# ---------------------------------------------------------------------------

import json as _json

async def _jimeng_request(
    cookie: str, method: str, uri: str, *, params=None, data=None
) -> dict:
    """Minimal signed Jimeng API call (no SDK dependency)."""
    ts = int(time.time())
    raw_sign = f"9e2c|{uri[-7:]}|7|5.8.0|{ts}||11ac"
    sign = hashlib.md5(raw_sign.encode()).hexdigest()

    web_id = str(random.randint(7_000_000_000_000_000_000, 9_999_999_999_999_999_999))
    uid = uuid.uuid4().hex

    cookie_str = "; ".join([
        f"_tea_web_id={web_id}", "is_staff_user=false",
        "store-region=cn-gd", "store-region-src=uid",
        f"sid_guard={cookie}%7C{ts}%7C5184000%7CMon%2C+03-Feb-2025+08%3A17%3A09+GMT",
        f"uid_tt={uid}", f"uid_tt_ss={uid}",
        f"sid_tt={cookie}", f"sessionid={cookie}", f"sessionid_ss={cookie}",
    ])

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Appid": "513695", "Appvr": "5.8.0",
        "Origin": "https://jimeng.jianying.com",
        "Pf": "7", "Referer": "https://jimeng.jianying.com",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Cookie": cookie_str, "Device-Time": str(ts),
        "Sign": sign, "Sign-Ver": "1",
    }
    url = f"https://jimeng.jianying.com{uri}"
    url_params = {**({"aid": "513695", "device_platform": "web", "region": "CN", "webId": web_id}),
                   **(params or {})}

    async with httpx.AsyncClient(timeout=45) as client:
        resp = await client.request(method, url, params=url_params, json=data, headers=headers)
    body = resp.json()
    if str(body.get("ret", -1)) != "0":
        raise RuntimeError(f"[{body.get('ret')}] {body.get('errmsg', '')}")
    return body.get("data", body)
