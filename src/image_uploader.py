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
from typing import Optional, Tuple, List, Dict, Any
import base64
import mimetypes

from .httpx_client import safe_post_async, safe_get_async
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

async def upload_remote_image_to_picgo(image_url: str) -> Optional[str]:
    """Download remote image and upload to PicGo/Chevereto; return hosted URL.

    Best effort: returns None on any error or if image bed disabled.
    """
    enabled = os.getenv("PICGO_UPLOAD_ENABLED", "false").lower() in ("1", "true", "yes", "on")
    if not enabled:
        return None

    api_key = os.getenv("PICGO_API_KEY")
    if not api_key:
        return None

    try:
        resp = await safe_get_async(image_url, timeout=30.0)
        if resp.status_code != 200:
            return None
        content_type = resp.headers.get("Content-Type", "")
        if not content_type.startswith("image/"):
            # Try extension guess
            guess = mimetypes.guess_type(image_url)[0]
            mime = guess or "image/png"
        else:
            mime = content_type.split(";")[0].strip()
        b64 = base64.b64encode(resp.content).decode("ascii")
        return await upload_data_uri_to_picgo(f"data:{mime};base64,{b64}")
    except Exception as e:
        log.debug(f"upload_remote_image_to_picgo failed: {e}")
        return None


async def transform_gemini_parts_images(parts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Rewrite Gemini response parts to include image-bed Markdown links.

    - If a part contains inlineData with base64, optionally upload to image bed
      (PICGO_UPLOAD_ENABLED=true), and replace that part with a text part
      containing a Markdown image link pointing to hosted URL. If upload fails
      or uploading is disabled, keep original part unmodified.
    - If a part contains fileData with a fileUri, add a text part pointing to
      that URI as Markdown for client display, keeping the original part.

    Returns a new parts list; original list is not mutated.
    """
    try:
        if not isinstance(parts, list):
            return parts

        new_parts: List[Dict[str, Any]] = []
        for part in parts:
            try:
                # Prefer to preserve non-image parts exactly
                inline = part.get("inlineData") or part.get("inline_data")
                file_d = part.get("fileData") or part.get("file_data")

                if inline and isinstance(inline, dict):
                    mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
                    b64 = inline.get("data")
                    if b64:
                        # Try upload if enabled
                        md_url: Optional[str] = None
                        try:
                            data_uri = f"data:{mime};base64,{b64}"
                            md_url = await upload_data_uri_to_picgo(data_uri)
                        except Exception as e:
                            log.debug(f"transform_gemini_parts_images: upload skipped/failed: {e}")

                        if md_url:
                            # Replace inlineData with a text Markdown link
                            new_parts.append({"text": f"![image]({md_url})"})
                            continue
                        else:
                            # Fallback: keep original to allow client-side handling
                            new_parts.append(part)
                            continue

                if file_d and isinstance(file_d, dict):
                    uri = file_d.get("fileUri") or file_d.get("file_uri")
                    if uri:
                        # Try to rehost remote URI to image bed if enabled
                        hosted = None
                        try:
                            hosted = await upload_remote_image_to_picgo(uri)
                        except Exception as e:
                            log.debug(f"remote image rehost failed: {e}")
                        if hosted:
                            new_parts.append({"text": f"![image]({hosted})"})
                        else:
                            # Fallback to original uri
                            new_parts.append(part)
                            new_parts.append({"text": f"![image]({uri})"})
                        continue

                # Default: keep original
                new_parts.append(part)
            except Exception as e:
                log.debug(f"transform_gemini_parts_images: part error: {e}")
                new_parts.append(part)

        return new_parts
    except Exception as e:
        log.debug(f"transform_gemini_parts_images: failed: {e}")
        return parts
