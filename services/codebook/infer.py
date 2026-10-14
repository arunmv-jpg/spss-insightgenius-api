"""Draft a codebook from a raw table (step ③ of the pipeline).

Every decision gets an `Inferred(rule, reason, confidence)` so the review UI can
explain it. Generic heuristics only; the one client rule (job-role group) is gated
by settings.client_rules_legacy and removed in IMPLEMENTATION_PLAN.md M4.
"""

import pandas as pd

from config import get_settings
from services.codebook.apply import clean_text, encode_variable
from services.codebook.heuristics import (
    JOB_ROLE_GROUP_LABELS,
    MAX_CATEGORIES,
    MULTI_SELECT_DELIMITER,
    _code_frame,
    _job_role_group,
    _looks_like_free_text,
    _merge_case_variants,
    _natural_key,
    _nps_group,
    _prefix_codes,
    _rating_labels,
    _scale_codes,
    _spss_name,
    _split_multi_select,
    is_non_substantive,
)
from services.codebook.model import Code, Codebook, Derived, Inferred, Option, Source, Variable
from services.ingest.readers import RawTable

NPS_BANDS = [
    {"code": 1.0, "label": "Detractor (0-6)", "min": 0, "max": 6},
    {"code": 2.0, "label": "Passive (7-8)", "min": 7, "max": 8},
    {"code": 3.0, "label": "Promoter (9-10)", "min": 9, "max": 10},
]


def infer_codebook(raw: RawTable) -> Codebook:
    """Infer how every column should be coded. Names are assigned in file order."""
    df = raw.df.dropna(axis=0, how="all")
    used: set[str] = set()
    variables: list[Variable] = []
    derived: list[Derived] = []

    for orig in df.columns:
        column, header = str(orig), str(orig).strip()
        series = df[orig]
        label = raw.var_labels.get(header) or header

        if series.isna().all():
            variables.append(Variable(column=column, label=label, role="ignore", inferred=Inferred(
                rule="empty", reason="No answers in this column")))
            continue

        options = _split_multi_select(series)
        if options is not None:
            var = _multi_variable(column, header, label, series, options, used)
            variables.append(var)
            if get_settings().client_rules_legacy:
                group = _job_role_group(header, options)
                if group is not None:
                    derived.append(Derived(
                        name=_spss_name(f"{header}_group", used), label=f"{label} group",
                        rule="legacy_job_role_group", source=column, after=var.options[-1].name,
                        codes=[Code(code=k, label=v) for k, v in JOB_ROLE_GROUP_LABELS.items()],
                        inferred=Inferred(rule="legacy:job_role_group",
                                          reason="Client rule: one role per respondent by priority"),
                    ))
            continue

        var = _single_variable(column, header, label, series, used)
        if header in raw.value_labels and var.role in ("single", "numeric"):
            # A "Value Labels" sheet in the workbook wins over inferred labels
            var.value_labels = {repr(float(k)): str(v) for k, v in raw.value_labels[header].items()}
            var.measure = "nominal"
        variables.append(var)

        nps = _nps_derived(var, header, series)
        if nps is not None:
            nps.name = _spss_name(f"{header}_group", used)
            derived.append(nps)

    return Codebook(
        source=Source(format=raw.fmt, rows=len(df), columns=len(df.columns)),
        variables=variables, derived=derived,
    )


def _multi_variable(column, header, label, series, options, used) -> Variable:
    answered = series.dropna().astype(str).str.strip()
    answered = answered[answered != ""]
    share = answered.str.contains(MULTI_SELECT_DELIMITER, regex=False).mean() if len(answered) else 0.0
    return Variable(
        column=column, label=label, role="multi", measure="nominal", source="text",
        delimiter=MULTI_SELECT_DELIMITER,
        options=[Option(label=o, name=_spss_name(f"{header}_{o}", used)) for o, _ in options],
        inferred=Inferred(rule="multi:delimited", confidence=0.9,
                          reason=f"{share:.0%} of answers list several options separated by "
                                 f"'{MULTI_SELECT_DELIMITER}' ({len(options)} distinct options)"),
    )


def _single_variable(column, header, label, series, used) -> Variable:
    name = _spss_name(header, used)
    base = dict(column=column, name=name, label=label)

    if pd.api.types.is_bool_dtype(series):
        return Variable(**base, role="single", measure="nominal", source="bool",
                        codes=[Code(code=0.0, label="False"), Code(code=1.0, label="True")],
                        inferred=Inferred(rule="bool", reason="True/False values"))
    if pd.api.types.is_numeric_dtype(series):
        labels = _rating_labels(series.astype(float))
        if labels:
            return Variable(**base, role="numeric", measure="nominal", source="number",
                            codes=[Code(code=k, label=v) for k, v in labels.items()],
                            inferred=Inferred(rule="numeric:rating", reason=(
                                f"Whole numbers {min(labels):g}-{max(labels):g}: treated as a rating scale")))
        return Variable(**base, role="numeric", measure="scale", source="number",
                        inferred=Inferred(rule="numeric", reason="Numbers"))
    if pd.api.types.is_datetime64_any_dtype(series):
        return Variable(**base, role="date", measure="nominal", source="datetime",
                        inferred=Inferred(rule="date", reason="Dates/times"))

    raw_text = clean_text(series)
    text = _merge_case_variants(raw_text)
    numeric = pd.to_numeric(text, errors="coerce")
    if text.notna().any() and numeric.notna().sum() == text.notna().sum():
        return Variable(**base, role="numeric", measure="scale", source="text",
                        inferred=Inferred(rule="numeric:text", reason="Numbers stored as text"))

    categories = sorted(text.dropna().unique(), key=_natural_key)
    if len(categories) == 1 and text.notna().all():
        return _text(base, "text:constant", f"Same answer in every row ('{categories[0]}')")
    if len(categories) > MAX_CATEGORIES:
        return _text(base, "text:many", f"{len(categories)} different answers (more than {MAX_CATEGORIES})")
    if _looks_like_free_text(text.dropna(), categories):
        return _text(base, "text:open", f"Open answers: {len(categories)} different of {int(text.notna().sum())}")

    labels, missing = _code_frame(categories)
    spellings: dict[str, list[str]] = {}
    for spelling, canonical in zip(raw_text, text):
        if isinstance(spelling, str) and spelling not in spellings.setdefault(canonical, []):
            spellings[canonical].append(spelling)
    missing_codes = set(missing or [])
    codes = [Code(code=c, label=lab, match=sorted(spellings.get(lab, []))) for c, lab in labels.items()
             if c not in missing_codes]
    missing_list = [Code(code=c, label=lab, match=sorted(spellings.get(lab, []))) for c, lab in labels.items()
                    if c in missing_codes]

    substantive = [c for c in categories if not is_non_substantive(c)]
    if len(substantive) >= 2 and _prefix_codes(substantive):
        inferred = Inferred(rule="scale:numeric_prefix", reason="Answers start with their code (e.g. '10 - …')")
    elif len(substantive) >= 2 and _scale_codes(substantive):
        inferred = Inferred(rule="scale:known", confidence=0.95,
                            reason=f"Matches a known {len(codes)}-point rating scale; coded in scale order")
    else:
        inferred = Inferred(rule="nominal", confidence=0.8,
                            reason=f"{len(codes)} categories, coded alphabetically ('Other' last)")
    if missing_list:
        inferred.reason += f"; {len(missing_list)} 'don't know'-type answer(s) excluded from bases"
    return Variable(**base, role="single", measure="nominal", source="text",
                    codes=codes, missing=missing_list, inferred=inferred)


def _text(base: dict, rule: str, reason: str) -> Variable:
    return Variable(**base, role="text", measure="nominal", source="text",
                    inferred=Inferred(rule=rule, reason=reason))


def _nps_derived(var: Variable, header: str, series: pd.Series) -> Derived | None:
    """Suggest Detractor/Passive/Promoter for a 0-10 recommend/NPS question."""
    if var.role not in ("single", "numeric") or not var.name:
        return None
    coded = encode_variable(series, var)
    missing = [m.code for m in var.missing] or None
    if _nps_group(header, coded, missing) is None:
        return None
    return Derived(
        name="", label=f"{var.label}: NPS group", rule="bands", source=var.name, after=var.name,
        params={"bands": NPS_BANDS}, measure="ordinal",
        codes=[Code(code=b["code"], label=b["label"]) for b in NPS_BANDS],
        inferred=Inferred(rule="nps", reason="0-10 'recommend' question: NPS groups 0-6 / 7-8 / 9-10"),
    )
