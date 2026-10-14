"""Short, safe names for downloaded files.

Excel refuses to open a file whose full path is longer than 218 characters, and
survey-platform exports already have long names (e.g. "hubspot-crm-exports-cx-pulse_
w3_sept2026_schoollead-2026-10-07-2.xls"). Output names are therefore kept short:
"<kind>_<short source name>.<ext>", using only characters every OS accepts.
"""

import re

MAX_STEM = 40

# Boilerplate prefixes survey platforms put in front of every export name
_EXPORT_PREFIXES = re.compile(r"^(hubspot[-_]crm[-_]exports?[-_]|hubspot[-_]|qualtrics[-_]|surveymonkey[-_])", re.I)


def short_stem(filename: str | None, max_len: int = MAX_STEM) -> str:
    """'hubspot-crm-exports-cx-pulse_w3_sept2026_schoollead-2026-10-07-2.xls' → 'cx-pulse_w3_sept2026_schoollead'."""
    stem = (filename or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    stem = stem.rsplit(".", 1)[0] if "." in stem else stem
    stem = _EXPORT_PREFIXES.sub("", stem)
    stem = re.sub(r"[-_ ]?\d{4}-\d{2}-\d{2}([-_]\d+)?$", "", stem)  # trailing export date "-2026-10-07-2"
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_-")
    if len(stem) > max_len:
        stem = stem[:max_len].rstrip("_-")
    return stem or "data"


def output_filename(kind: str, source_filename: str | None, extension: str = ".xlsx") -> str:
    """e.g. output_filename('analysis', 'hubspot-crm-exports-cx-pulse_….xls') → 'analysis_cx-pulse_w3_sept2026_schoollead.xlsx'."""
    extension = extension if extension.startswith(".") else f".{extension}"
    return f"{kind}_{short_stem(source_filename)}{extension}"
