"""Codebook endpoints: how every column of an uploaded file is coded, and edits to it
(PLAN_ANY_FORMAT.md §8, IMPLEMENTATION_PLAN.md M2/M4). Saving codebooks across waves arrives in M5.
"""

import asyncio
import json
import time

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Request, UploadFile

from auth import require_scope, KeyConfig
from middleware.processing import run_in_executor
from middleware.rate_limiter import check_rate_limit
from shared.file_resolver import resolve_file
from shared.response import success_response
from shared.codebook_resolver import parse_codebook, resolve_codebook, store_codebook
from services.data_loader import EditError, describe_file, edit_codebook

router = APIRouter(tags=["Codebook"])

DESCRIPTION = (
    "Returns the codebook for a file: for every column, how it is coded — role "
    "(single / multi / numeric / text / date / ignore), SPSS name and label, codes with "
    "the raw answers mapped to them and respondent counts, user-missing codes, multi-select "
    "options, derived variables (e.g. NPS groups) and the reason behind each decision. "
    "Excel/CSV files are coded by inference; SPSS files are described from their metadata. "
    "`summary.unmatched_answers` lists answers a supplied codebook doesn't cover.\n\n"
    "Edit with `edits` (JSON list of operations, see services/codebook/patch.py): set_label, set_role, "
    "set_measure, relabel_code, set_missing, merge_codes, reorder_codes, add_derived (recode, bands, "
    "priority, count, combination, coalesce, flags), update_derived, remove_derived. Send the returned "
    "`codebook` back as `codebook` on later edits and on /v1/crosstab, /v1/tabulate, /v1/auto-analyze… "
    "Invalid edits return 400 with the reason."
)

EDITS_HELP = 'JSON list of edits, e.g. [{"op": "set_missing", "var": "Q5", "code": 98, "missing": true}]'
CODEBOOK_HELP = "Codebook JSON from a previous response (default: inferred, or the one saved for file_id)"


def _parse_edits(edits: str | list | None) -> list[dict]:
    if not edits:
        return []
    try:
        parsed = json.loads(edits) if isinstance(edits, str) else edits
    except ValueError as e:
        raise HTTPException(400, detail={"code": "INVALID_EDITS", "message": f"edits must be a JSON list: {e}"})
    if not isinstance(parsed, list) or not all(isinstance(op, dict) for op in parsed):
        raise HTTPException(400, detail={"code": "INVALID_EDITS", "message": "edits must be a JSON list of objects"})
    return parsed


async def _run(fn, *args) -> dict:
    try:
        return await run_in_executor(fn, *args)
    except (asyncio.TimeoutError, RuntimeError):
        raise  # handled by global exception handlers (504 / 503)
    except EditError as e:
        raise HTTPException(400, detail={"code": "INVALID_EDIT", "message": str(e)})
    except ValueError as e:
        raise HTTPException(400, detail={"code": "INVALID_FILE", "message": str(e)})
    except Exception as e:
        raise HTTPException(500, detail={"code": "PROCESSING_FAILED", "message": str(e)})


async def _describe(file_bytes: bytes, filename: str, codebook=None, edits: list[dict] | None = None) -> dict:
    if edits:
        return await _run(edit_codebook, file_bytes, filename, edits, codebook)
    return await _run(describe_file, file_bytes, filename, codebook)


@router.post("/v1/codebook", summary="How each column of a file is coded", description=DESCRIPTION)
async def codebook_endpoint(
    request: Request,
    file: UploadFile = File(None, description="Data file: .sav, .csv, .tsv, .xlsx, .xls (or use file_id)"),
    file_id: str | None = Form(None, description="File session ID from /v1/files/upload"),
    codebook: str | None = Form(None, description=CODEBOOK_HELP),
    edits: str | None = Form(None, description=EDITS_HELP),
    key: KeyConfig = Depends(require_scope("metadata")),
    _rl: None = Depends(check_rate_limit),
):
    start = time.perf_counter()
    file_bytes, filename = await resolve_file(file=file, file_id=file_id)
    current = await resolve_codebook(codebook, file_id)
    result = await _describe(file_bytes, filename, current, _parse_edits(edits))
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
    result = await _describe(file_bytes, filename, await resolve_codebook(None, file_id))
    return success_response(result, processing_time_ms=int((time.perf_counter() - start) * 1000))


@router.patch("/v1/files/{file_id}/codebook", summary="Edit and save the codebook of an uploaded file",
              description="Applies `edits` to the file's codebook (the saved one, or inferred) and saves the "
                          "result with the file session, so later requests with this file_id use it. "
                          "Same edit operations as POST /v1/codebook. Requires Redis file sessions.")
async def patch_file_codebook(
    file_id: str,
    request: Request,
    body: dict = Body(..., examples=[{"edits": [{"op": "set_label", "var": "Q1", "label": "Gender"}]}]),
    key: KeyConfig = Depends(require_scope("metadata")),
    _rl: None = Depends(check_rate_limit),
):
    start = time.perf_counter()
    file_bytes, filename = await resolve_file(file_id=file_id)
    current = parse_codebook(body.get("codebook")) or await resolve_codebook(None, file_id)
    result = await _describe(file_bytes, filename, current, _parse_edits(body.get("edits")))
    await store_codebook(file_id, parse_codebook(result["codebook"]))
    return success_response(result, processing_time_ms=int((time.perf_counter() - start) * 1000))
