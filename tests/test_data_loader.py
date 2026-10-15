"""Excel / CSV input support — services.data_loader + REST endpoints."""

import io
import json

import numpy as np
import pandas as pd
import pytest

from services.data_loader import load_data, _spss_name

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _xlsx(sheets: dict[str, pd.DataFrame]) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for name, df in sheets.items():
            df.to_excel(writer, sheet_name=name, index=False)
    return buf.getvalue()


@pytest.fixture(scope="module")
def survey_df():
    """Labelled survey data, as typically exported to Excel (text categories)."""
    rng = np.random.default_rng(7)
    n = 120
    return pd.DataFrame({
        "Q1. Gender": rng.choice(["Male", "Female"], size=n),
        "Age group": rng.choice(["18-34", "35-54", "55+"], size=n),
        "Satisfaction": rng.choice(
            ["1 - Very poor", "2 - Poor", "3 - OK", "4 - Good", "10 - Excellent"], size=n),
        "Spend": rng.uniform(5, 100, size=n).round(2),
    })


@pytest.fixture(scope="module")
def survey_xlsx(survey_df):
    return _xlsx({"Data": survey_df})


# ── Unit: loader ─────────────────────────────────────────────────────────────

def test_excel_text_columns_become_labelled_codes(survey_xlsx):
    data = load_data(survey_xlsx, "survey.xlsx")

    assert data.meta is not None
    assert data.mrx_dataset is not None
    assert list(data.df.columns) == ["Q1_Gender", "Age_group", "Satisfaction", "Spend"]

    # Original header preserved as variable label
    assert data.meta.column_names_to_labels["Q1_Gender"] == "Q1. Gender"

    vl = data.meta.variable_value_labels
    assert set(vl["Q1_Gender"].values()) == {"Male", "Female"}
    # Natural sort: "10 - Excellent" is the highest code, not between 1 and 2
    assert max(vl["Satisfaction"].items())[1] == "10 - Excellent"
    assert "Spend" not in vl
    assert pd.api.types.is_numeric_dtype(data.df["Q1_Gender"])


def test_excel_label_sheets_are_applied():
    df = pd.DataFrame({"region": [1, 2, 1, 2], "score": [3, 4, 5, 4]})
    xlsx = _xlsx({
        "Data": df,
        "Variable Labels": pd.DataFrame({"Variable": ["region"], "Label": ["Sales region"]}),
        "Value Labels": pd.DataFrame({
            "Variable": ["region", "region"], "Value": [1, 2], "Label": ["North", "South"]}),
    })
    data = load_data(xlsx, "coded.xlsx")

    assert data.meta.column_names_to_labels["region"] == "Sales region"
    assert data.meta.variable_value_labels["region"] == {1.0: "North", 2.0: "South"}
    assert data.meta.column_names_to_labels["score"] == "score"


def test_excel_uses_first_non_label_sheet_without_data_sheet(survey_df):
    xlsx = _xlsx({"Respondents": survey_df.head(10), "Notes": pd.DataFrame({"x": ["n/a"]})})
    data = load_data(xlsx, "book.xlsx")
    assert len(data.df) == 10


def test_csv_numbers_stored_as_text_stay_numeric():
    csv_bytes = "id;answer\n1001;Yes\n1002;No\n1003;Yes\n".encode("cp1252")
    data = load_data(csv_bytes, "semi.csv")
    assert "id" not in data.meta.variable_value_labels
    assert set(data.meta.variable_value_labels["answer"].values()) == {"Yes", "No"}


def test_multi_select_column_expands_to_flags():
    """HubSpot-style 'A;B' answers → one 0/1 variable per option, NaN when unanswered."""
    df = pd.DataFrame({"Role": ["Teacher", "Teacher;Exams Officer", None, "Exams Officer", "Teacher"],
                       "ID": [101, 102, 103, 104, 105]})
    data = load_data(_xlsx({"Data": df}), "roles.xlsx")

    assert list(data.df.columns) == ["Role_Exams_Officer", "Role_Teacher", "ID"]
    assert data.meta.column_names_to_labels["Role_Teacher"] == "Role: Teacher"
    teacher = data.df["Role_Teacher"].tolist()
    assert teacher[:2] == [1.0, 1.0] and pd.isna(teacher[2]) and teacher[3:] == [0.0, 1.0]
    assert data.df["Role_Exams_Officer"].sum() == 2


def test_free_text_is_not_categorised():
    df = pd.DataFrame({"Comment": [f"answer number {i}" for i in range(20)]})
    data = load_data(_xlsx({"Data": df}), "comments.xlsx")
    assert "Comment" not in data.meta.variable_value_labels
    assert pd.api.types.is_string_dtype(data.df["Comment"])


def test_integer_rating_scale_gets_labels():
    df = pd.DataFrame({"NPS": [0, 7, 9, 10, 10, 3], "Spend": [1.5, 2.25, 9.0, 3.0, 4.0, 5.5]})
    data = load_data(_xlsx({"Data": df}), "nps.xlsx")
    assert data.meta.variable_value_labels["NPS"] == {0.0: "0", 3.0: "3", 7.0: "7", 9.0: "9", 10.0: "10"}
    assert "Spend" not in data.meta.variable_value_labels


def test_agreement_scale_coded_in_scale_order_with_dont_know_missing():
    """HubSpot exports Likert answers as text — code them low→high, not alphabetically."""
    answers = ["Agree", "Strongly Agree", "Disagree", "Neither agree or disagree",
               "Strongly Disagree", "I do not know", "Not applicable - No results received", "agree"]
    data = load_data(_xlsx({"Data": pd.DataFrame({"Exam results": answers})}), "likert.xlsx")

    assert data.meta.variable_value_labels["Exam_results"] == {
        1.0: "Strongly Disagree", 2.0: "Disagree", 3.0: "Neither agree or disagree",
        4.0: "Agree", 5.0: "Strongly Agree",
    }
    # "agree" merged into "Agree"; don't know / N/A are user-missing → excluded from base
    assert data.df["Exam_results"].tolist()[:5] == [4.0, 5.0, 2.0, 3.0, 1.0]
    assert data.df["Exam_results"].iloc[5:7].isna().all() and data.df["Exam_results"].iloc[7] == 4.0


def test_scale_keeps_absent_levels_so_codes_are_stable():
    df = pd.DataFrame({"Sat": ["Satisfied", "Very satisfied", "Satisfied", "Dissatisfied"]})
    vl = load_data(_xlsx({"Data": df}), "sat.xlsx").meta.variable_value_labels["Sat"]
    assert vl[1.0] == "Very dissatisfied" and vl[5.0] == "Very satisfied"
    assert vl[2.0] == "Dissatisfied" and vl[4.0] == "Satisfied"


def test_numeric_prefix_is_used_as_code():
    df = pd.DataFrame({"Likely": ["0 - Not at all likely", "10 - Extremely likely", "5", "7", "Don't know"]})
    data = load_data(_xlsx({"Data": df}), "prefix.xlsx")
    assert data.meta.variable_value_labels["Likely"] == {
        0.0: "0 - Not at all likely", 5.0: "5", 7.0: "7", 10.0: "10 - Extremely likely"}
    assert data.df["Likely"].tolist()[:4] == [0.0, 10.0, 5.0, 7.0] and pd.isna(data.df["Likely"].iloc[4])


def test_nominal_dont_know_stays_visible_and_other_sorts_last():
    df = pd.DataFrame({"Brand": ["Zeta", "Other", "Alpha", "Don't know", "Zeta"]})
    data = load_data(_xlsx({"Data": df}), "brand.xlsx")
    assert data.meta.variable_value_labels["Brand"] == {1.0: "Alpha", 2.0: "Zeta", 3.0: "Other", 98.0: "Don't know"}
    assert data.df["Brand"].notna().all()


def test_constant_export_column_is_not_categorised():
    df = pd.DataFrame({"Source": ["Shareable Link"] * 4, "Q": ["Yes", "No", "Yes", "Yes"]})
    data = load_data(_xlsx({"Data": df}), "const.xlsx")
    assert "Source" not in data.meta.variable_value_labels
    assert data.meta.variable_value_labels["Q"] == {1.0: "Yes", 2.0: "No"}


def test_nps_question_gets_group_variable():
    df = pd.DataFrame({"Overall NPS": [0, 6, 7, 8, 9, 10], "Rating": [1, 2, 3, 4, 5, 5]})
    data = load_data(_xlsx({"Data": df}), "nps.xlsx")
    assert data.df["Overall_NPS_group"].tolist() == [1.0, 1.0, 2.0, 2.0, 3.0, 3.0]
    assert data.meta.column_names_to_labels["Overall_NPS_group"] == "Overall NPS: NPS group"
    assert "Rating_group" not in data.df.columns


def test_grid_t2b_ignores_dont_know_codes():
    """A natively labelled DK=99 must not be picked as a top-2-box endpoint."""
    from services.data_loader import is_non_substantive
    vl = {1.0: "Poor", 2.0: "Fair", 3.0: "Good", 4.0: "Excellent", 99.0: "Don't know"}
    assert sorted(k for k, v in vl.items() if not is_non_substantive(v))[-2:] == [3.0, 4.0]


def test_unsupported_format_raises():
    with pytest.raises(ValueError, match="Unsupported"):
        load_data(b"whatever", "data.json")


def test_spss_name_sanitization():
    used: set[str] = set()
    assert _spss_name("Q1. Gender", used) == "Q1_Gender"
    assert _spss_name("1st choice", used) == "V_1st_choice"
    assert _spss_name("BY", used) == "BY_"
    assert _spss_name("Q1 Gender", used) == "Q1_Gender_2"
    assert len(_spss_name("x" * 100, used).encode()) <= 64


# ── Integration: REST endpoints accept Excel ─────────────────────────────────

def test_metadata_endpoint_accepts_xlsx(client, auth_headers, survey_xlsx):
    response = client.post(
        "/v1/metadata", headers=auth_headers,
        files={"file": ("survey.xlsx", survey_xlsx, XLSX_MIME)},
    )
    assert response.status_code == 200, response.text
    variables = {v["name"]: v for v in response.json()["data"]["variables"]}
    assert variables["Q1_Gender"]["label"] == "Q1. Gender"
    assert set(variables["Q1_Gender"]["value_labels"].values()) == {"Male", "Female"}


def test_frequency_endpoint_accepts_xlsx(client, auth_headers, survey_xlsx):
    response = client.post(
        "/v1/frequency", headers=auth_headers,
        files={"file": ("survey.xlsx", survey_xlsx, XLSX_MIME)},
        data={"variable": "Age_group"},
    )
    assert response.status_code == 200, response.text
    labels = {f["label"] for f in response.json()["data"]["frequencies"]}
    assert labels == {"18-34", "35-54", "55+"}


def test_crosstab_endpoint_accepts_xlsx(client, auth_headers, survey_xlsx):
    response = client.post(
        "/v1/crosstab", headers=auth_headers,
        files={"file": ("survey.xlsx", survey_xlsx, XLSX_MIME)},
        data={"spec": json.dumps({"row": "Satisfaction", "col": "Q1_Gender"})},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["total_responses"] == 120


def test_auto_analyze_accepts_xlsx(client, auth_headers, survey_xlsx):
    response = client.post(
        "/v1/auto-analyze", headers=auth_headers,
        files={"file": ("survey.xlsx", survey_xlsx, XLSX_MIME)},
    )
    assert response.status_code == 200, response.text
    assert response.content[:4] == b"PK\x03\x04"


def test_unsupported_extension_rejected(client, auth_headers):
    response = client.post(
        "/v1/frequency", headers=auth_headers,
        files={"file": ("data.json", b"{}", "application/json")},
        data={"variable": "x"},
    )
    assert response.status_code == 400


def test_mean_row_aligns_with_total_column():
    """Regression: Mean row used banner indices that skipped the auto-inserted Total column."""
    import openpyxl
    from services.quantipy_engine import QuantiProEngine
    from services.tabulation_builder import TabulateSpec, build_tabulation

    df = pd.DataFrame({"Score": [1, 2, 3, 4, 5, 5, 4, 3] * 5, "Group": ["A", "A", "B", "B"] * 10})
    data = load_data(_xlsx({"Data": df}), "means.xlsx")
    result = build_tabulation(QuantiProEngine, data, TabulateSpec(banners=["Group"], stubs=["Score"], include_means=True))

    ws = openpyxl.load_workbook(io.BytesIO(result.excel_bytes))["Score"]
    mean_row = next(r for r in ws.iter_rows(values_only=True) if r[0] == "Mean")
    group_means = df.groupby("Group")["Score"].mean()
    assert float(mean_row[1].split()[0]) == round(df["Score"].mean(), 2)
    assert float(mean_row[2].split()[0]) == round(group_means["A"], 2)
    assert float(mean_row[3].split()[0]) == round(group_means["B"], 2)


def test_job_role_group_assigns_one_role_by_priority():
    """HubSpot CX job role (multi-select) → single role group, from the HubSpot CX setup."""
    from services.codebook.infer import infer_codebook
    from services.data_loader import read_raw
    from services.setups import apply_setup, get_setup

    roles = ["Teacher; Exams Officer", "School Leader; Teacher", "Head of Department",
             "Head of Department; Teacher", "Teacher", "Other", "Other; Head of Department", None]
    file_bytes = _xlsx({"Data": pd.DataFrame({"ID": range(8), "Job role": roles})})
    raw = read_raw(file_bytes, "roles.xlsx")
    codebook, applied, skipped = apply_setup(infer_codebook(raw), get_setup("hubspot_cx_pulse"), raw.df)
    assert applied == ["Job role group"]  # this file has no Customer type / drivers: those are skipped
    data = load_data(file_bytes, "roles.xlsx", codebook)

    assert data.meta.variable_value_labels["Job_role_group"] == {
        1.0: "Exams Officer", 2.0: "Leader", 3.0: "Teacher", 4.0: "Other"}
    assert data.meta.column_names_to_labels["Job_role_group"] == "Job role group"
    groups = data.df["Job_role_group"].tolist()
    assert groups[:7] == [1.0, 2.0, 2.0, 3.0, 3.0, 4.0, 2.0] and pd.isna(groups[7])
    # The 0/1 option flags are still there for "% who teach at all"
    assert data.df["Job_role_Teacher"].sum() == 4


def test_role_group_only_for_job_role_with_exams_officer():
    df = pd.DataFrame({"Hobbies": ["Teacher; Other", "Other", "Teacher"]})
    data = load_data(_xlsx({"Data": df}), "hobbies.xlsx")
    assert "Hobbies_group" not in data.df.columns


def test_sparse_all_unique_text_is_not_categorised():
    """A "please specify" column with a few answers, all different, is free text — not a banner."""
    df = pd.DataFrame({"ID": range(40),
                       "Specify": [f"answer {i}" if i % 6 == 0 else None for i in range(40)]})
    data = load_data(_xlsx({"Data": df}), "specify.xlsx")
    assert "Specify" not in data.meta.variable_value_labels
