"""Generate a synthetic HubSpot CX survey export for golden tests.

Same 37-column layout and answer wording as a real HubSpot "CX Pulse" export,
but every answer is random (fixed seed): no client data. Edge cases the coding
rules care about are always included (see EDGE_ROLES / EDGE_CUSTOMER_TYPES).

    python tests/fixtures/exports/make_hubspot_cx_sample.py

writes hubspot_cx_sample.xlsx next to this file. Re-running gives the same file.
"""

from pathlib import Path

import numpy as np
import pandas as pd

N = 400
SEED = 20261014
OUT = Path(__file__).with_name("hubspot_cx_sample.xlsx")

ROLES = ["Teacher", "Head of Department", "School Leader", "Exams Officer",
         "School Group/School District Leader", "Other"]
ROLE_WEIGHTS = [0.55, 0.2, 0.1, 0.08, 0.02, 0.05]
EDGE_ROLES = ["Other", "Head of Department; Other", "Other; Teacher", "School Leader; Exams Officer",
              "Head of Department", "Head of Department; Teacher", "School Group/School District Leader"]

RESOURCES = "Teaching and learning resources such as textbooks, digital tools, or curriculum support"
PRODUCTS = [RESOURCES, "Programmes or curriculum", "Exams or qualifications"]
NOT_APPLICABLE = "Not applicable - my centre does not use Acme products /services"
EDGE_CUSTOMER_TYPES = [NOT_APPLICABLE, f"{RESOURCES}; Programmes or curriculum; Exams or qualifications; Other",
                       f"{RESOURCES}; Other", "Programmes or curriculum", "Exams or qualifications"]

DRIVERS = ["The accessibility of Acme's products and services",
           "The quality of Acme's products and services",
           "The ease of working with Acme", "The support Acme provide",
           "The communication I receive from Acme",
           "The outcomes my learners can achieve with Acme",
           "The value for money of Acme's products and services", "Other"]

AGREEMENT = ["Strongly Disagree", "Disagree", "Neither agree or disagree", "Agree", "Strongly Agree",
             "I do not know", "Not applicable - No results received"]
AGREEMENT_WEIGHTS = [0.04, 0.07, 0.11, 0.5, 0.24, 0.02, 0.02]


def _multi(rng, options, weights, max_picks):
    k = int(rng.integers(1, max_picks + 1))
    picks = rng.choice(options, size=k, replace=False, p=np.array(weights) / sum(weights))
    return "; ".join(picks)


def _sparse_text(rng, n, share, prefix):
    return [f"{prefix} {i}" if rng.random() < share else None for i in range(n)]


def build(n: int = N, seed: int = SEED) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    nps = rng.choice(range(11), size=n, p=[.01, .01, .01, .015, .01, .02, .025, .11, .22, .25, .32])

    roles = [_multi(rng, ROLES, ROLE_WEIGHTS, 3) for _ in range(n)]
    roles[:len(EDGE_ROLES)] = EDGE_ROLES
    customer = [_multi(rng, PRODUCTS, [.5, .25, .25], 3) for _ in range(n)]
    customer[:len(EDGE_CUSTOMER_TYPES)] = EDGE_CUSTOMER_TYPES

    def drivers(band):
        return [_multi(rng, DRIVERS, [1] * 7 + [.3], 3) if band(s) else None for s in nps]

    detractor = nps <= 6
    return pd.DataFrame({
        "Record ID": np.arange(9_000_001, 9_000_001 + n),
        "Email": None, "Contact first name": None, "Contact last name": None, "Response": None,
        "Industry Standard Question Type": None,
        "Sentiment": "Not Applicable",
        "Submission Name": None,
        "Date": pd.Timestamp("2026-09-01") + pd.to_timedelta(rng.integers(0, 30 * 24 * 60, n), unit="min"),
        "Source": "Shareable Link", "Survey ID": 82, "Survey Type": "Custom Survey",
        "Rating": None, "Visitor id": None, "Tags": None,
        "Centre Number": [f"ZZ{i:04d}" for i in rng.permutation(n)],
        "Centre Name": [f"Synthetic School {i}" for i in rng.permutation(n)],
        "Job role": roles,
        "Job role not listed": [f"Role text {i}" if "Other" in r else None for i, r in enumerate(roles)],
        "Customer type": customer,
        "Alternative products & services": _sparse_text(rng, n, 0.02, "Product text"),
        "CX Pulse Overall NPS": nps,
        "Detractors Drivers": drivers(lambda s: s <= 6),
        "Passive Drivers": drivers(lambda s: 7 <= s <= 8),
        "Promoter Drivers": drivers(lambda s: s >= 9),
        "Other drivers": _sparse_text(rng, n, 0.08, "Driver text"),
        "Detractor Contact Permission": [rng.choice(["Yes", "No"]) if d else None for d in detractor],
        "Detractor experience detail": [f"Experience text {i}" if d and rng.random() < .5 else None
                                        for i, d in enumerate(detractor)],
        "CX Principle - Ease": rng.choice(range(1, 6), n, p=[.02, .04, .15, .4, .39]),
        "Detail on ease of working with us": _sparse_text(rng, n, 0.35, "Ease text"),
        "CX Principle - Quality": rng.choice(range(1, 6), n, p=[.01, .03, .1, .39, .47]),
        "Detail on Quality of products, services, experiences": _sparse_text(rng, n, 0.25, "Quality text"),
        "CX Principle - Accessible": rng.choice(range(1, 6), n, p=[.01, .05, .2, .36, .38]),
        "Detail on accessibile & inclusive": _sparse_text(rng, n, 0.25, "Access text"),
        "Exam Results Alternative": rng.choice(AGREEMENT, n, p=AGREEMENT_WEIGHTS),
        "Exam Results Rating Explanation": _sparse_text(rng, n, 0.2, "Exam text"),
        "CX General Feedback": _sparse_text(rng, n, 0.5, "Feedback text"),
    })


if __name__ == "__main__":
    build().to_excel(OUT, index=False)
    print(f"wrote {OUT}")
