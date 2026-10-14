"""Format-agnostic data loading: .sav / .csv / .tsv / .xlsx / .xls → SPSSData.

Tabular formats (Excel, CSV) carry no SPSS metadata, so downstream features
(banner detection, value labels, MRX significance testing) would degrade or fail.
Instead we convert them into an in-memory .sav first:

  - Text columns with few distinct values  → numeric codes + value labels, coded
    in scale order when the answers form a known rating scale ("Strongly disagree"
    … "Strongly agree") or carry a numeric prefix ("10 - Extremely likely").
    On scales, "Don't know" / "Not applicable" get codes 96-99 declared as SPSS
    user-missing, so they drop out of bases, means and T2B.
  - Text columns with many distinct values → SPSS string variables
  - A 0-10 "recommend" / NPS question gets a derived Detractor/Passive/Promoter variable
  - Headers are sanitized into valid SPSS names; the original header becomes
    the variable label (unless a "Variable Labels" sheet provides one)

…then load that .sav through the regular QuantiProEngine.load_spss path, so the
rest of the system sees exactly the same shape as for a native SPSS file.

Excel workbooks may optionally include the sheets written by /v1/convert:
  - "Data"            — the respondent-level data (else the first non-label sheet)
  - "Variable Labels" — columns: Variable, Label
  - "Value Labels"    — columns: Variable, Value, Label (for numeric-coded data)
"""

import csv
import io
import logging
import os
import re
import tempfile
from typing import Any

import pandas as pd
import pyreadstat

from config import get_settings
from services.quantipy_engine import QuantiProEngine, SPSSData

logger = logging.getLogger(__name__)

SPSS_FORMATS = {"sav", "zsav", "por"}
CSV_FORMATS = {"csv", "tsv"}
EXCEL_FORMATS = {"xlsx", "xls"}
SUPPORTED_FORMATS = SPSS_FORMATS | CSV_FORMATS | EXCEL_FORMATS

# Text columns with at most this many distinct values are treated as categorical
MAX_CATEGORIES = 50

# Multi-select answers exported as delimited text (HubSpot, Qualtrics/SurveyMonkey CSVs).
# A text column is treated as multi-select when at least this share of answers
# contains the delimiter and the options are a small set.
MULTI_SELECT_DELIMITER = ";"
MULTI_SELECT_MIN_SHARE = 0.05

DATA_SHEET = "data"
VARIABLE_LABELS_SHEET = "variable labels"
VALUE_LABELS_SHEET = "value labels"

# Known rating scales, lowest level first. Each level lists its accepted wordings;
# the first is the label used when that level is absent from the data. A column
# matches a scale when every substantive answer maps to a distinct level, and is
# then coded 1..k over the full scale so codes don't shift between waves.
RATING_SCALES: list[list[tuple[str, ...]]] = [
    [("Yes",), ("No",)],
    [("Strongly disagree", "completely disagree", "totally disagree"),
     ("Disagree", "somewhat disagree", "tend to disagree", "slightly disagree", "mostly disagree"),
     ("Neither agree nor disagree", "neutral", "neither"),
     ("Agree", "somewhat agree", "tend to agree", "slightly agree", "mostly agree"),
     ("Strongly agree", "completely agree", "totally agree")],
    [("Strongly disagree", "completely disagree"), ("Disagree",),
     ("Somewhat disagree", "slightly disagree", "tend to disagree"),
     ("Neither agree nor disagree", "neutral", "neither"),
     ("Somewhat agree", "slightly agree", "tend to agree"), ("Agree",),
     ("Strongly agree", "completely agree")],
    [("Very dissatisfied", "extremely dissatisfied", "completely dissatisfied"),
     ("Dissatisfied", "somewhat dissatisfied", "fairly dissatisfied", "quite dissatisfied"),
     ("Neither satisfied nor dissatisfied", "neutral", "neither"),
     ("Satisfied", "somewhat satisfied", "fairly satisfied", "quite satisfied"),
     ("Very satisfied", "extremely satisfied", "completely satisfied")],
    [("Very unlikely", "extremely unlikely", "definitely will not", "definitely not"),
     ("Unlikely", "somewhat unlikely", "fairly unlikely", "probably will not", "probably not"),
     ("Neither likely nor unlikely", "neutral", "neither", "might or might not"),
     ("Likely", "somewhat likely", "fairly likely", "probably will"),
     ("Very likely", "extremely likely", "definitely will")],
    [("Not at all important",), ("Not very important", "slightly important", "of little importance"),
     ("Moderately important", "somewhat important", "neutral"),
     ("Very important", "important"), ("Extremely important", "critical")],
    [("Very difficult", "extremely difficult"), ("Difficult", "somewhat difficult", "fairly difficult"),
     ("Neither easy nor difficult", "neutral", "neither"),
     ("Easy", "somewhat easy", "fairly easy"), ("Very easy", "extremely easy")],
    [("Never",), ("Rarely", "seldom"), ("Sometimes", "occasionally"),
     ("Often", "frequently", "usually"), ("Always", "very often")],
    [("Very poor", "terrible", "awful"), ("Poor",), ("Fair", "average", "ok", "okay"),
     ("Good",), ("Very good",), ("Excellent", "outstanding")],
]

# "Don't know"-style answers, with the user-missing code each kind gets.
_NON_SUBSTANTIVE = [
    (98, re.compile(r"^(i )?(do not|don't|dont) know|^(i'm )?(not sure|unsure)|^(can't|cannot|can not) "
                    r"(say|tell|answer|recall|remember)|^dk$")),
    (99, re.compile(r"^not applicable|^n/?a$|^does not apply|^doesn't apply")),
    (97, re.compile(r"^prefer not to|^(i'd )?rather not|^refused|^decline")),
    (96, re.compile(r"^no opinion|^no answer|^no response")),
]

# "10 - Extremely likely", "1. Poor", "3) Neutral", or a bare "5" — but not a range like "18-24"
_CODE_PREFIX = re.compile(r"^(\d{1,2})\s*(?:[-–—.:)]\s*(?=\D)|$)")

_NPS_HEADER = re.compile(r"\bnps\b|recommend", re.IGNORECASE)

# LEGACY client rule (client CX survey on HubSpot), gated by settings.client_rules_legacy and
# removed in IMPLEMENTATION_PLAN.md M4 when it becomes a codebook rule.
# "Job role" is multi-select, but analysts report on one role per respondent,
# assigned by priority. Applied when the options include "Exams Officer".
_JOB_ROLE_HEADER = re.compile(r"\bjob roles?\b", re.IGNORECASE)
JOB_ROLE_GROUP_LABELS = {1.0: "Exams Officer", 2.0: "Leader", 3.0: "Teacher", 4.0: "Other"}

_SPSS_RESERVED = {"ALL", "AND", "BY", "EQ", "GE", "GT", "LE", "LT", "NE", "NOT", "OR", "TO", "WITH"}
_SPSS_NAME_MAX_BYTES = 64
_SPSS_VALUE_LABEL_MAX_BYTES = 120


def file_format(filename: str) -> str:
    """Lower-case extension of filename, defaulting to 'sav' when there is none."""
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else "sav"


def load_data(file_bytes: bytes, filename: str = "upload.sav") -> SPSSData:
    """Load any supported data file into SPSSData.

    This is a blocking/CPU-bound operation — call via run_in_executor().

    Raises:
        ValueError: unsupported format or unreadable file contents.
    """
    fmt = file_format(filename)
    if fmt in SPSS_FORMATS:
        return QuantiProEngine.load_spss(file_bytes, filename)

    if fmt in CSV_FORMATS:
        df = _read_csv(file_bytes, fmt)
        var_labels: dict[str, str] = {}
        value_labels: dict[str, dict[Any, str]] = {}
    elif fmt in EXCEL_FORMATS:
        df, var_labels, value_labels = _read_excel(file_bytes, fmt)
    else:
        raise ValueError(
            f"Unsupported file format '.{fmt}'. "
            f"Accepted: {', '.join('.' + f for f in sorted(SUPPORTED_FORMATS))}"
        )

    if df.empty or len(df.columns) == 0:
        raise ValueError(f"No data found in '{filename}'")

    sav_bytes = _tabular_to_sav(df, var_labels, value_labels)
    data = QuantiProEngine.load_spss(sav_bytes, filename)
    logger.info("Loaded %s as SPSS: %d cases x %d variables", fmt, len(data.df), len(data.df.columns))
    return data


def read_table(file_bytes: bytes, filename: str) -> pd.DataFrame | None:
    """Raw respondent rows with original headers for Excel/CSV input; None for SPSS files."""
    fmt = file_format(filename)
    if fmt in CSV_FORMATS:
        df = _read_csv(file_bytes, fmt)
    elif fmt in EXCEL_FORMATS:
        df = _read_excel(file_bytes, fmt)[0]
    else:
        return None
    return df.dropna(axis=0, how="all").reset_index(drop=True)


# ── Readers ──────────────────────────────────────────────────────────────────

def _read_csv(file_bytes: bytes, fmt: str) -> pd.DataFrame:
    """Read CSV/TSV, sniffing the delimiter and tolerating Excel-style encodings."""
    sep = "\t" if fmt == "tsv" else ","
    try:
        sample = file_bytes[:8192].decode("utf-8", errors="replace")
        sep = csv.Sniffer().sniff(sample).delimiter
    except Exception:
        pass

    last_err: Exception | None = None
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return pd.read_csv(io.BytesIO(file_bytes), sep=sep, encoding=encoding)
        except UnicodeDecodeError as e:
            last_err = e
        except Exception as e:
            raise ValueError(f"Could not parse {fmt.upper()} file: {e}") from e
    raise ValueError(f"Could not decode {fmt.upper()} file: {last_err}")


def _read_excel(
    file_bytes: bytes, fmt: str,
) -> tuple[pd.DataFrame, dict[str, str], dict[str, dict[Any, str]]]:
    """Read the data sheet plus optional label sheets from an Excel workbook."""
    engine = "xlrd" if fmt == "xls" else "openpyxl"
    try:
        try:
            sheets = pd.read_excel(io.BytesIO(file_bytes), sheet_name=None, engine=engine)
        except (AssertionError, IndexError) as e:
            # xlrd rejects .xls files whose shared-string table was written loosely
            # (e.g. re-saved HubSpot exports); calamine reads them as Excel does.
            logger.warning("%s could not parse workbook (%r), retrying with calamine", engine, e)
            engine = "calamine"
            sheets = pd.read_excel(io.BytesIO(file_bytes), sheet_name=None, engine=engine)
    except ImportError as e:
        raise ValueError(f"Reading .{fmt} files requires an extra package on the server: {e}") from e
    except Exception as e:
        logger.exception("Excel read failed (engine=%s)", engine)
        raise ValueError(f"Could not read Excel file ({type(e).__name__}): {e or 'no details'}") from e

    by_name = {name.strip().lower(): df for name, df in sheets.items()}
    label_sheets = {VARIABLE_LABELS_SHEET, VALUE_LABELS_SHEET}

    if DATA_SHEET in by_name:
        df = by_name[DATA_SHEET]
    else:
        candidates = [df for name, df in by_name.items() if name not in label_sheets and not df.empty]
        if not candidates:
            raise ValueError("Excel workbook contains no data sheet")
        df = candidates[0]

    var_labels: dict[str, str] = {}
    vl_sheet = by_name.get(VARIABLE_LABELS_SHEET)
    if vl_sheet is not None and {"Variable", "Label"} <= set(vl_sheet.columns):
        for var, label in zip(vl_sheet["Variable"], vl_sheet["Label"]):
            if pd.notna(var) and pd.notna(label) and str(label).strip():
                var_labels[str(var)] = str(label).strip()

    value_labels: dict[str, dict[Any, str]] = {}
    val_sheet = by_name.get(VALUE_LABELS_SHEET)
    if val_sheet is not None and {"Variable", "Value", "Label"} <= set(val_sheet.columns):
        for var, value, label in zip(val_sheet["Variable"], val_sheet["Value"], val_sheet["Label"]):
            if pd.isna(var) or pd.isna(value) or pd.isna(label):
                continue
            try:
                code = float(value)
            except (TypeError, ValueError):
                continue
            value_labels.setdefault(str(var), {})[code] = str(label)

    return df, var_labels, value_labels


# ── Tabular → .sav conversion ────────────────────────────────────────────────

def _tabular_to_sav(
    df: pd.DataFrame,
    var_labels: dict[str, str],
    value_labels: dict[str, dict[Any, str]],
) -> bytes:
    """Encode a plain DataFrame as .sav bytes with labels and measurement levels."""
    df = df.dropna(axis=1, how="all").dropna(axis=0, how="all")

    out: dict[str, pd.Series] = {}
    column_labels: list[str] = []
    sav_value_labels: dict[str, dict[float, str]] = {}
    missing_ranges: dict[str, list[float]] = {}
    measures: dict[str, str] = {}
    used_names: set[str] = set()

    for orig in df.columns:
        header = str(orig).strip()

        # "Select all that apply" exported as "A;B;C" → one 0/1 variable per option,
        # labelled "Header: Option" so auto-detect groups them as a multi-response set.
        options = _split_multi_select(df[orig])
        if options is not None:
            for option, flags in options:
                name = _spss_name(f"{header}_{option}", used_names)
                out[name] = flags.reset_index(drop=True)
                column_labels.append(f"{var_labels.get(header) or header}: {option}")
                sav_value_labels[name] = {0.0: "Not selected", 1.0: "Selected"}
                measures[name] = "nominal"

            role_group = _job_role_group(header, options) if get_settings().client_rules_legacy else None
            if role_group is not None:
                group_name = _spss_name(f"{header}_group", used_names)
                out[group_name] = role_group.reset_index(drop=True)
                column_labels.append(f"{var_labels.get(header) or header} group")
                sav_value_labels[group_name] = JOB_ROLE_GROUP_LABELS
                measures[group_name] = "nominal"
            continue

        name = _spss_name(header, used_names)
        series, labels, missing = _encode_column(df[orig])

        if header in value_labels and pd.api.types.is_numeric_dtype(series):
            labels = value_labels[header]  # user-supplied labels win over inferred ones
        if labels:
            sav_value_labels[name] = labels
            measures[name] = "nominal"
        elif pd.api.types.is_numeric_dtype(series):
            measures[name] = "scale"
        else:
            measures[name] = "nominal"
        if missing:
            missing_ranges[name] = missing

        out[name] = series.reset_index(drop=True)
        label = var_labels.get(header) or header
        column_labels.append(label)

        nps_group = _nps_group(header, series, missing)
        if nps_group is not None:
            group_name = _spss_name(f"{header}_group", used_names)
            out[group_name] = nps_group.reset_index(drop=True)
            column_labels.append(f"{label}: NPS group")
            sav_value_labels[group_name] = {1.0: "Detractor (0-6)", 2.0: "Passive (7-8)", 3.0: "Promoter (9-10)"}
            measures[group_name] = "ordinal"

    sav_df = pd.DataFrame(out)

    fd, tmp_path = tempfile.mkstemp(suffix=".sav")
    os.close(fd)
    try:
        pyreadstat.write_sav(
            sav_df, tmp_path,
            column_labels=column_labels,
            variable_value_labels=sav_value_labels,
            missing_ranges=missing_ranges,
            variable_measure=measures,
        )
        with open(tmp_path, "rb") as f:
            return f.read()
    finally:
        os.unlink(tmp_path)


def _split_multi_select(series: pd.Series) -> list[tuple[str, pd.Series]] | None:
    """Detect a delimited multi-select column and expand it into 0/1 flags.

    Returns [(option, flags)] or None if the column isn't multi-select. Flags are
    1 = selected, 0 = answered but not selected, NaN = question not answered.
    """
    if pd.api.types.is_numeric_dtype(series) or pd.api.types.is_datetime64_any_dtype(series):
        return None
    text = series.map(lambda v: str(v).strip() if pd.notna(v) else None)
    text = text.where(text != "", None).dropna()
    if text.empty:
        return None

    delimited = text.str.contains(MULTI_SELECT_DELIMITER, regex=False)
    if delimited.mean() < MULTI_SELECT_MIN_SHARE:
        return None

    selections = text.map(
        lambda v: {t.strip() for t in v.split(MULTI_SELECT_DELIMITER) if t.strip()})
    options = sorted(set().union(*selections), key=_nominal_key)
    # Free text that happens to contain ";" explodes into many distinct fragments
    if not 2 <= len(options) <= MAX_CATEGORIES or len(options) >= text.nunique():
        return None

    result = []
    for option in options:
        flags = pd.Series(float("nan"), index=series.index)
        flags.loc[selections.index] = selections.map(lambda s, o=option: 1.0 if o in s else 0.0)
        result.append((option, flags))
    return result


def _job_role_group(header: str, options: list[tuple[str, pd.Series]]) -> pd.Series | None:
    """One role per respondent from a multi-select job role question, by priority:

    1 Exams Officer — ticked Exams Officer, whatever else they ticked
    2 Leader        — School (Group/District) Leader, or Head of Department who isn't a Teacher
    3 Teacher       — ticked Teacher
    4 Other         — anything else (only "Other", unknown roles)
    """
    flags = {_normalize(option): selected for option, selected in options}
    if not _JOB_ROLE_HEADER.search(header) or "exams officer" not in flags:
        return None
    answered = options[0][1].notna()
    none = pd.Series(False, index=answered.index)

    def ticked(*roles: str) -> pd.Series:
        result = none
        for role in roles:
            result = result | flags.get(role, none).eq(1)
        return result

    hod, teacher = ticked("head of department"), ticked("teacher")
    group = pd.Series(4.0, index=answered.index)
    # Assign lowest priority first so higher-priority roles overwrite it
    group[teacher] = 3.0
    group[hod & ~teacher] = 2.0
    group[ticked("school leader", "school group/school district leader")] = 2.0
    group[ticked("exams officer")] = 1.0
    return group.where(answered)


def _encode_column(series: pd.Series) -> tuple[pd.Series, dict[float, str] | None, list | None]:
    """Return (sav-compatible series, value labels or None, SPSS missing_ranges or None)."""
    if pd.api.types.is_bool_dtype(series):
        return series.astype(float), {0.0: "False", 1.0: "True"}, None
    if pd.api.types.is_numeric_dtype(series):
        series = series.astype(float)
        return series, _rating_labels(series), None
    if pd.api.types.is_datetime64_any_dtype(series):
        if getattr(series.dt, "tz", None) is not None:
            series = series.dt.tz_localize(None)
        return series, None, None

    text = series.map(lambda v: re.sub(r"\s+", " ", str(v)).strip() if pd.notna(v) else None)
    text = _merge_case_variants(text.where(text != "", None))

    # Numbers stored as text (common in CSVs and hand-edited sheets)
    numeric = pd.to_numeric(text, errors="coerce")
    if text.notna().any() and numeric.notna().sum() == text.notna().sum():
        return numeric.astype(float), None, None

    categories = sorted(text.dropna().unique(), key=_natural_key)
    # Export metadata (e.g. HubSpot "Source" = "Shareable Link" on every row) isn't a question
    is_constant = len(categories) == 1 and text.notna().all()
    if is_constant or not 0 < len(categories) <= MAX_CATEGORIES or _looks_like_free_text(text.dropna(), categories):
        return text.fillna("").astype(str), None, None

    labels, missing = _code_frame(categories)
    codes = {label: code for code, label in labels.items()}
    if missing:
        # User-missing values load as NaN, so a label would only add an empty row to tables
        labels = {code: label for code, label in labels.items() if code not in missing}
        if len(missing) > 3:  # SPSS allows at most three discrete missing values
            missing = [{"lo": min(missing), "hi": max(missing)}]
    return text.map(codes).astype(float), labels, missing


def _merge_case_variants(text: pd.Series) -> pd.Series:
    """Collapse answers differing only in case ("agree" / "Agree") onto the most common spelling."""
    counts = text.value_counts()
    canonical: dict[str, str] = {}
    for spelling in sorted(counts.index, key=lambda s: (-counts[s], s)):
        canonical.setdefault(spelling.casefold(), spelling)
    if len(canonical) == len(counts):
        return text
    return text.map(lambda v: canonical[v.casefold()] if isinstance(v, str) else None)


def _code_frame(categories: list[str]) -> tuple[dict[float, str], list[float] | None]:
    """Assign codes to text answers: numeric prefix > known rating scale > natural order.

    On ordered (prefix/scale) questions, "Don't know"-type answers become
    user-missing codes so they're excluded from bases and means. On nominal
    questions they stay valid categories, listed last.
    """
    substantive = [c for c in categories if not is_non_substantive(c)]
    non_substantive = [c for c in categories if is_non_substantive(c)]

    ordered = None
    if len(substantive) >= 2:
        ordered = _prefix_codes(substantive) or _scale_codes(substantive)
    if ordered is None:
        labels = {float(i + 1): c for i, c in enumerate(sorted(substantive, key=_nominal_key))}
        return {**labels, **_non_substantive_codes(non_substantive, set(labels))}, None

    extra = _non_substantive_codes(non_substantive, set(ordered))
    return {**ordered, **extra}, sorted(extra) or None


def _prefix_codes(categories: list[str]) -> dict[float, str] | None:
    """Use leading numbers as codes: "0 - Not at all likely" … "10 - Extremely likely"."""
    codes: dict[float, str] = {}
    for cat in categories:
        m = _CODE_PREFIX.match(cat)
        if not m or float(m.group(1)) in codes:
            return None
        codes[float(m.group(1))] = cat
    return dict(sorted(codes.items()))


def _scale_codes(categories: list[str]) -> dict[float, str] | None:
    """Code answers by their position in the first known rating scale they all fit."""
    for levels in RATING_SCALES:
        lookup = {_normalize(w): i for i, level in enumerate(levels) for w in level}
        positions = [lookup.get(_normalize(c)) for c in categories]
        if None in positions or len(set(positions)) != len(positions):
            continue
        present = dict(zip(positions, categories))
        return {float(i + 1): present.get(i, level[0]) for i, level in enumerate(levels)}
    return None


def _non_substantive_codes(categories: list[str], used: set[float]) -> dict[float, str]:
    """Codes 96-99 by kind (98 = don't know, 99 = not applicable, ...), always above valid codes."""
    floor = max(used, default=0.0)
    result: dict[float, str] = {}
    for cat in sorted(categories, key=_natural_key):
        code = float(next(c for c, pattern in _NON_SUBSTANTIVE if pattern.search(_normalize(cat))))
        while code in result or code in used:
            code -= 1
        if code <= floor:
            code = max([floor, *result]) + 1
        result[code] = cat
    return result


def is_non_substantive(label: str) -> bool:
    """True for "Don't know" / "Not applicable" / "Prefer not to say"-type answers."""
    norm = _normalize(label)
    return any(pattern.search(norm) for _, pattern in _NON_SUBSTANTIVE)


def _normalize(label: str) -> str:
    """Comparable form of an answer: lower-case, straight quotes, "nor" → "or" agnostic."""
    s = str(label).casefold().replace("’", "'").replace("‘", "'").replace("&", "and")
    s = re.sub(r"\bnor\b", "or", s)
    s = re.sub(r"[\s_]+", " ", s)
    return s.strip(" .!")


def _nps_group(header: str, series: pd.Series, missing: list | None) -> pd.Series | None:
    """Detractor/Passive/Promoter (1/2/3) from a 0-10 recommend question, else None."""
    if not _NPS_HEADER.search(header) or not pd.api.types.is_numeric_dtype(series):
        return None
    missing_values = {m for m in missing or [] if not isinstance(m, dict)}
    for m in missing or []:
        if isinstance(m, dict):
            missing_values |= set(series[(series >= m["lo"]) & (series <= m["hi"])].unique())
    valid = series.where(~series.isin(missing_values))
    values = valid.dropna()
    if values.empty or values.min() < 0 or values.max() > 10 or values.max() < 7 or (values % 1 != 0).any():
        return None
    return pd.cut(valid, bins=[-1, 6, 8, 10], labels=[1.0, 2.0, 3.0]).astype(float)


def _looks_like_free_text(answers: pd.Series, categories: list[str]) -> bool:
    """Open-ended answers: mostly unique, or too long for an SPSS value label (120 bytes)."""
    if any(len(c.encode("utf-8")) > _SPSS_VALUE_LABEL_MAX_BYTES for c in categories):
        return True
    # A sparse "please specify" column: every answer different, so nothing to tabulate —
    # unless the answers form a recognisable scale (a few respondents on a 0-10 question)
    if len(categories) >= 5 and len(categories) == len(answers):
        substantive = [c for c in categories if not is_non_substantive(c)]
        return not (_prefix_codes(substantive) or _scale_codes(substantive))
    return len(categories) > 10 and len(categories) > 0.5 * len(answers)


def _rating_labels(series: pd.Series) -> dict[float, str] | None:
    """Identity labels for integer rating scales (e.g. NPS 0-10, Likert 1-5).

    Unlabelled numeric columns are skipped by auto-analyze, so without this the
    core metrics of a survey export would never be tabulated. Means are unaffected.
    """
    values = series.dropna().unique()
    if not 2 <= len(values) <= 11:
        return None
    if values.min() < 0 or values.max() > 10 or any(v != int(v) for v in values):
        return None
    return {float(v): str(int(v)) for v in sorted(values)}


def _natural_key(s: str) -> list:
    """Sort key so '2 - Agree' precedes '10 - Strongly agree'."""
    return [(0, int(part), "") if part.isdigit() else (1, 0, part.lower()) for part in re.split(r"(\d+)", s)]


def _nominal_key(s: str) -> tuple:
    """Natural order, with "Other…" / "None of the above" after the regular options."""
    return (bool(re.match(r"^(other|none of the above)\b", _normalize(s))), _natural_key(s))


def _spss_name(header: str, used: set[str]) -> str:
    """Turn an arbitrary header into a unique, valid SPSS variable name."""
    name = re.sub(r"[^\w.@#$]", "_", header, flags=re.UNICODE)
    name = re.sub(r"[._]*_[._]*", "_", name).strip("_").rstrip(".")
    if not name or not (name[0].isalpha() or name[0] == "@"):
        name = f"V_{name}" if name else "V"
    if name.upper() in _SPSS_RESERVED:
        name += "_"
    name = _truncate_bytes(name, _SPSS_NAME_MAX_BYTES)

    candidate, n = name, 1
    while candidate.lower() in used:
        n += 1
        suffix = f"_{n}"
        candidate = _truncate_bytes(name, _SPSS_NAME_MAX_BYTES - len(suffix)) + suffix
    used.add(candidate.lower())
    return candidate


def _truncate_bytes(s: str, max_bytes: int) -> str:
    encoded = s.encode("utf-8")
    if len(encoded) <= max_bytes:
        return s
    return encoded[:max_bytes].decode("utf-8", errors="ignore")
