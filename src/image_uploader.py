"""
Image uploader integration for external image hosting (e.g., PicGo/Chevereto).

Reads configuration from environment variables:
  PICGO_UPLOAD_ENABLED=true|false
  PICGO_UPLOAD_URL=https://www.picgo.net/api/1/upload (override if docs differ)
  PICGO_API_KEY=chv_xxx (Chevereto-style API key)

If enabled and a data URI image is detected, uploads and returns a public URL.
Falls back to None on any error.
"""
from __future__ import annotations

import os
import re
from typing import Optional, Tuple

from .httpx_client import safe_post_async
from log import log


_DATA_URI_RE = re.compile(r"^data:(?P<mime>[^;]+);base64,(?P<b64>.+)$", re.IGNORECASE)


def _parse_data_uri(data_uri: str) -> Optional[Tuple[str, str]]:
    m = _DATA_URI_RE.match(data_uri.strip())
    if not m:
        return None
    return m.group("mime"), m.group("b64")


async def upload_data_uri_to_picgo(data_uri: str) -> Optional[str]:
    """Upload a data URI image to PicGo/Chevereto-like API. Returns public URL or None."""
    enabled = os.getenv("PICGO_UPLOAD_ENABLED", "false").lower() in ("1", "true", "yes", "on")
    if not enabled:
        return None

    api_key = os.getenv("PICGO_API_KEY")
    if not api_key:
        log.debug("PICGO_UPLOAD_ENABLED is true but PICGO_API_KEY not set; skip upload")
        return None

    parsed = _parse_data_uri(data_uri)
    if not parsed:
        return None
    mime, b64 = parsed

    # Endpoint: default to Chevereto v1 style
    upload_url = os.getenv("PICGO_UPLOAD_URL", "https://www.picgo.net/api/1/upload")

    # Chevereto API typically accepts form fields: key, source (data URI or raw base64), format=json
    form = {
        "key": api_key,
        "source": f"data:{mime};base64,{b64}",
        "format": "json",
    }

    try:
        resp = await safe_post_async(upload_url, data=form, timeout=30.0)
        data = resp.json()
        # Common Chevereto response paths
        url = (
            data.get("image", {}).get("url")
            or data.get("image", {}).get("display_url")
            or data.get("image", {}).get("url_viewer")
            or data.get("data", {}).get("url")
            or data.get("data", {}).get("display_url")
        )
        if not url:
            log.warning(f"PicGo upload returned no url: {str(data)[:200]}")
            return None
        return url
    except Exception as e:
        log.error(f"PicGo upload failed: {e}")
        return None

