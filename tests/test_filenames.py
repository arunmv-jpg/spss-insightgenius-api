"""Download names stay short enough for Excel (full path ≤ 218 characters)."""

import io
from pathlib import Path

import pytest

from shared.filenames import output_filename, short_stem

HUBSPOT = "hubspot-crm-exports-cx-pulse_w3_sept2026_schoollead-2026-10-07-2.xls"


@pytest.mark.parametrize("source,expected", [
    (HUBSPOT, "cx-pulse_w3_sept2026_schoollead"),
    ("CX Pulse Feb 2026 - anon.xls", "CX_Pulse_Feb_2026_-_anon"),
    ("survey.sav", "survey"),
    ("", "data"),
    (r"C:\Users\me\Downloads\results (final).csv", "results_final"),
])
def test_short_stem(source, expected):
    assert short_stem(source) == expected


def test_output_filename_is_short_and_safe():
    name = output_filename("analysis", HUBSPOT)
    assert name == "analysis_cx-pulse_w3_sept2026_schoollead.xlsx"
    long = output_filename("tables", "x" * 300 + ".csv")
    assert len(long) <= len("tables_") + 40 + len(".xlsx")
    # Typical Windows download path + name stays well under Excel's 218-character limit
    assert len(rf"C:\Users\someone.with.a.long.name\Downloads\{long}") < 218


def test_auto_analyze_download_name(client, auth_headers):
    sample = Path(__file__).parent / "fixtures" / "exports" / "hubspot_cx_sample.xlsx"
    response = client.post("/v1/auto-analyze", headers=auth_headers,
                           files={"file": (HUBSPOT.replace(".xls", ".xlsx"), sample.read_bytes(), "application/octet-stream")},
                           data={"options": '{"include_summary": false}'})
    assert response.status_code == 200, response.text
    assert 'filename="analysis_cx-pulse_w3_sept2026_schoollead.xlsx"' in response.headers["content-disposition"]
    from middleware import rate_limiter
    rate_limiter._windows.clear()
