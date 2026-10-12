"""MCP file session management — Redis sessions, base64 decoding, data loading.

Handles file resolution from Redis file_id sessions or inline base64,
and loading data into SPSSData for analysis.
"""

import base64
import json
import logging

import redis.asyncio as aioredis
from fastmcp.exceptions import ToolError

from config import get_settings
from mcp_server.auth import _make_error
from services.data_loader import SUPPORTED_FORMATS, file_format, load_data
from services.quantipy_engine import SPSSData

logger = logging.getLogger(__name__)

# ── Base64 handling ──────────────────────────────────────────────────────────

MAX_BASE64_BYTES = 50 * 1024 * 1024  # 50MB


def _decode_base64(file_base64: str) -> bytes:
    """Decode base64 file data, raise ToolError if malformed or too large."""
    try:
        decoded = base64.b64decode(file_base64)
    except Exception:
        raise ToolError(
            "Invalid base64 encoding for file_base64. "
            "Encode the file bytes with standard base64."
        )
    if len(decoded) > MAX_BASE64_BYTES:
        raise ToolError(json.dumps(_make_error(
            "file_too_large",
            f"File exceeds {MAX_BASE64_BYTES // 1024 // 1024}MB limit.",
            "Upload at https://spss.insightgenius.io/upload instead.",
        )))
    return decoded


# ── Redis helper ─────────────────────────────────────────────────────────────

async def _get_redis() -> aioredis.Redis | None:
    """Get an async Redis client, or None if REDIS_URL is not configured."""
    settings = get_settings()
    if not settings.redis_url:
        return None
    return aioredis.from_url(settings.redis_url, decode_responses=False)


# ── File resolution ──────────────────────────────────────────────────────────

async def _resolve_file(
    file_id: str | None,
    file_base64: str | None,
    filename: str,
) -> tuple[bytes, str]:
    """Resolve file bytes from either a Redis session (file_id) or inline base64.

    Returns (file_bytes, format_str). Refreshes TTL on session access.
    Raises ToolError if neither source is available or data is invalid.
    """
    settings = get_settings()

    # Priority 1: file_id from Redis session
    if file_id:
        r = await _get_redis()
        if r is None:
            raise ToolError(
                "file_id requires Redis. Either configure REDIS_URL on the server "
                "or pass file_base64 directly."
            )
        try:
            file_key = f"spss:file:{file_id}"
            meta_key = f"spss:meta:{file_id}"
            file_bytes = await r.get(file_key)
            if not file_bytes:
                await r.aclose()
                raise ToolError(json.dumps(_make_error(
                    "file_session_expired",
                    "Your file session has expired (sessions last 30 minutes). "
                    "Please upload your file again.",
                    "Guide the user to re-upload their file using spss_upload_file "
                    "or via https://spss.insightgenius.io/upload",
                    upload_url="https://spss.insightgenius.io/upload",
                )))
            meta_raw = await r.get(meta_key)
            # Refresh sliding TTL on both keys
            ttl = settings.spss_session_ttl_seconds
            await r.expire(file_key, ttl)
            await r.expire(meta_key, ttl)
            await r.aclose()

            if meta_raw:
                meta_info = json.loads(meta_raw)
                filename = meta_info.get("filename", filename)

            fmt = filename.rsplit(".", 1)[-1].lower() if "." in filename else "sav"
            return file_bytes, fmt
        except ToolError:
            raise
        except Exception as e:
            try:
                await r.aclose()
            except Exception:
                pass
            raise ToolError(f"Redis error retrieving file session: {e}")

    # Priority 2: inline base64
    if file_base64:
        file_bytes = _decode_base64(file_base64)
        fmt = filename.rsplit(".", 1)[-1].lower() if "." in filename else "sav"
        return file_bytes, fmt

    raise ToolError(json.dumps(_make_error(
        "file_missing",
        "No file provided. Please upload your data file first. "
        "For large files (> 1MB), go to https://spss.insightgenius.io/upload and tell me the code that appears.",
        "Ask the user to either: (1) upload the file in this conversation for base64 encoding, "
        "or (2) upload at https://spss.insightgenius.io/upload and provide the file_id.",
        upload_url="https://spss.insightgenius.io/upload",
    )))


# ── Data loading ─────────────────────────────────────────────────────────────

def _load_data(file_bytes: bytes, format_str: str, filename: str) -> SPSSData:
    """Load file bytes into SPSSData. Supports .sav, .csv, .tsv, .xlsx, .xls.

    CSV/Excel are converted to SPSS form (value labels, variable labels) so they
    get the same auto-detection and significance testing as .sav files.

    CPU-bound — call via run_in_executor.
    """
    if format_str not in SUPPORTED_FORMATS:
        raise ToolError(json.dumps(_make_error(
            "unsupported_format",
            f"The file format .{format_str} is not supported. "
            "Talk2Data accepts: .sav (SPSS), .csv, .tsv, .xlsx, and .xls files.",
            "Ask the user if they can export their data in one of the supported formats.",
            supported_formats=[".sav", ".csv", ".tsv", ".xlsx", ".xls"],
        )))
    # Make the extension authoritative even if the stored filename lacks one
    if file_format(filename) != format_str:
        filename = f"{filename}.{format_str}"
    return load_data(file_bytes, filename)
