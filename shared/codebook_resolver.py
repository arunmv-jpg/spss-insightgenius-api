"""Which codebook applies to a request: one sent with it, or one stored with the file session."""

import json

from fastapi import HTTPException
from pydantic import ValidationError

from config import get_settings
from services.codebook.model import Codebook

REDIS_KEY = "spss:codebook:{file_id}"


def parse_codebook(raw: str | dict | None) -> Codebook | None:
    if not raw:
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        return Codebook.model_validate(data)
    except (ValueError, ValidationError) as e:
        raise HTTPException(400, detail={"code": "INVALID_CODEBOOK", "message": f"Invalid codebook: {e}"})


async def resolve_codebook(codebook: str | None, file_id: str | None) -> Codebook | None:
    """A codebook sent with the request wins; else the one saved for this file_id (if any)."""
    explicit = parse_codebook(codebook)
    if explicit is not None or not file_id or not get_settings().redis_url:
        return explicit
    import redis.asyncio as aioredis
    r = aioredis.from_url(get_settings().redis_url, decode_responses=True)
    try:
        stored = await r.get(REDIS_KEY.format(file_id=file_id))
        if stored:
            await r.expire(REDIS_KEY.format(file_id=file_id), get_settings().spss_session_ttl_seconds)
    except Exception:
        stored = None
    finally:
        await r.aclose()
    return parse_codebook(stored) if stored else None


async def store_codebook(file_id: str, codebook: Codebook) -> None:
    settings = get_settings()
    if not settings.redis_url:
        raise HTTPException(400, detail={"code": "NO_SESSION",
                                         "message": "Saving edits to a file_id needs Redis; send `codebook` with each request instead."})
    import redis.asyncio as aioredis
    r = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        await r.set(REDIS_KEY.format(file_id=file_id), codebook.model_dump_json(),
                    ex=settings.spss_session_ttl_seconds)
    finally:
        await r.aclose()
