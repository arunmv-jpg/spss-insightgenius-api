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
    csv_bytes = "id;answer\n1;Yes\n2;No\n3;Yes\n".encode("cp1252")
    data = load_data(csv_bytes, "semi.csv")
    assert "id" not in data.meta.variable_value_labels
    assert set(data.meta.variable_value_labels["answer"].values()) == {"Yes", "No"}


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
