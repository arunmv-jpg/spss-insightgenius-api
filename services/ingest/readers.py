"""Readers: uploaded bytes → raw respondent table (+ optional label sheets).

No interpretation happens here: values stay as the file had them. Coding decisions
are made by services.codebook.infer and applied by services.codebook.apply.
"""

import csv
import io
import logging
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

SPSS_FORMATS = {"sav", "zsav", "por"}
CSV_FORMATS = {"csv", "tsv"}
EXCEL_FORMATS = {"xlsx", "xls"}
SUPPORTED_FORMATS = SPSS_FORMATS | CSV_FORMATS | EXCEL_FORMATS

DATA_SHEET = "data"
VARIABLE_LABELS_SHEET = "variable labels"
VALUE_LABELS_SHEET = "value labels"



@dataclass
class RawTable:
    """A tabular upload before coding: one row per respondent, original headers."""
    df: pd.DataFrame
    fmt: str
    var_labels: dict[str, str] = field(default_factory=dict)        # "Variable Labels" sheet
    value_labels: dict[str, dict[Any, str]] = field(default_factory=dict)  # "Value Labels" sheet


def file_format(filename: str) -> str:
    """Lower-case extension of filename, defaulting to 'sav' when there is none."""
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else "sav"


def read_raw(file_bytes: bytes, filename: str) -> RawTable:
    """Read a CSV/TSV/Excel upload. SPSS files don't go through here (they carry metadata)."""
    fmt = file_format(filename)
    if fmt in CSV_FORMATS:
        raw = RawTable(_read_csv(file_bytes, fmt), fmt)
    elif fmt in EXCEL_FORMATS:
        df, var_labels, value_labels = _read_excel(file_bytes, fmt)
        raw = RawTable(df, fmt, var_labels, value_labels)
    else:
        raise ValueError(
            f"Unsupported file format '.{fmt}'. "
            f"Accepted: {', '.join('.' + f for f in sorted(SUPPORTED_FORMATS))}"
        )
    if raw.df.empty or len(raw.df.columns) == 0:
        raise ValueError(f"No data found in '{filename}'")
    return raw


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


# ── Formats ──────────────────────────────────────────────────────────────────

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
