"""Codebook editing + derived-variable rules (IMPLEMENTATION_PLAN.md M4)."""

import json
from pathlib import Path

import pandas as pd
import pytest

from services.codebook.apply import apply_codebook
from services.codebook.infer import infer_codebook
from services.codebook.patch import EditError, apply_ops
from services.data_loader import load_data
from services.ingest.readers import read_raw
from services.respondent_sheet import build_columns

FIXTURE = Path(__file__).parent / "fixtures" / "exports" / "hubspot_cx_sample.xlsx"
CLIENT_RULES = Path(__file__).parent / "fixtures" / "codebooks" / "cx_client_rules.json"
SAV = Path(__file__).parent / "fixtures" / "example3_raw.sav"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
EXAM = "Exam_Results_Alternative"


@pytest.fixture(scope="module")
def raw():
    return read_raw(FIXTURE.read_bytes(), FIXTURE.name)


@pytest.fixture(scope="module")
def cb(raw):
    return infer_codebook(raw)


def coded(raw, cb, edits):
    edited = apply_ops(cb, edits, raw.df)
    return edited, apply_codebook(raw.df, edited).columns


@pytest.fixture(autouse=True)
def _fresh_rate_limit():
    from middleware import rate_limiter
    yield
    rate_limiter._windows.clear()


# ── variable edits ───────────────────────────────────────────────────────────

def test_set_label_and_measure(raw, cb):
    edited, _ = coded(raw, cb, [{"op": "set_label", "var": EXAM, "label": "Results were fair"},
                                {"op": "set_measure", "var": EXAM, "measure": "ordinal"}])
    var = edited.variable(EXAM)
    assert (var.label, var.measure) == ("Results were fair", "ordinal")
    assert cb.variable(EXAM).label != "Results were fair"  # original untouched


def test_set_missing_moves_code_out_of_base(raw, cb):
    edited, cols = coded(raw, cb, [{"op": "set_missing", "var": EXAM, "code": "Neither agree or disagree"}])
    var = edited.variable(EXAM)
    assert 3.0 in [m.code for m in var.missing] and 3.0 not in [c.code for c in var.codes]
    data = load_data(FIXTURE.read_bytes(), FIXTURE.name, codebook=edited)
    assert 3.0 not in set(data.df[EXAM].dropna())  # user-missing → excluded on load
    assert (cols[EXAM] == 3.0).sum() == (raw.df["Exam Results Alternative"] == "Neither agree or disagree").sum()


def test_merge_codes_keeps_target_code(raw, cb):
    edited, cols = coded(raw, cb, [{"op": "merge_codes", "var": EXAM, "codes": ["Agree", "Strongly Agree"], "into": 4}])
    agree_raw = raw.df["Exam Results Alternative"].isin(["Agree", "Strongly Agree"]).sum()
    assert (cols[EXAM] == 4.0).sum() == agree_raw
    assert 5.0 not in [c.code for c in edited.variable(EXAM).codes]


def test_relabel_keeps_matching_raw_answer(raw, cb):
    edited, cols = coded(raw, cb, [{"op": "relabel_code", "var": EXAM, "code": 4, "label": "Agree (net)"}])
    assert (cols[EXAM] == 4.0).sum() == (raw.df["Exam Results Alternative"] == "Agree").sum()
    assert edited.variable(EXAM).codes[3].label == "Agree (net)"


def test_reorder_codes_renumbers(raw, cb):
    order = ["Strongly Agree", "Agree", "Neither agree or disagree", "Disagree", "Strongly Disagree"]
    edited, cols = coded(raw, cb, [{"op": "reorder_codes", "var": EXAM, "order": order}])
    assert [c.label for c in edited.variable(EXAM).codes] == order
    assert (cols[EXAM] == 1.0).sum() == (raw.df["Exam Results Alternative"] == "Strongly Agree").sum()


def test_set_role_ignore_and_text_and_single(raw, cb):
    edited, cols = coded(raw, cb, [{"op": "set_role", "var": "Source", "role": "ignore"},
                                   {"op": "set_role", "var": EXAM, "role": "text"},
                                   {"op": "set_role", "var": "Sentiment", "role": "single"}])
    assert "Source" not in cols and cols[EXAM].iloc[0] in set(raw.df["Exam Results Alternative"])
    assert [c.label for c in edited.variable("Sentiment").codes] == ["Not Applicable"]
    assert (cols["Sentiment"] == 1.0).all()


def test_set_role_single_refuses_open_text(raw, cb):
    with pytest.raises(EditError, match="too many to code"):
        apply_ops(cb, [{"op": "set_role", "var": "Centre_Name", "role": "single"}], raw.df)


def test_unknown_variable_and_op_rejected(raw, cb):
    with pytest.raises(EditError, match="no variable"):
        apply_ops(cb, [{"op": "set_label", "var": "Nope", "label": "x"}], raw.df)
    with pytest.raises(EditError, match="unknown operation"):
        apply_ops(cb, [{"op": "explode"}], raw.df)


# ── rules ────────────────────────────────────────────────────────────────────

def test_recode_and_bands(raw, cb):
    edits = [
        {"op": "add_derived", "derived": {"label": "Exam T2B", "rule": "recode", "params": {
            "from": EXAM, "groups": [{"code": 1, "label": "Agree (T2B)", "values": ["Agree", "Strongly Agree"]},
                                     {"code": 2, "label": "Other", "values": [1, 2, 3]}]}}},
        {"op": "add_derived", "derived": {"label": "NPS bands", "rule": "bands", "params": {
            "from": "CX_Pulse_Overall_NPS", "bands": [{"code": 1, "label": "Low", "min": 0, "max": 6},
                                                      {"code": 2, "label": "High", "min": 7, "max": 10}]}}},
    ]
    edited, cols = coded(raw, cb, edits)
    t2b = raw.df["Exam Results Alternative"].isin(["Agree", "Strongly Agree"]).sum()
    assert (cols["Exam_T2B"] == 1.0).sum() == t2b
    assert cols["Exam_T2B"].isna().sum() == raw.df["Exam Results Alternative"].str.contains("know|applicable").sum()
    assert (cols["NPS_bands"] == 2.0).sum() == (raw.df["CX Pulse Overall NPS"] >= 7).sum()
    assert [c.label for c in edited.find_derived("NPS_bands").codes] == ["Low", "High"]


def test_count_without_bands_is_numeric(raw, cb):
    _, cols = coded(raw, cb, [{"op": "add_derived", "derived": {
        "label": "Roles ticked", "rule": "count", "params": {"from": "Job role"}}}])
    expected = raw.df["Job role"].map(lambda v: len(v.split(";")))
    assert cols["Roles_ticked"].tolist() == expected.astype(float).tolist()


def test_invalid_rule_reference_rejected(raw, cb):
    with pytest.raises(EditError, match="not an option"):
        apply_ops(cb, [{"op": "add_derived", "derived": {"label": "x", "rule": "priority", "params": {
            "base": "Job role", "groups": [{"code": 1, "label": "a", "when": {"var": "Job role", "any": ["Pilot"]}}]}}}],
            raw.df)
    with pytest.raises(EditError, match="needs a label"):
        apply_ops(cb, [{"op": "add_derived", "derived": {"rule": "bands", "params": {}}}], raw.df)


def test_remove_and_update_derived(raw, cb):
    nps = cb.derived[-1].name
    edited, cols = coded(raw, cb, [{"op": "remove_derived", "name": nps}])
    assert nps not in cols
    edited, cols = coded(raw, cb, [{"op": "update_derived", "name": nps, "derived": {
        "label": "NPS 2 groups", "rule": "bands", "params": {
            "from": "CX_Pulse_Overall_NPS", "bands": [{"code": 1, "label": "0-8", "min": 0, "max": 8},
                                                      {"code": 2, "label": "9-10", "min": 9, "max": 10}]}}}])
    assert set(cols[nps].dropna()) == {1.0, 2.0}


def test_name_collision_rejected(raw, cb):
    with pytest.raises(EditError, match="already used"):
        apply_ops(cb, [{"op": "add_derived", "derived": {"name": EXAM, "label": "dup", "rule": "flags",
                                                          "params": {"from": EXAM}}}], raw.df)


# ── the client rules, as data ────────────────────────────────────────────────

@pytest.fixture(scope="module")
def client_coded(raw, cb):
    edits = json.loads(CLIENT_RULES.read_text(encoding="utf-8"))["edits"]
    edited = apply_ops(cb, edits, raw.df)
    return edited, apply_codebook(raw.df, edited).columns


def test_client_role_rule_matches_legacy_code(client_coded):
    _, cols = client_coded
    pd.testing.assert_series_equal(cols["Role_group"], cols["Job_role_group"], check_names=False)


def test_client_product_and_driver_rules_match_analyst_sheet(raw, client_coded):
    edited, cols = client_coded
    sheet = {h: pd.to_numeric(pd.Series(v), errors="coerce").fillna(0).reset_index(drop=True)
             for h, v, _ in build_columns(raw.df) if isinstance(h, str)}
    for code, column in [(1.0, "Single product"), (2.0, "Dual product"), (3.0, "Triple product")]:
        assert ((cols["Product_count"] == code).astype(float) == sheet[column]).all(), column
    for code, column in [(1.0, "Resources only"), (3.0, "Exams only"), (6.0, "Resources + Exams")]:
        assert ((cols["Product_mix"] == code).astype(float) == sheet[column]).all(), column
    assert ((cols["Programmes_or_exams"] == 1.0).astype(float) == sheet["Programmes AND/OR exams"]).all()
    for opt in edited.find_derived("DRIVERS").outputs:
        assert (cols[opt.name].fillna(0) == sheet[opt.label]).all(), opt.label


def test_client_rules_place_role_group_after_job_role(client_coded):
    edited, _ = client_coded
    names = edited.output_names()
    assert names.index("Role_group") == names.index("Job_role_Other") + 2  # after the legacy group too


# ── API ──────────────────────────────────────────────────────────────────────

def _post(client, auth_headers, path, data, file=FIXTURE, mime=XLSX_MIME):
    return client.post(path, headers=auth_headers, files={"file": (file.name, file.read_bytes(), mime)}, data=data)


def test_edit_endpoint_and_crosstab_with_edited_codebook(client, auth_headers):
    edits = [{"op": "merge_codes", "var": EXAM, "codes": ["Agree", "Strongly Agree"], "into": "Agree"},
             {"op": "relabel_code", "var": EXAM, "code": 4, "label": "Agree (any)"}]
    response = _post(client, auth_headers, "/v1/codebook", {"edits": json.dumps(edits)})
    assert response.status_code == 200, response.text
    codebook = response.json()["data"]["codebook"]
    exam = next(v for v in codebook["variables"] if v["name"] == EXAM)
    assert [c["label"] for c in exam["codes"]][-1] == "Agree (any)" and exam["codes"][-1]["count"] > 0

    spec = {"row": EXAM, "col": "CX_Pulse_Overall_NPS_group"}
    xtab = _post(client, auth_headers, "/v1/crosstab", {"spec": json.dumps(spec), "codebook": json.dumps(codebook)})
    assert xtab.status_code == 200, xtab.text
    labels = [r["row_label"] for r in xtab.json()["data"]["table"]]
    assert "Agree (any)" in labels and "Strongly Agree" not in labels


def test_edit_endpoint_rejects_bad_edits(client, auth_headers):
    bad = _post(client, auth_headers, "/v1/codebook", {"edits": json.dumps([{"op": "set_label", "var": "Nope", "label": "x"}])})
    assert bad.status_code == 400 and "no variable" in bad.json()["detail"]["message"]
    malformed = _post(client, auth_headers, "/v1/codebook", {"edits": "{not json"})
    assert malformed.status_code == 400


def test_edit_endpoint_spss_not_supported(client, auth_headers):
    response = _post(client, auth_headers, "/v1/codebook",
                     {"edits": json.dumps([{"op": "set_label", "var": "Q_1", "label": "x"}])},
                     file=SAV, mime="application/octet-stream")
    assert response.status_code == 400


def test_invalid_codebook_rejected(client, auth_headers):
    spec = {"row": EXAM, "col": "CX_Pulse_Overall_NPS_group"}
    response = _post(client, auth_headers, "/v1/crosstab", {"spec": json.dumps(spec), "codebook": '{"variables": 3}'})
    assert response.status_code == 400


def test_metadata_and_frequency_use_edited_codebook(client, auth_headers):
    """A derived variable added by an edit is a real variable for every analysis endpoint."""
    edits = [{"op": "add_derived", "derived": {"name": "Product_count", "label": "Number of products",
              "rule": "count", "params": {"from": "Customer type", "bands": [
                  {"code": 1, "label": "Single", "min": 1, "max": 1},
                  {"code": 2, "label": "Multiple", "min": 2, "max": None}]}}}]
    codebook = _post(client, auth_headers, "/v1/codebook", {"edits": json.dumps(edits)}).json()["data"]["codebook"]
    meta = _post(client, auth_headers, "/v1/metadata", {"codebook": json.dumps(codebook)})
    assert meta.status_code == 200, meta.text
    assert "Product_count" in [v["name"] for v in meta.json()["data"]["variables"]]
    freq = _post(client, auth_headers, "/v1/frequency", {"variable": "Product_count", "codebook": json.dumps(codebook)})
    assert freq.status_code == 200, freq.text
    assert {f["label"] for f in freq.json()["data"]["frequencies"]} == {"Single", "Multiple"}


def test_review_page_has_editing():
    html = (Path(__file__).parent.parent / "public" / "review.html").read_text(encoding="utf-8")
    assert "add_derived" in html and "set_role" in html and "innerHTML" not in html
