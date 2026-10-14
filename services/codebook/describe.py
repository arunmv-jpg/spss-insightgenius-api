"""Fill a codebook with counts so people can judge each decision (review payload)."""

from collections import Counter

import pandas as pd

from services.codebook.model import Codebook


def annotate_counts(codebook: Codebook, columns: dict[str, pd.Series]) -> Codebook:
    """Counts per code / option / derived category from coded columns (missing codes still present)."""
    cb = codebook.model_copy(deep=True)
    for var in cb.variables:
        if var.role == "multi":
            flags = [columns.get(o.name) for o in var.options]
            for opt, f in zip(var.options, flags):
                opt.count = int((f == 1).sum()) if f is not None else None
            first = next((f for f in flags if f is not None), None)
            var.base = int(first.notna().sum()) if first is not None else None
            continue
        series = columns.get(var.name) if var.name else None
        if series is None:
            continue
        if var.role == "text":
            var.base = int((series.astype(str).str.strip() != "").sum())
            continue
        missing = {m.code for m in var.missing}
        var.base = int((series.notna() & ~series.isin(missing)).sum())
        counts = Counter(series.dropna().tolist())
        for code in [*var.codes, *var.missing]:
            code.count = int(counts.get(code.code, 0))
    for d in cb.derived:
        for opt in d.outputs:
            flags = columns.get(opt.name)
            opt.count = int((flags == 1).sum()) if flags is not None else None
        series = columns.get(d.name)
        if series is not None:
            counts = Counter(series.dropna().tolist())
            for code in d.codes:
                code.count = int(counts.get(code.code, 0))
    return cb


def summarize(codebook: Codebook, unmatched: dict[str, list[str]] | None = None) -> dict:
    roles = Counter(v.role for v in codebook.variables)
    return {
        "columns": len(codebook.variables),
        "output_variables": len(codebook.output_names()),
        "by_role": dict(sorted(roles.items())),
        "derived": len(codebook.derived),
        "needs_review": sum(v.status == "needs_review" for v in codebook.variables),
        "unmatched_answers": unmatched or {},
    }
