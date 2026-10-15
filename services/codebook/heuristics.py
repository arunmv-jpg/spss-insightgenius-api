"""Generic inference heuristics for coding survey answers (no client-specific rules: those are
setups, see services.setups).

Used by services.codebook.infer to draft a codebook; every function here is pure.
"""

import re

import pandas as pd

# Text columns with at most this many distinct values are treated as categorical
MAX_CATEGORIES = 50

# Multi-select answers exported as delimited text (HubSpot, Qualtrics/SurveyMonkey CSVs).
# A text column is treated as multi-select when at least this share of answers
# contains the delimiter and the options are a small set.
MULTI_SELECT_DELIMITER = ";"
MULTI_SELECT_MIN_SHARE = 0.05

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

_SPSS_RESERVED = {"ALL", "AND", "BY", "EQ", "GE", "GT", "LE", "LT", "NE", "NOT", "OR", "TO", "WITH"}
_SPSS_NAME_MAX_BYTES = 64
_SPSS_VALUE_LABEL_MAX_BYTES = 120


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
