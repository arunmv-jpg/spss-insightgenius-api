"""Apply a codebook to a raw table (step ⑤ of the pipeline): deterministic, no guessing.

The same codebook on the same file always gives the same coded data — the basis for
wave-on-wave consistency once codebooks are saved (IMPLEMENTATION_PLAN.md M5).
"""

import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
import pyreadstat

from services.codebook.heuristics import _merge_case_variants
from services.codebook.model import Codebook, Variable
from services.codebook.rules import Context, compute


@dataclass
class Coded:
    """SPSS-ready data + metadata, in output order."""
    columns: dict[str, pd.Series] = field(default_factory=dict)
    column_labels: list[str] = field(default_factory=list)
    value_labels: dict[str, dict[float, str]] = field(default_factory=dict)
    missing_ranges: dict[str, list[Any]] = field(default_factory=dict)
    measures: dict[str, str] = field(default_factory=dict)
    unmatched: dict[str, list[str]] = field(default_factory=dict)  # answers the codebook doesn't know

    def add(self, name: str, series: pd.Series, label: str, measure: str,
            value_labels: dict[float, str] | None = None, missing: list[Any] | None = None):
        self.columns[name] = series.reset_index(drop=True)
        self.column_labels.append(label)
        self.measures[name] = measure
        if value_labels:
            self.value_labels[name] = value_labels
        if missing:
            self.missing_ranges[name] = missing


def clean_text(series: pd.Series) -> pd.Series:
    """Answers as text with whitespace collapsed; blanks → None."""
    text = series.map(lambda v: re.sub(r"\s+", " ", str(v)).strip() if pd.notna(v) else None)
    return text.where(text != "", None)


def encode_variable(series: pd.Series, var: Variable, unmatched: list[str] | None = None) -> pd.Series:
    """One column → its coded form, exactly as the codebook says."""
    if var.source == "bool" or var.source == "number":
        return series.astype(float)
    if var.source == "datetime" or var.role == "date":
        if getattr(series.dt, "tz", None) is not None:
            series = series.dt.tz_localize(None)
        return series

    text = clean_text(series)
    if var.role == "numeric":
        return pd.to_numeric(text, errors="coerce").astype(float)
    if var.role == "text":
        return _merge_case_variants(text).fillna("").astype(str)

    # single: map every known spelling to its code
    lookup = {spelling: c.code for c in [*var.codes, *var.missing] for spelling in (c.match or [c.label])}
    coded = text.map(lookup)
    if unmatched is not None:
        unmatched += sorted(set(text[text.notna() & coded.isna()]))
    return coded.astype(float)


def multi_flags(series: pd.Series, delimiter: str, options: list[str]) -> list[pd.Series]:
    """One 1/0 series per option; NaN where the question wasn't answered."""
    text = series.map(lambda v: str(v).strip() if pd.notna(v) else None)
    text = text.where(text != "", None).dropna()
    selections = text.map(lambda v: {t.strip() for t in v.split(delimiter) if t.strip()})
    flags = []
    for option in options:
        f = pd.Series(float("nan"), index=series.index)
        f.loc[selections.index] = selections.map(lambda s, o=option: 1.0 if o in s else 0.0)
        flags.append(f)
    return flags


def apply_codebook(df: pd.DataFrame, codebook: Codebook) -> Coded:
    df = df.dropna(axis=0, how="all")
    by_column = {str(c): c for c in df.columns}
    coded = Coded()
    ctx = Context(codebook, coded.columns, pd.RangeIndex(len(df)))

    def emit_derived(after: str | None):
        for d in codebook.derived:
            if d.after == after:
                for name, label, values, labels, measure in compute(d, ctx):
                    coded.add(name, values, label, measure, labels)

    for var in codebook.variables:
        if var.role == "ignore":
            continue
        if var.column not in by_column:
            raise ValueError(f"Column '{var.column}' from the codebook is not in this file")
        series = df[by_column[var.column]]

        if var.role == "multi":
            for opt, flags in zip(var.options, multi_flags(series, var.delimiter, [o.label for o in var.options])):
                coded.add(opt.name, flags, f"{var.label}: {opt.label}", "nominal", {0.0: "Not selected", 1.0: "Selected"})
            emit_derived(var.anchor())
            continue

        unmatched: list[str] = []
        values = encode_variable(series, var, unmatched)
        if unmatched:
            coded.unmatched[var.name] = unmatched
        labels = ({float(k): v for k, v in var.value_labels.items()} if var.value_labels is not None
                  else {c.code: c.label for c in var.codes})
        missing = sorted(m.code for m in var.missing)
        if len(missing) > 3:  # SPSS allows at most three discrete missing values
            missing = [{"lo": min(missing), "hi": max(missing)}]
        coded.add(var.name, values, var.label, var.measure or "nominal", labels or None, missing or None)
        emit_derived(var.anchor())

    emit_derived(None)  # derived variables placed at the end
    return coded


def write_sav(coded: Coded) -> bytes:
    fd, tmp_path = tempfile.mkstemp(suffix=".sav")
    os.close(fd)
    try:
        pyreadstat.write_sav(
            pd.DataFrame(coded.columns), tmp_path,
            column_labels=coded.column_labels,
            variable_value_labels=coded.value_labels,
            missing_ranges=coded.missing_ranges,
            variable_measure=coded.measures,
        )
        with open(tmp_path, "rb") as f:
            return f.read()
    finally:
        os.unlink(tmp_path)
