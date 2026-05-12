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
    metrics: "AnalysisMetrics" = Field(default_factory=lambda: AnalysisMetrics())


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


@dataclass(frozen=True)
class ImprovementCandidate:
    file: str
    line: int
    language: str
    category: str
    reason: str
    evidence: str
    original_code: str
    confidence: float


@dataclass(frozen=True)
class AnalysisMetrics:
    latency_seconds: float = 0.0
    prompt_tokens: int = 0
    response_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    batch_cache_hits: int = 0
    batch_cache_misses: int = 0
    improvement_candidates: int = 0


@dataclass(frozen=True)
class ExecutionMetrics:
    sonar_findings: int = 0
    generated_issues: int = 0
    invalid_issues: int = 0
    patch_invalid_issues: int = 0
    final_issues: int = 0
    blocking_findings: bool = False


@dataclass(frozen=True)
class CommentSyncResult:
    desired: int = 0
    created: int = 0
    reused: int = 0
    deleted: int = 0


class IssueBatchDecision(BaseModel):
    issues: list[Issue]


class AgentExecutionError(Exception):
    """Raised when the agent cannot complete a required execution step."""
