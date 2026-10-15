"""Setups: saved, reusable recodes for a kind of survey export (RECODING_PLAN.md §6).

Built-in setups ship in `presets/`; an exported setup (JSON) can be sent back as `setup`.
"""

import time

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from auth import require_scope, KeyConfig
from middleware.rate_limiter import check_rate_limit
from routers.codebook import CODEBOOK_HELP, _run
from shared.codebook_resolver import resolve_codebook
from shared.file_resolver import resolve_file
from shared.response import success_response
from services.setups import SetupError, apply_setup_to_file, builtin_setups, get_setup, match_file, parse_setup

router = APIRouter(tags=["Setups"])


@router.get("/v1/setups", summary="Built-in setups",
            description="Named lists of codebook edits for known survey exports (e.g. HubSpot CX Pulse), "
                        "with the columns each expects. Apply one with POST /v1/setups/apply.")
async def list_setups(
    request: Request,
    key: KeyConfig = Depends(require_scope("metadata")),
    _rl: None = Depends(check_rate_limit),
):
    start = time.perf_counter()
    setups = [{"id": s.id, "name": s.name, "description": s.description, "columns": s.match.columns,
               "edits": s.edits} for s in builtin_setups()]
    return success_response({"setups": setups}, processing_time_ms=int((time.perf_counter() - start) * 1000))


@router.post("/v1/setups/match", summary="Setups that fit a file",
             description="Compares the file's columns with each built-in setup's expected columns. "
                         "`complete` = every expected column is present; `score` = the share present.")
async def match_endpoint(
    request: Request,
    file: UploadFile = File(None, description="Data file (or use file_id)"),
    file_id: str | None = Form(None, description="File session ID from /v1/files/upload"),
    codebook: str | None = Form(None, description=CODEBOOK_HELP),
    key: KeyConfig = Depends(require_scope("metadata")),
    _rl: None = Depends(check_rate_limit),
):
    start = time.perf_counter()
    file_bytes, filename = await resolve_file(file=file, file_id=file_id)
    result = await _run(match_file, file_bytes, filename, await resolve_codebook(codebook, file_id))
    return success_response(result, processing_time_ms=int((time.perf_counter() - start) * 1000))


@router.post("/v1/setups/apply", summary="Apply a setup to a file's codebook",
             description="Applies a setup's edits one by one to the file's codebook (the one sent, or inferred). "
                         "Edits that don't fit this file (a column or option it doesn't have) are skipped and "
                         "listed in `setup.skipped` with the reason. Returns the codebook as /v1/codebook does; "
                         "send it as `codebook` to the analysis endpoints.")
async def apply_endpoint(
    request: Request,
    file: UploadFile = File(None, description="Data file (or use file_id)"),
    file_id: str | None = Form(None, description="File session ID from /v1/files/upload"),
    codebook: str | None = Form(None, description=CODEBOOK_HELP),
    setup_id: str | None = Form(None, description="A built-in setup, e.g. hubspot_cx_pulse"),
    setup: str | None = Form(None, description="Or a setup JSON: {name, match: {columns}, edits: [...]}"),
    key: KeyConfig = Depends(require_scope("metadata")),
    _rl: None = Depends(check_rate_limit),
):
    start = time.perf_counter()
    if bool(setup_id) == bool(setup):
        raise HTTPException(400, detail={"code": "INVALID_SETUP", "message": "Send either setup_id or setup"})
    try:
        chosen = get_setup(setup_id) if setup_id else parse_setup(setup)
    except SetupError as e:
        raise HTTPException(404 if setup_id else 400, detail={"code": "SETUP_NOT_FOUND" if setup_id else "INVALID_SETUP",
                                                              "message": str(e)})
    file_bytes, filename = await resolve_file(file=file, file_id=file_id)
    result = await _run(apply_setup_to_file, file_bytes, filename, chosen, await resolve_codebook(codebook, file_id))
    return success_response(result, processing_time_ms=int((time.perf_counter() - start) * 1000))
