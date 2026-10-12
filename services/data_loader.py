"""Format-agnostic data loading: .sav / .csv / .tsv / .xlsx / .xls → SPSSData.

Tabular formats (Excel, CSV) carry no SPSS metadata, so downstream features
(banner detection, value labels, MRX significance testing) would degrade or fail.
Instead we convert them into an in-memory .sav first:

  - Text columns with few distinct values  → numeric codes + value labels
  - Text columns with many distinct values → SPSS string variables
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

from services.quantipy_engine import QuantiProEngine, SPSSData

logger = logging.getLogger(__name__)

SPSS_FORMATS = {"sav", "zsav", "por"}
CSV_FORMATS = {"csv", "tsv"}
EXCEL_FORMATS = {"xlsx", "xls"}
SUPPORTED_FORMATS = SPSS_FORMATS | CSV_FORMATS | EXCEL_FORMATS

# Text columns with at most this many distinct values are treated as categorical
MAX_CATEGORIES = 50

DATA_SHEET = "data"
VARIABLE_LABELS_SHEET = "variable labels"
VALUE_LABELS_SHEET = "value labels"

_SPSS_RESERVED = {"ALL", "AND", "BY", "EQ", "GE", "GT", "LE", "LT", "NE", "NOT", "OR", "TO", "WITH"}
_SPSS_NAME_MAX_BYTES = 64


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
        sheets = pd.read_excel(io.BytesIO(file_bytes), sheet_name=None, engine=engine)
    except ImportError as e:
        raise ValueError(f"Reading .{fmt} files requires an extra package on the server: {e}") from e
    except Exception as e:
        raise ValueError(f"Could not read Excel file: {e}") from e

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
    measures: dict[str, str] = {}
    used_names: set[str] = set()

    for orig in df.columns:
        header = str(orig).strip()
        name = _spss_name(header, used_names)
        series, labels = _encode_column(df[orig])

        if labels is None and header in value_labels and pd.api.types.is_numeric_dtype(series):
            labels = value_labels[header]
        if labels:
            sav_value_labels[name] = labels
            measures[name] = "nominal"
        elif pd.api.types.is_numeric_dtype(series):
            measures[name] = "scale"
        else:
            measures[name] = "nominal"

        out[name] = series.reset_index(drop=True)
        column_labels.append(var_labels.get(header) or header)

    sav_df = pd.DataFrame(out)

    fd, tmp_path = tempfile.mkstemp(suffix=".sav")
    os.close(fd)
    try:
        pyreadstat.write_sav(
            sav_df, tmp_path,
            column_labels=column_labels,
            variable_value_labels=sav_value_labels,
            variable_measure=measures,
        )
        with open(tmp_path, "rb") as f:
            return f.read()
    finally:
        os.unlink(tmp_path)


def _encode_column(series: pd.Series) -> tuple[pd.Series, dict[float, str] | None]:
    """Return (sav-compatible series, value labels or None)."""
    if pd.api.types.is_bool_dtype(series):
        return series.astype(float), {0.0: "False", 1.0: "True"}
    if pd.api.types.is_numeric_dtype(series):
        return series.astype(float), None
    if pd.api.types.is_datetime64_any_dtype(series):
        if getattr(series.dt, "tz", None) is not None:
            series = series.dt.tz_localize(None)
        return series, None

    text = series.map(lambda v: str(v).strip() if pd.notna(v) else None)
    text = text.where(text != "", None)

    # Numbers stored as text (common in CSVs and hand-edited sheets)
    numeric = pd.to_numeric(text, errors="coerce")
    if text.notna().any() and numeric.notna().sum() == text.notna().sum():
        return numeric.astype(float), None

    categories = sorted(text.dropna().unique(), key=_natural_key)
    if 0 < len(categories) <= MAX_CATEGORIES:
        codes = {cat: float(i + 1) for i, cat in enumerate(categories)}
        return text.map(codes).astype(float), {code: cat for cat, code in codes.items()}

    return text.fillna("").astype(str), None


def _natural_key(s: str) -> list:
    """Sort key so '2 - Agree' precedes '10 - Strongly agree'."""
    return [(0, int(part), "") if part.isdigit() else (1, 0, part.lower()) for part in re.split(r"(\d+)", s)]


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
