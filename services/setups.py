"""Setups: named, reusable lists of codebook edits (RECODING_PLAN.md §6).

A setup holds the recodes a survey needs every wave (e.g. the job-role priority groups of a
HubSpot CX export) as data instead of code. Built-in setups ship as JSON in `presets/`;
users can export the edits they made on the review page as a setup and import it later.

Applying is forgiving: each edit is tried on its own, and edits that don't fit this file
(a column or option missing in this wave) are skipped and reported, never silently dropped.
Saved setups per organisation (with versions) arrive with Supabase in IMPLEMENTATION_PLAN.md M5.
"""

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import BaseModel, Field, ValidationError

from services.codebook.infer import infer_codebook
from services.codebook.model import Codebook
from services.codebook.patch import EditError, apply_ops
from services.data_loader import SPSS_FORMATS, describe_file, file_format, read_raw

PRESETS_DIR = Path(__file__).resolve().parent.parent / "presets"
MIN_MATCH = 0.5  # share of a setup's columns a file needs before the setup is suggested


class SetupMatch(BaseModel):
    columns: list[str] = Field(default_factory=list, description="Column headers the setup expects")


class Setup(BaseModel):
    id: str = ""
    name: str
    description: str = ""
    match: SetupMatch = Field(default_factory=SetupMatch)
    edits: list[dict[str, Any]]


class SetupError(ValueError):
    pass


@lru_cache(maxsize=1)
def builtin_setups() -> tuple[Setup, ...]:
    setups = []
    for path in sorted(PRESETS_DIR.glob("*.json")):
        setup = Setup.model_validate_json(path.read_text(encoding="utf-8"))
        setup.id = setup.id or path.stem
        setups.append(setup)
    return tuple(setups)


def get_setup(setup_id: str) -> Setup:
    for setup in builtin_setups():
        if setup.id == setup_id:
            return setup
    raise SetupError(f"No setup '{setup_id}'")


def parse_setup(raw: str | dict) -> Setup:
    """A setup sent by a client (an exported / imported setup file)."""
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        return Setup.model_validate(data)
    except (ValueError, ValidationError) as e:
        raise SetupError(f"Invalid setup: {e}") from None


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).strip().casefold()


def match_setups(codebook: Codebook, setups: tuple[Setup, ...] | None = None) -> list[dict]:
    """Setups whose expected columns this file has, best first."""
    headers = {_norm(v.column) for v in codebook.variables} | {_norm(v.label) for v in codebook.variables}
    out = []
    for setup in builtin_setups() if setups is None else setups:
        wanted = setup.match.columns
        if not wanted:
            continue
        missing = [c for c in wanted if _norm(c) not in headers]
        score = 1 - len(missing) / len(wanted)
        if score >= MIN_MATCH:
            out.append({"id": setup.id, "name": setup.name, "description": setup.description,
                        "score": round(score, 3), "complete": not missing, "missing_columns": missing,
                        "edits": len(setup.edits)})
    return sorted(out, key=lambda m: -m["score"])


def describe_edit(op: dict) -> str:
    """A short human name for an edit, for the applied / skipped report."""
    kind = op.get("op", "?")
    if kind in ("add_derived", "update_derived"):
        return str((op.get("derived") or {}).get("label") or op.get("name") or kind)
    if kind == "set_data_workbook":
        return "Data sheets" if op.get("settings") else "No data sheets"
    if kind == "remove_derived":
        return f"remove {op.get('name')}"
    target = op.get("var", "")
    return f"{target}: {kind.replace('_', ' ')}" if target else kind


def _known_answers(codebook: Codebook, ref: str) -> set[str] | None:
    """Option labels of a multi-select / answer labels of a coded question (casefolded); None if unknown."""
    var = codebook.find(ref)
    if var is None:
        d = codebook.find_derived(ref)
        if d is None:
            return None
        return {o.label.casefold() for o in d.outputs} or {c.label.casefold() for c in d.codes}
    if var.role == "multi":
        return {o.label.casefold() for o in var.options}
    return {c.label.casefold() for c in [*var.codes, *var.missing]}


def _fit_to_file(op: dict, codebook: Codebook) -> tuple[dict, list[str]]:
    """Leave out answers this wave doesn't have (nobody gave them) instead of failing the whole recode.

    any/in/none drop the missing answers; all with a missing option can't match anyone, so it becomes
    an empty any (never true). Numeric codes are kept as they are.
    """
    if op.get("op") not in ("add_derived", "update_derived"):
        return op, []
    notes: list[str] = []
    name = describe_edit(op)

    def absent(ref: str, values: list) -> list:
        known = _known_answers(codebook, ref)
        if known is None:
            return []
        return [v for v in values if isinstance(v, str) and v.casefold() not in known]

    def fit(node: Any, ref: str | None = None) -> Any:
        if isinstance(node, list):
            return [fit(n, ref) for n in node]
        if not isinstance(node, dict):
            return node
        node = {k: fit(v, ref) for k, v in node.items()}
        target = node.get("var") or (ref if "values" in node else None)
        for key in ("any", "all", "none", "in", "values"):
            if target and isinstance(node.get(key), list):
                missing = absent(target, node[key])
                if not missing:
                    continue
                notes.extend(f"{name}: '{m}' isn't an answer of '{target}' in this file, so it was left out"
                             for m in missing)
                if key == "all":
                    node = {"var": target, "any": []}
                    break
                node[key] = [v for v in node[key] if v not in missing]
        return node

    derived = dict(op.get("derived") or {})
    params = derived.get("params") or {}
    source = params.get("from") if isinstance(params.get("from"), str) else derived.get("source")
    derived["params"] = fit(params, source)
    return {**op, "derived": derived}, notes


def apply_setup(codebook: Codebook, setup: Setup, df: pd.DataFrame, notes: list[str] | None = None,
                applied_ops: list[dict] | None = None) -> tuple[Codebook, list[str], list[dict]]:
    """Apply a setup's edits one by one → (codebook, applied, skipped with reasons).

    Answers missing from this wave are left out of the rules and described in `notes` (if given);
    `applied_ops` (if given) receives the edits as applied to this file.
    """
    applied, skipped = [], []
    for op in setup.edits:
        fitted, op_notes = _fit_to_file(op, codebook)
        try:
            codebook = apply_ops(codebook, [fitted], df)
            applied.append(describe_edit(op))
            if applied_ops is not None:
                applied_ops.append(fitted)
            if notes is not None:
                notes.extend(op_notes)
        except EditError as e:
            reason = re.sub(r"^Edit 1 \([^)]*\): ", "", str(e))
            skipped.append({"edit": describe_edit(op), "reason": reason})
    return codebook, applied, skipped


def _file_codebook(file_bytes: bytes, filename: str, codebook: Codebook | None):
    if file_format(filename) in SPSS_FORMATS:
        raise ValueError("Setups apply to Excel/CSV exports; an SPSS file keeps its own coding")
    raw = read_raw(file_bytes, filename)
    return raw, codebook or infer_codebook(raw)


def match_file(file_bytes: bytes, filename: str, codebook: Codebook | None = None) -> dict:
    """Blocking: setups that fit this file."""
    _, cb = _file_codebook(file_bytes, filename, codebook)
    return {"matches": match_setups(cb)}


def apply_setup_to_file(file_bytes: bytes, filename: str, setup: Setup, codebook: Codebook | None = None) -> dict:
    """Blocking: the file's codebook with the setup applied (described, as /v1/codebook returns it)."""
    raw, cb = _file_codebook(file_bytes, filename, codebook)
    notes: list[str] = []
    ops: list[dict] = []
    edited, applied, skipped = apply_setup(cb, setup, raw.df, notes, ops)
    result = describe_file(file_bytes, filename, edited)
    result["setup"] = {"id": setup.id, "name": setup.name, "applied": applied, "skipped": skipped,
                       "notes": notes, "edits": ops}
    return result
