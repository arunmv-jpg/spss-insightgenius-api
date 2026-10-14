"""POST /v1/crosstab — Crosstab with significance testing."""

import asyncio
import json
import time

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from auth import require_scope, KeyConfig
from middleware.processing import run_in_executor
from middleware.rate_limiter import check_rate_limit
from shared.codebook_resolver import resolve_codebook
from shared.file_resolver import resolve_file
from shared.response import success_response
from shared.significance import low_base_marker
from services.quantipy_engine import QuantiProEngine, QUANTIPYMRX_AVAILABLE, _sanitize_value
from services.data_loader import load_data

router = APIRouter(tags=["Analysis"])


@router.post("/v1/crosstab", summary="Crosstab with significance", description="Cross-tabulation with column proportion z-test significance letters (A/B/C notation).")
async def crosstab(
    request: Request,
    file: UploadFile = File(None, description="Data file: .sav, .csv, .tsv, .xlsx, .xls (or use file_id)"),
    file_id: str | None = Form(None, description="File session ID from /v1/library/upload"),
    codebook: str | None = Form(None, description="Codebook JSON from /v1/codebook (edited coding); default: inferred, or saved for file_id"),
    spec: str = Form(..., description='JSON object: {"row": "Q1", "col": "S2", "weight": null, "significance_level": 0.95, "significance_levels": [0.95, 0.90] (optional, lowercase letters at the 2nd level), "low_base_warn": 100, "low_base_flag": 30}'),
    key: KeyConfig = Depends(require_scope("crosstab")),
    _rl: None = Depends(check_rate_limit),
):
    start = time.perf_counter()

    file_bytes, filename = await resolve_file(file=file, file_id=file_id)
    cb = await resolve_codebook(codebook, file_id)

    # Parse spec JSON
    try:
        spec_data = json.loads(spec)
    except json.JSONDecodeError:
        raise HTTPException(400, detail={"code": "VALIDATION_ERROR", "message": "Invalid JSON in 'spec' field"})

    row_var = spec_data.get("row")
    col_var = spec_data.get("col")
    if not row_var or not col_var:
        raise HTTPException(400, detail={"code": "VALIDATION_ERROR", "message": "'row' and 'col' are required in spec"})

    weight = spec_data.get("weight")
    sig_level = spec_data.get("significance_level", 0.95)
    secondary_level = None
    if spec_data.get("significance_levels"):
        try:
            levels = sorted({float(level) for level in spec_data["significance_levels"]}, reverse=True)
        except (TypeError, ValueError):
            levels = []
        if not levels or not all(0 < level < 1 for level in levels):
            raise HTTPException(400, detail={"code": "VALIDATION_ERROR",
                                             "message": "significance_levels must be numbers between 0 and 1, e.g. [0.95, 0.90]"})
        sig_level = levels[0]
        secondary_level = levels[1] if len(levels) > 1 else None
    low_base_warn = int(spec_data.get("low_base_warn", 100))
    low_base_flag = int(spec_data.get("low_base_flag", 30))

    try:
        data = await run_in_executor(load_data, file_bytes, filename, cb)
    except (asyncio.TimeoutError, RuntimeError):
        raise
    except Exception as e:
        raise HTTPException(500, detail={"code": "PROCESSING_FAILED", "message": f"Failed to load SPSS: {e}"})

    try:
        result = await run_in_executor(
            QuantiProEngine.crosstab_with_significance, data, row_var, col_var, weight, sig_level,
            secondary_level,
        )
    except (asyncio.TimeoutError, RuntimeError):
        raise
    except ValueError as e:
        raise HTTPException(400, detail={"code": "VARIABLE_NOT_FOUND", "message": str(e)})
    except Exception as e:
        raise HTTPException(500, detail={"code": "PROCESSING_FAILED", "message": str(e)})

    # Unweighted base per banner column, with Decipher-style low-sample markers
    valid = data.df[[row_var, col_var]].dropna()
    bases = {str(_sanitize_value(v)): int(n) for v, n in valid[col_var].value_counts().items()}
    result["column_bases"] = bases
    result["low_base"] = {v: m for v, n in bases.items() if (m := low_base_marker(n, low_base_warn, low_base_flag))}

    elapsed_ms = int((time.perf_counter() - start) * 1000)
    return success_response(result, processing_time_ms=elapsed_ms, meta={
        "engine_version": "1.0.0",
        "quantipymrx_available": QUANTIPYMRX_AVAILABLE,
    })
