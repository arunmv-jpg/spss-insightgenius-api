# Implementation Plan: Any-Format Input + Codebook Review

> Companion to [PLAN_ANY_FORMAT.md](PLAN_ANY_FORMAT.md) (the *what* and *why*).
> This document is the *how*: PR-sized tasks, files, interfaces, tests, order.

## 0. Ground rules

- **Small PRs to `master`**, each green on `python -m pytest tests/ -x -q`, each deployable on
  its own (Railway auto-deploys `master`). Branch names: `feat/m<N>-<topic>`.
- **No behaviour change without a flag** until the milestone that switches it on.
  Flags live in `config.Settings` (`CODEBOOK_PIPELINE`, `REVIEW_UI`, `CLIENT_RULES_LEGACY`).
- **Existing contracts stay:** `load_data()` signature, response envelope, endpoint shapes.
  New fields are additive only.
- **Pure ASGI middleware only** (CLAUDE.md). Identity = `key.name`; never trust a client-sent user id.
- **Every inference rule ships with a fixture + snapshot test.** Every rule type ships with unit tests.
- **Regression guard for the whole project:** `tests/test_tabulation_real.py` (example3 golden) and
  a new `tests/test_cx_pulse_golden.py` (§M0.3) must stay green from M0 to M8.

Sizes: **S** ≈ 1–3 days · **M** ≈ 1–2 weeks · **L** ≈ 2–4 weeks (one engineer).

```
M0 ─► M1 ─► M2 ─► M3 ─► M4 ─► M5 ─► M7
                    │           └──► M6 (can run in parallel with M5/M7 once M2 is done)
                    └──────────────────────► M8 (after M4)
```

---

## M0: Land Phase 1 safely (S)

Goal: commit today's uncommitted work without baking client rules into the core.

| PR | Task | Files |
|---|---|---|
| M0.1 | Commit generic Phase-1 work: scale lexicon, missing codes, prefix codes, case merge, constant columns, NPS group, Mean-row fix, T2B ignores DK, calamine fallback, `read_table()` | `services/data_loader.py`, `services/tabulation_builder.py`, `requirements.txt`, `tests/test_data_loader.py` |
| M0.2 | Put client rules behind `CLIENT_RULES_LEGACY` (default **on** so prod output is unchanged): `_job_role_group`, `respondent_sheet.py`, its auto-analyze hook | `config.py`, `services/data_loader.py`, `services/respondent_sheet.py`, `routers/auto_analyze.py` |
| M0.3 | Golden test: anonymised, trimmed copy of the Sept Pulse export → snapshot of the codebook-relevant metadata + the tables workbook (sheet names, bases, key % cells) + data-workbook counts | `tests/fixtures/exports/hubspot_cx_pulse_sample.xls`, `tests/test_cx_pulse_golden.py` |
| M0.4 | Housekeeping: `outputs/` ignored (done), `.claude/launch.json` documented in README "Run locally" | `README.md` |

**Done when:** PRs merged, deploy healthy, golden test green.
**Note:** the fixture must be anonymised (drop Email/name columns, shuffle Record IDs).

---

## M1: Decipher-style tables (S)

| PR | Task | Files |
|---|---|---|
| M1.1 | `TabulateSpec.significance_levels: list[float] = [0.95]` (keep `significance_level` as an alias). Engine computes letters per level; the higher level wins; lower-level letters are lowercase | `services/tabulation_builder.py`, `services/quantipy_engine.py` (`crosstab_with_significance` ~L770–905), `shared/significance.py` (no change to tests; called once per level) |
| M1.2 | Response shape: `significance_letters` stays (uppercase, primary level); new `significance_letters_secondary` (additive) | `services/quantipy_engine.py`, `schemas/` if typed |
| M1.3 | Low-base markers: `low_base: {"warn": 100, "flag": 30}` → `*` / `**` after Base (N); legend on the Summary sheet; JSON field `low_base_flag` per column | `services/tabulation_builder.py` (`_write_crosstab_sheet`, `_build_excel` summary) |
| M1.4 | Auto-analyze default `[0.95, 0.90]`; MCP tool params | `routers/auto_analyze.py`, `mcp_server/tools/*.py` |

**Tests:** two-level letters on a constructed dataset (one pair significant at 90% but not 95%);
base markers at 29/30/99/100 boundaries; the example3 golden is unchanged with the default single level.
**Done when:** the golden workbook shows `A`/`a` letters and `**` on any column with a base under 30.

---

## M2: Split the pipeline + codebook model (M)

> **Status:** done on `feat/m2-codebook-pipeline`, except M2.6's Redis cache of the coded .sav
> (deferred: loading isn't a bottleneck yet; revisit with the review UI's repeated previews in M3).
> Old vs new loader verified identical (data, labels, value labels, missing ranges, measures) on the
> synthetic fixture and three real client exports.

Goal: `ingest → infer → apply` as separate modules; codebook visible via the API (read-only).
**No output change**: proven by the golden tests.

### M2.1 Codebook model
`services/codebook/model.py` (Pydantic v2):

```python
class Code(BaseModel):        code: float; label: str; match: list[str] = []
class Inferred(BaseModel):    rule: str; confidence: float; reason: str
class Variable(BaseModel):
    column: str; name: str; label: str | None = None
    role: Literal["id","pii","date","weight","single","multi","numeric","text","ignore"]
    measure: Literal["nominal","ordinal","scale"] | None = None
    storage: Literal["value","delimited","columns"] | None = None   # multi only
    delimiter: str | None = None
    codes: list[Code] = []; missing: list[Code] = []
    tags: list[str] = []; export: bool = True
    inferred: Inferred | None = None; status: Literal["ok","needs_review","new"] = "ok"
class Group(BaseModel):       name: str; role: Literal["multi","grid"]; members: list[str]; label: str | None = None
class Derived(BaseModel):     name: str; label: str; rule: str; params: dict
class Outputs(BaseModel):     banners: list[str] = []; data_workbook: dict | None = None; significance: dict | None = None
class Source(BaseModel):      adapter: str = "generic"; header_rows: int = 1; sheet: str | None = None; delimiter: str | None = None
class Codebook(BaseModel):
    id: str | None = None; name: str | None = None; version: int = 0
    source: Source; fingerprint: dict; variables: list[Variable]
    groups: list[Group] = []; derived: list[Derived] = []; outputs: Outputs = Outputs()
```

### M2.2 Ingest
`services/ingest/raw.py`: `RawTable(df: DataFrame, header_rows: list[list[str]], sheet: str | None, encoding, delimiter)`.
`services/ingest/readers.py`: move `_read_csv`, `_read_excel` (incl. calamine fallback), add `read_sav()` (returns RawTable + an SPSS-derived codebook).
`services/ingest/adapters/base.py`: `Adapter.detect(raw) -> float`, `Adapter.normalize(raw) -> RawTable`.
`services/ingest/adapters/generic.py`: today's behaviour. (Platform adapters come in M6.)

### M2.3 Infer
`services/codebook/infer.py: infer_codebook(raw: RawTable) -> Codebook`. Move from `data_loader`:
`_encode_column` decisions, `_code_frame`, `_scale_codes`, `_prefix_codes`, `_non_substantive_codes`,
`_split_multi_select` (detection part), `_looks_like_free_text`, `_rating_labels`, constant detection,
NPS suggestion (as an *unapplied* suggested `Derived`, `status="needs_review"`).
Each decision fills `Inferred(rule, confidence, reason)`.

### M2.4 Apply
`services/codebook/apply.py: apply_codebook(raw, cb) -> tuple[DataFrame, SavMeta, ApplyReport]`.
`SavMeta` = column labels, value labels, missing ranges, measures (exactly what `pyreadstat.write_sav` needs).
`ApplyReport` = `new_options`, `unmatched_values`, `missing_columns`, `extra_columns`.
Then `write_sav()` → bytes → `QuantiProEngine.load_spss()` (unchanged).

### M2.5 Wire up
`data_loader.load_data(bytes, filename, codebook: Codebook | None = None)` =
read → adapter → (`codebook` or `infer_codebook`) → `apply_codebook` → `load_spss`.
Behind `CODEBOOK_PIPELINE` (default on once goldens match).

### M2.6 Cache + endpoint
Redis keys `spss:codebook:{file_id}` (JSON) and `spss:coded:{file_id}` (sav bytes), same TTL/sliding
expiry as `spss:file:{file_id}` (`shared/file_resolver.py` L71–72). `resolve_file` gains `resolve_coded()`.
`GET /v1/files/{file_id}/codebook` → codebook + per-code counts (`routers/codebook.py`, scope `metadata`).
MCP: `spss_get_codebook`.

**Tests:** snapshot `tests/snapshots/codebook_<fixture>.json` for every fixture; all existing tests
unchanged; `load_data()` output byte-identical to pre-refactor for the fixtures (compare DataFrame + meta).
**Done when:** every endpoint runs through the new pipeline with no diff in the golden tests.

---

## M3: Review UI v1 (M)

| PR | Task | Files |
|---|---|---|
| M3.1 | Review payload endpoint: `GET /v1/files/{id}/review` = structure + codebook + per-variable distribution (counts per code, base, missing) + suggested banners. One call renders the page | `routers/codebook.py` |
| M3.2 | `public/review.html`: two panes. Left = codebook list (type chips, distribution bars, ⚠ needs-review / new filters, reason tooltips). Right = crosstab preview (row/banner pickers → `POST /v1/crosstab` with `file_id`), table with two-level letters + low-base markers, Chart.js toggle (bar / stacked / diverging for scales) | `public/review.html`, `main.py` route `/review` |
| M3.3 | Flow: upload → review → "Approve & continue" (M3: approve = accept the inferred codebook) → existing auto-analyze/tabulate screens with `file_id` | `public/upload.html`, `public/wizard.html`, `public/express.html`, `public/app.html` |
| M3.4 | Structure bar: shows adapter/header rows/sheet/delimiter (read-only in M3; editable in M6) | `public/review.html` |

Front end: vanilla JS + Chart.js 4.4.7 (already used in `chat.html`), same auth handling as the
other pages. No framework introduced.
**Tests:** API tests for `/review`; Playwright smoke (`tests/e2e/test_review.py`, run on demand, not on every commit):
upload the fixture → 37 variables listed → choose row/banner → table renders.
**Done when:** any fixture can be uploaded, inspected, previewed and continued without Excel.

---

## M4: Editing + derived rules; remove client code (L)

### M4.1 Patch API
`PATCH /v1/files/{id}/codebook` with a list of operations:
`set_role`, `set_measure`, `rename`, `set_label`, `reorder_codes`, `merge_codes`, `set_missing`,
`exclude`, `set_pii`, `group_columns`, `ungroup`, `split_multi`, `add_derived`, `update_derived`, `remove_derived`.
Each op is validated and applied to the cached codebook → re-apply → returns the updated codebook + affected
variables. Codes of existing answers never change (merge keeps the lowest code).
`services/codebook/patch.py` + `tests/test_codebook_patch.py` (one test per op).

### M4.2 Rule engine
`services/codebook/derive.py: build_derived(df, cb) -> df` with one function per rule:

| Rule | Params (sketch) |
|---|---|
| `recode` | `{from, map: {old_code: new_code}, labels}` |
| `bands` | `{from, bands: [{code,label,min,max}]}` |
| `priority` | `{from?, groups: [{code,label,when: Cond}], else: {code,label} \| null}` |
| `count` | `{from: multi, exclude?: [labels], bands?: [...]}` |
| `combination` | `{from: multi, combos: [{code,label,exact\|subset: [labels]}], ignore?: [labels]}` |
| `coalesce` | `{from: [vars], label}` |
| `flags` | `{from, values?: [...]}` → one 0/1 var per option/value |

`Cond` (`services/codebook/conditions.py`): `any/all/none` over option labels, `and/or/not`,
`{"var","eq|in|between"}`. Evaluated with pandas masks; **no `eval`**. Dependency order by topological
sort; cycles rejected.
`POST /v1/files/{id}/derived/preview` → counts per category on the full data (or a sample above 100k rows).

### M4.3 UI editing
Inline edits in `review.html` (each edit = one PATCH op; preview refreshes): drag-reorder codes,
merge, mark missing, role/measure dropdowns, group columns. "Add derived variable" drawer with one form
per rule type + a live count table.

### M4.4 Migrate the client rules to data
`tests/fixtures/codebooks/cx_pulse.json` expresses: role `priority`, products `count` + `combination`,
NPS `bands`, DRIVERS `coalesce` + `flags`, score `flags`, banners, data-workbook tabs, the HubSpot
system columns as `ignore`. Then **delete** `_job_role_group`, `JOB_ROLE_*`, `HUBSPOT_SYSTEM_COLUMNS`,
`_DRIVER_ORDER`, `CATEGORY_SHEETS`, `_PRODUCTS`, and the `CLIENT_RULES_LEGACY` flag.

**Tests:**
- `test_cx_pulse_golden.py` now loads the codebook fixture → identical output to M0.3.
- `tests/test_no_client_rules.py` greps `services/` and `routers/` for `<client name>|Exams Officer|Customer type|Pulse|HubSpot-specific option text` → must find nothing.
- Rule-engine unit tests and property tests (priority groups are mutually exclusive; every answered respondent is in exactly one group).

**Done when:** the Sept Pulse workbook is reproduced from `cx_pulse.json` alone, and there's no client code left.

---

## M5: Save + reuse across waves (M)

| PR | Task | Files |
|---|---|---|
| M5.1 | Migration `migrations/002_codebooks.sql`: `codebooks(id uuid pk, owner text not null, name text, created_at)`, `codebook_versions(codebook_id fk, version int, body jsonb, fingerprint text[], created_at, created_by, primary key(codebook_id, version))`; RLS by owner | `migrations/`, `services/codebook/store.py` |
| M5.2 | Endpoints: `POST /v1/codebooks` (save current file codebook as new codebook or new version), `GET /v1/codebooks`, `GET /v1/codebooks/{id}`, `GET …/versions/{v}` (versions are immutable) | `routers/codebook.py` |
| M5.3 | Apply saved: `POST /v1/files/{id}/codebook/apply {codebook_id, version?}` → `ApplyReport`. New options get the next free code and `status="new"`; unmatched columns are reported | `services/codebook/apply.py` |
| M5.4 | Fingerprint matching on upload: column-name overlap ≥ 0.8 (configurable) → `matched_codebook` in the upload response; per decision #2, apply automatically or suggest | `routers/file_upload.py`, `services/codebook/store.py` |
| M5.5 | `codebook_id` accepted by crosstab/frequency/tabulate/auto-analyze/convert/wave-compare; wave-compare applies **one** codebook to both files; workbook Summary sheet records codebook name + version | routers + `services/tabulation_builder.py` |
| M5.6 | UI: "Matched *CX Pulse v4*: nothing changed → Continue" fast path; "2 new options" banner otherwise; save/version dialog | `public/review.html` |

**Tests:** wave-stability suite: wave B with an added option, a removed option, a renamed column,
reordered columns → codes unchanged and the report is correct; store tests against a Supabase test schema
(or a mocked client, matching the library tests).
**Done when:** the Oct Pulse export applied with the Sept codebook needs zero edits and wave-compare is consistent.

---

## M6: Platform adapters (M–L, parallel after M2)

One PR per adapter: `services/ingest/adapters/<platform>.py` + fixture + snapshot.

| Adapter | detect() signals | normalize() |
|---|---|---|
| `hubspot` | `Record ID` + (`Survey ID` \| `Submission Name`) | `;` delimiter; system columns → `ignore` suggestions |
| `surveymonkey` | Row 2 contains "Response" / "Open-Ended Response" under merged row-1 headers | Merge 2 header rows → `Question - Option`; one-column-per-option sets → `Group(multi)` |
| `qualtrics` | Row 3 cells parse as `{"ImportId": …}` | Drop rows 2–3, keep QIDs as names and row-2 text as labels; detect choice-text vs numeric export |
| `typeform` / `googleforms` | `#`/`Submit Date`/`Network ID` · `Timestamp` first column | `,` multi-selects (quoted-comma aware); per-option column sets |
| `alchemer` | `Response ID`, `Time Started` | `:`-joined option columns → groups |
| `decipher` | `record`, `uuid`, `status`, `qtime` | `q1r1c1` names → grids/multis by name pattern |

Plus `POST /v1/files/{id}/structure` (override header rows/sheet/delimiter/adapter → re-ingest →
re-infer) and the editable structure bar in `review.html`.
**Blocked on:** a real, anonymised sample export per platform (decision #3).
**Done when:** each golden sample gives a correct codebook with no manual edits.

---

## M7: Generic data workbook + exports (S–M)

| PR | Task | Files |
|---|---|---|
| M7.1 | `services/exports/data_workbook.py`: generalise `respondent_sheet.write_sheet` (COUNT column, % row, count row, live formulas, flag fill, freeze panes) driven by `outputs.data_workbook` (`segment_tabs`, `title`, `include_pii=false`) | new module; delete `respondent_sheet.py` |
| M7.2 | `POST /v1/exports/data-workbook`; auto-analyze appends it when `outputs.data_workbook` is set (replaces the M0 hook) | `routers/`, `routers/auto_analyze.py` |
| M7.3 | `.sav` export with the full codebook (labels, missing ranges, measures, derived vars); CSV/XLSX coded export | `routers/convert.py` |
| M7.4 | UI: choose segment tabs in review → download | `public/review.html` |

**Done when:** the client's FULL DATA + 8 tabs come purely from `cx_pulse.json`; any survey can get the same layout.

---

## M8: AI rule drafting (S, optional)

`POST /v1/codebooks/{id}/describe-rule {text}` → Claude (Sonnet; see the `claude-api` skill for the
current model ID) with the codebook's variables/options and the rule JSON schema
(structured output) → draft `Derived` + preview counts. **Never applied without approval.**
UI: "Describe it" box in the derived-variable drawer.
**Tests:** recorded responses (no live calls in CI); the analyst's job-role note → expected `priority` rule.

---

## Cross-cutting

- **Performance:** coded .sav cached per file (M2.6) removes re-parsing on every request. Derived
  preview samples above 100k rows. Target: review payload < 1.5 s for 2k × 100.
- **Security:** PATCH ops validated against the model; condition language has no eval; codebook
  owner = `key.name`; PII role excluded from exports by default.
- **Observability:** log `adapter`, `inference confidence histogram`, `needs_review count`,
  `apply report` per upload (no cell values in logs).
- **Docs:** README "Upload any format" section; `openapi.json` regenerated; MCP tool docs in
  `public/mcp-docs.html`; CLAUDE.md endpoint table updated.
- **Known separate bug:** MRX crosstab path always falls back to pandas (spawned task). Fix it before M1
  if possible, since M1 touches the same code.

## Definition of done (whole plan)

1. Upload SPSS / CSV / Excel / HubSpot / SurveyMonkey / Qualtrics / Typeform / Google Forms / Alchemer /
   Decipher sample → review screen shows a correct codebook; zero manual edits on golden samples.
2. Sept Pulse tables + data workbook reproduced from `cx_pulse.json`; no client strings in code.
3. Oct Pulse runs through the "nothing changed → continue" path with codes identical to Sept.
4. All tables show 95/90 letters and low-base markers.
5. Test suite green: existing tests + snapshots + wave-stability + rule engine + no-client-rules guard.

## Open inputs (blocking where noted)

| Input | Blocks |
|---|---|
| Analyst answers: col BZ, "Programmes AND/OR exams", Triple + Other | M4.4 (`cx_pulse.json` correctness) |
| Codebook edit permissions; org vs per-key sharing | M5.1 schema / RLS |
| Auto-apply vs ask on fingerprint match | M5.4 |
| Sample exports per platform + priority order | M6 |
| Review step mandatory for API users? | M3.3 / M5.6 |
