from dataclasses import dataclass

from pydantic import BaseModel, Field


class Issue(BaseModel):
    sonar_key: str
    file: str
    target_type: str
    target_name: str
    line: int
    original_start_line: int | None = None
    original_end_line: int | None = None
    problem: str
    severity: str
    solution: str
    original_code: str
    proposed_code: str
    required_imports: list[str] = Field(default_factory=list)


class Decision(BaseModel):
    issues: list[Issue]


@dataclass(frozen=True)
class ScopeInfo:
    kind: str
    name: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class BuildValidationResult:
    executed: bool
    success: bool
    reason: str = ""


class IssueBatchDecision(BaseModel):
    issues: list[Issue]


class AgentExecutionError(Exception):
    """Raised when the agent cannot complete a required execution step."""
