"""The codebook: every decision about how a raw survey file becomes coded data.

Inferred automatically (services.codebook.infer), applied deterministically
(services.codebook.apply), and returned by the API so users can see — and later
edit — how each column was understood. See PLAN_ANY_FORMAT.md §4.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

Role = Literal["id", "pii", "date", "weight", "single", "multi", "numeric", "text", "ignore"]
Measure = Literal["nominal", "ordinal", "scale"]


class Inferred(BaseModel):
    """Why the inference made a decision (shown in the review UI)."""
    rule: str
    reason: str
    confidence: float = 1.0


class Code(BaseModel):
    code: float
    label: str
    match: list[str] = Field(default_factory=list, description="Raw answers coded to this value")
    count: int | None = Field(None, description="Respondents with this code (filled for review)")


class Option(BaseModel):
    """One option of a multi-select question, stored as a 0/1 variable."""
    label: str
    name: str
    count: int | None = None


class Variable(BaseModel):
    column: str = Field(description="Header in the uploaded file")
    name: str | None = Field(None, description="SPSS variable name (None when not output)")
    label: str
    role: Role
    measure: Measure | None = None
    codes: list[Code] = Field(default_factory=list, description="Valid codes (value labels)")
    missing: list[Code] = Field(default_factory=list, description="User-missing codes (excluded from bases)")
    value_labels: dict[str, str] | None = Field(
        None, description="Explicit value labels overriding `codes` (e.g. from a Value Labels sheet)")
    delimiter: str | None = None              # multi
    options: list[Option] = Field(default_factory=list)  # multi
    source: Literal["number", "text", "bool", "datetime"] | None = None
    tags: list[str] = Field(default_factory=list)
    inferred: Inferred | None = None
    status: Literal["ok", "needs_review", "new"] = "ok"
    base: int | None = Field(None, description="Respondents who answered (filled for review)")


class Derived(BaseModel):
    """A variable computed from others by a rule (PLAN_ANY_FORMAT.md §5)."""
    name: str
    label: str
    rule: Literal["bands", "legacy_job_role_group"]
    source: str = Field(description="Name of the variable it's computed from (column for multi-select)")
    after: str = Field(description="Output position: right after this variable")
    params: dict[str, Any] = Field(default_factory=dict)
    codes: list[Code] = Field(default_factory=list)
    measure: Measure = "nominal"
    inferred: Inferred | None = None


class Source(BaseModel):
    format: str
    adapter: str = "generic"
    rows: int = 0
    columns: int = 0


class Codebook(BaseModel):
    version: int = 0
    source: Source
    variables: list[Variable]
    derived: list[Derived] = Field(default_factory=list)

    def variable(self, name: str) -> Variable | None:
        return next((v for v in self.variables if v.name == name), None)

    def output_names(self) -> list[str]:
        """SPSS variables produced, in file order."""
        names: list[str] = []
        for var in self.variables:
            if var.role == "multi":
                names += [o.name for o in var.options]
            elif var.name and var.role != "ignore":
                names.append(var.name)
            names += [d.name for d in self.derived if d.after == (var.options[-1].name if var.options else var.name)]
        return names
