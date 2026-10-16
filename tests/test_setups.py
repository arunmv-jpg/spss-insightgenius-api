"""Setups: the client rules as reusable data (RECODING_PLAN.md §6, R4)."""

import io
import json
from pathlib import Path

import pandas as pd
import pytest

from services.codebook.infer import infer_codebook
from services.data_loader import read_raw
from services.setups import apply_setup, builtin_setups, get_setup, match_setups, parse_setup

FIXTURE = Path(__file__).parent / "fixtures" / "exports" / "hubspot_cx_sample.xlsx"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture(autouse=True)
def _fresh_rate_limit():
    from middleware import rate_limiter
    yield
    rate_limiter._windows.clear()


@pytest.fixture(scope="module")
def raw():
    return read_raw(FIXTURE.read_bytes(), FIXTURE.name)


def _xlsx(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    return buf.getvalue()


def test_builtin_setups_load():
    ids = {s.id for s in builtin_setups()}
    assert {"hubspot_cx_pulse", "hubspot_cx_exam_stage"} <= ids
    for setup in builtin_setups():
        assert setup.edits and setup.match.columns
        # No client names in the repo: rules refer to columns and generic answers, never brand wording
        assert "Acme" not in json.dumps(setup.model_dump())  # the fixture's stand-in brand


def test_match_prefers_the_survey_with_all_columns(raw):
    matches = match_setups(infer_codebook(raw))
    assert matches[0]["id"] == "hubspot_cx_pulse" and matches[0]["complete"]
    exam = next(m for m in matches if m["id"] == "hubspot_cx_exam_stage")
    assert not exam["complete"] and exam["missing_columns"] == ["Exam Stage NPS"]


def test_no_match_for_an_unrelated_file():
    raw = read_raw(_xlsx(pd.DataFrame({"Age": [20, 30, 40], "Gender": ["F", "M", "F"]})), "x.xlsx")
    assert match_setups(infer_codebook(raw)) == []


def test_apply_creates_the_client_recodes(raw):
    cb, applied, skipped = apply_setup(infer_codebook(raw), get_setup("hubspot_cx_pulse"), raw.df)
    assert not skipped
    assert applied == ["Job role group", "Number of products", "Product mix", "Programmes AND/OR exams", "Drivers",
                       "Data sheets"]
    role = cb.find_derived("Job_role_group")
    assert [c.label for c in role.codes] == ["Exams Officer", "Leader", "Teacher", "Other"]
    assert role.after == "Job_role_Other"


def test_applying_twice_skips_with_a_reason(raw):
    setup = get_setup("hubspot_cx_pulse")
    cb, _, _ = apply_setup(infer_codebook(raw), setup, raw.df)
    _, applied, skipped = apply_setup(cb, setup, raw.df)
    assert applied == ["Data sheets"] and len(skipped) == len(setup.edits) - 1  # the sheet setting is just set again
    assert "already used" in skipped[0]["reason"]


def test_new_wave_without_an_option_still_gets_the_role_group():
    """Nobody ticked 'School Group/School District Leader' this wave: left out of the rule, and said so."""
    roles = ["Teacher; Exams Officer", "School Leader; Teacher", "Head of Department",
             "Head of Department; Teacher", "Teacher", "Other"]
    raw = read_raw(_xlsx(pd.DataFrame({"Job role": roles})), "wave.xlsx")
    notes: list[str] = []
    cb, applied, skipped = apply_setup(infer_codebook(raw), get_setup("hubspot_cx_pulse"), raw.df, notes)
    assert applied == ["Job role group", "Data sheets"]  # sheet columns that don't fit are left out when built
    assert {s["edit"] for s in skipped} == {"Number of products", "Product mix", "Programmes AND/OR exams", "Drivers"}
    assert any("School Group/School District Leader" in n for n in notes)


def test_parse_setup_rejects_bad_json():
    from services.setups import SetupError
    with pytest.raises(SetupError):
        parse_setup("{not json")
    with pytest.raises(SetupError):
        parse_setup({"name": "x"})  # no edits


# ── API ──────────────────────────────────────────────────────────────────────

def _post(client, auth_headers, path, data):
    return client.post(path, headers=auth_headers, data=data,
                       files={"file": (FIXTURE.name, FIXTURE.read_bytes(), XLSX_MIME)})


def test_list_endpoint(client, auth_headers):
    r = client.get("/v1/setups", headers=auth_headers)
    assert r.status_code == 200
    assert any(s["id"] == "hubspot_cx_pulse" for s in r.json()["data"]["setups"])


def test_match_and_apply_endpoints(client, auth_headers):
    r = _post(client, auth_headers, "/v1/setups/match", {})
    assert r.status_code == 200 and r.json()["data"]["matches"][0]["id"] == "hubspot_cx_pulse"

    r = _post(client, auth_headers, "/v1/setups/apply", {"setup_id": "hubspot_cx_pulse"})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["setup"]["skipped"] == [] and "Job role group" in data["setup"]["applied"]
    role = next(d for d in data["codebook"]["derived"] if d["name"] == "Job_role_group")
    assert sum(c["count"] for c in role["codes"]) == 400

    # The returned codebook drives the analysis endpoints
    r = _post(client, auth_headers, "/v1/crosstab", {"spec": json.dumps({"row": "Exam_Results_Alternative", "col": "Job_role_group"}),
                                                     "codebook": json.dumps(data["codebook"])})
    assert r.status_code == 200, r.text


def test_apply_an_exported_setup(client, auth_headers):
    setup = {"name": "Mine", "edits": [{"op": "add_derived", "derived": {
        "label": "Ease T2B", "rule": "bands", "params": {"from": "CX_Principle_Ease",
                                                         "bands": [{"code": 1, "label": "Top 2", "min": 4, "max": 5}]}}},
        {"op": "set_label", "var": "Not_a_column", "label": "x"}]}
    r = _post(client, auth_headers, "/v1/setups/apply", {"setup": json.dumps(setup)})
    assert r.status_code == 200, r.text
    report = r.json()["data"]["setup"]
    assert report["applied"] == ["Ease T2B"] and report["skipped"][0]["edit"] == "Not_a_column: set label"


def test_apply_errors(client, auth_headers):
    assert _post(client, auth_headers, "/v1/setups/apply", {}).status_code == 400
    assert _post(client, auth_headers, "/v1/setups/apply", {"setup_id": "nope"}).status_code == 404
    assert _post(client, auth_headers, "/v1/setups/apply", {"setup": "{bad"}).status_code == 400
