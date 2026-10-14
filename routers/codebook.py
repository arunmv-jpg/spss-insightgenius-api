"""Codebook endpoints: how every column of an uploaded file is coded (PLAN_ANY_FORMAT.md §8).

Read-only for now (IMPLEMENTATION_PLAN.md M2); editing arrives in M4, saving in M5.
"""

import asyncio
import time

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from auth import require_scope, KeyConfig
from middleware.processing import run_in_executor
from middleware.rate_limiter import check_rate_limit
from shared.file_resolver import resolve_file
from shared.response import success_response
from services.data_loader import describe_file

router = APIRouter(tags=["Codebook"])

DESCRIPTION = (
    "Returns the codebook for a file: for every column, how it is coded — role "
    "(single / multi / numeric / text / date / ignore), SPSS name and label, codes with "
    "the raw answers mapped to them and respondent counts, user-missing codes, multi-select "
    "options, derived variables (e.g. NPS groups) and the reason behind each decision. "
    "Excel/CSV files are coded by inference; SPSS files are described from their metadata. "
    "`summary.unmatched_answers` lists answers a supplied codebook doesn't cover."
)


async def _describe(file_bytes: bytes, filename: str) -> dict:
    try:
        return await run_in_executor(describe_file, file_bytes, filename)
    except (asyncio.TimeoutError, RuntimeError):
        raise  # handled by global exception handlers (504 / 503)
    except ValueError as e:
        raise HTTPException(400, detail={"code": "INVALID_FILE", "message": str(e)})
    except Exception as e:
        raise HTTPException(500, detail={"code": "PROCESSING_FAILED", "message": str(e)})


@router.post("/v1/codebook", summary="How each column of a file is coded", description=DESCRIPTION)
async def codebook(
    request: Request,
    file: UploadFile = File(None, description="Data file: .sav, .csv, .tsv, .xlsx, .xls (or use file_id)"),
    file_id: str | None = Form(None, description="File session ID from /v1/files/upload"),
    key: KeyConfig = Depends(require_scope("metadata")),
    _rl: None = Depends(check_rate_limit),
):
    start = time.perf_counter()
    file_bytes, filename = await resolve_file(file=file, file_id=file_id)
    result = await _describe(file_bytes, filename)
    return success_response(result, processing_time_ms=int((time.perf_counter() - start) * 1000))


@router.get("/v1/files/{file_id}/codebook", summary="Codebook of an uploaded file", description=DESCRIPTION)
async def file_codebook(
    file_id: str,
    request: Request,
    key: KeyConfig = Depends(require_scope("metadata")),
    _rl: None = Depends(check_rate_limit),
):
    start = time.perf_counter()
    file_bytes, filename = await resolve_file(file_id=file_id)
    result = await _describe(file_bytes, filename)
    return success_response(result, processing_time_ms=int((time.perf_counter() - start) * 1000))
