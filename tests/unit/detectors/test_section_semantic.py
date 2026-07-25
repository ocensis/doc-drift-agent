from __future__ import annotations

import pytest

from drift_agent.agent.budget import BudgetLedger
from drift_agent.detectors.section_semantic import (
    ResolvedSection,
    SectionEvidence,
    SectionSemanticDetector,
)
from drift_agent.domain.enums import FindingDisposition
from drift_agent.domain.models import RunBudgets
from drift_agent.model.budgeted import ModelClient
from drift_agent.model.client import ModelClientError
from drift_agent.model.contracts import (
    ModelCallUsage,
    StructuredModelRequest,
    StructuredModelResponse,
)
from drift_agent.providers.section_claims import SectionClaim

_SECTION_TEXT = "## 架构说明\n\n系统由三个节点组成。\n节点显式调用工具完成任务。\n"
_SECTION_START = 120
_FIRST_QUOTE = "系统由三个节点组成。"
_SECOND_QUOTE = "节点显式调用工具完成任务。"

_CONSISTENT = {"consistent": True, "stale_statements": []}


class _FakeTransport:
    """Scripted ModelTransport returning canned outputs or raising exceptions."""

    def __init__(self, results: list[dict[str, object] | Exception]) -> None:
        self.results = list(results)
        self.requests: list[StructuredModelRequest] = []

    def validate_request(self, request: StructuredModelRequest) -> None:
        return None

    def complete_structured(
        self,
        request: StructuredModelRequest,
        *,
        timeout_seconds: float,
    ) -> StructuredModelResponse:
        self.requests.append(request)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return StructuredModelResponse(
            provider="openrouter",
            profile=request.profile,
            requested_model="provider/model",
            actual_model="provider/model",
            request_id=f"gen-{len(self.requests)}",
            finish_reason="stop",
            output=result,
            usage=ModelCallUsage(
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
                cost_usd=0.0,
            ),
        )


def _client(transport: _FakeTransport, *, max_model_calls: int = 8) -> ModelClient:
    ledger = BudgetLedger(
        RunBudgets(
            max_model_calls_per_run=max_model_calls,
            max_input_tokens_per_run=1_000_000,
        )
    )
    return ModelClient(transport, ledger)


def _claim(
    *,
    path: str = "docs/architecture.md",
    text: str = _SECTION_TEXT,
    start_byte: int = _SECTION_START,
) -> SectionClaim:
    return SectionClaim(
        path=path,
        heading="架构说明",
        line=10,
        text=text,
        tokens=[],
        start_byte=start_byte,
        end_byte=start_byte + len(text.encode("utf-8")),
        source_hash="sha256:doc",
    )


def _section(*, path: str = "docs/architecture.md") -> ResolvedSection:
    return ResolvedSection(
        claim=_claim(path=path),
        evidence=[
            SectionEvidence(
                path="src/pkg/graph.py",
                excerpt="def build_graph() -> None: ...",
                source_hash="sha256:code",
            )
        ],
    )


def _stale(*quotes: str, code_quote: str = "def build_graph()") -> dict[str, object]:
    return {
        "consistent": False,
        "stale_statements": [
            {"quote": quote, "explanation": f"代码已不再满足:{quote}", "code_quote": code_quote}
            for quote in quotes
        ],
    }


def test_statement_without_verifiable_code_quote_is_dropped() -> None:
    transport = _FakeTransport([_stale(_FIRST_QUOTE, code_quote="not in any excerpt")])

    findings, calls_used = SectionSemanticDetector().detect(
        [_section()],
        _client(transport),
        repository_id="repo",
        max_calls=5,
    )

    assert findings == []
    assert calls_used == 1


def test_consistent_section_produces_no_findings() -> None:
    transport = _FakeTransport([_CONSISTENT])

    findings, calls_used = SectionSemanticDetector().detect(
        [_section()],
        _client(transport),
        repository_id="repository",
        max_calls=4,
    )

    assert findings == []
    assert calls_used == 1
    request = transport.requests[0]
    assert request.profile == "strong"
    assert _SECTION_TEXT[:100] in request.user_prompt
    assert "src/pkg/graph.py" in request.user_prompt
    assert request.response_schema.get("additionalProperties") is False


def test_stale_statements_anchor_quote_bytes_inside_chinese_section() -> None:
    transport = _FakeTransport([_stale(_SECOND_QUOTE, _FIRST_QUOTE)])
    section = _section()

    findings, calls_used = SectionSemanticDetector().detect(
        [section],
        _client(transport),
        repository_id="repository",
        max_calls=4,
    )

    assert calls_used == 1
    assert [finding.old_value["quote"] for finding in findings] == [
        _FIRST_QUOTE,
        _SECOND_QUOTE,
    ]
    section_bytes = _SECTION_TEXT.encode("utf-8")
    expectations = zip(findings, [_FIRST_QUOTE, _SECOND_QUOTE], [12, 13], strict=True)
    for finding, quote, line in expectations:
        assert finding.type == "semantic_drift"
        assert finding.kind == "semantic_section_drift"
        assert finding.reason_code == "semantic.section_mismatch"
        assert finding.disposition is FindingDisposition.DETECTED
        assert finding.truth_source == "code"
        assert finding.symbol_id == "section:docs/architecture.md#架构说明"
        assert finding.component_id == "架构说明"
        assert finding.detector_id == "semantic.section"
        assert finding.detector_version == "1"
        assert finding.new_value is None
        assert finding.reason == f"代码已不再满足:{quote}"
        assert finding.fingerprint
        assert finding.id == f"finding_{finding.fingerprint}"
        assert finding.doc_evidence.path == "docs/architecture.md"
        assert finding.doc_evidence.source_hash == "sha256:doc"
        assert finding.doc_evidence.line == line
        start = finding.doc_evidence.start_byte - _SECTION_START
        end = finding.doc_evidence.end_byte - _SECTION_START
        assert section_bytes[start:end].decode("utf-8") == quote
        assert finding.code_evidence.path == "src/pkg/graph.py"
        assert finding.code_evidence.line == 1
        assert finding.code_evidence.source_hash == "sha256:code"
        assert finding.code_evidence.start_byte == 0
        assert finding.code_evidence.end_byte == 0
    first, second = findings
    assert first.doc_evidence.start_byte == _SECTION_START + 17
    assert first.doc_evidence.end_byte == _SECTION_START + 47
    assert second.doc_evidence.start_byte == _SECTION_START + 48
    assert second.doc_evidence.end_byte == _SECTION_START + 87


def test_unlocatable_quote_falls_back_to_the_whole_section_range() -> None:
    transport = _FakeTransport([_stale("文档中不存在的句子。")])
    section = _section()

    findings, _ = SectionSemanticDetector().detect(
        [section],
        _client(transport),
        repository_id="repository",
        max_calls=4,
    )

    assert len(findings) == 1
    finding = findings[0]
    assert finding.doc_evidence.line == section.claim.line
    assert finding.doc_evidence.start_byte == section.claim.start_byte
    assert finding.doc_evidence.end_byte == section.claim.end_byte


def test_stale_statements_are_capped_at_three_per_section() -> None:
    quotes = [_FIRST_QUOTE, _SECOND_QUOTE, "三", "四", "五"]
    transport = _FakeTransport([_stale(*quotes)])

    findings, _ = SectionSemanticDetector().detect(
        [_section()],
        _client(transport),
        repository_id="repository",
        max_calls=4,
    )

    assert len(findings) == 3


def test_max_calls_stops_before_processing_remaining_sections() -> None:
    transport = _FakeTransport([_CONSISTENT])

    findings, calls_used = SectionSemanticDetector().detect(
        [_section(), _section(path="docs/other.md")],
        _client(transport),
        repository_id="repository",
        max_calls=1,
    )

    assert findings == []
    assert calls_used == 1
    assert len(transport.requests) == 1


def test_transport_error_skips_the_section_without_raising() -> None:
    transport = _FakeTransport([ModelClientError("model_unavailable"), _stale(_FIRST_QUOTE)])

    findings, calls_used = SectionSemanticDetector().detect(
        [_section(), _section(path="docs/other.md")],
        _client(transport),
        repository_id="repository",
        max_calls=4,
    )

    assert calls_used == 2
    assert [finding.doc_evidence.path for finding in findings] == ["docs/other.md"]


def test_schema_invalid_output_skips_the_section_without_raising() -> None:
    invalid: dict[str, object] = {
        "consistent": False,
        "stale_statements": [{"quote": _FIRST_QUOTE}],
    }
    transport = _FakeTransport([invalid, _stale(_SECOND_QUOTE)])

    findings, calls_used = SectionSemanticDetector().detect(
        [_section(), _section(path="docs/other.md")],
        _client(transport),
        repository_id="repository",
        max_calls=4,
    )

    assert calls_used == 2
    assert [finding.old_value["quote"] for finding in findings] == [_SECOND_QUOTE]


def test_budget_exhaustion_stops_the_loop_cleanly() -> None:
    transport = _FakeTransport([_CONSISTENT, _CONSISTENT])

    findings, calls_used = SectionSemanticDetector().detect(
        [_section(), _section(path="docs/other.md")],
        _client(transport, max_model_calls=1),
        repository_id="repository",
        max_calls=4,
    )

    assert findings == []
    assert calls_used == 1
    assert len(transport.requests) == 1


def test_consistent_flag_wins_over_contradictory_stale_statements() -> None:
    contradictory: dict[str, object] = {
        "consistent": True,
        "stale_statements": [
            {"quote": _FIRST_QUOTE, "explanation": "矛盾输出"},
        ],
    }
    transport = _FakeTransport([contradictory])

    findings, calls_used = SectionSemanticDetector().detect(
        [_section()],
        _client(transport),
        repository_id="repository",
        max_calls=4,
    )

    assert findings == []
    assert calls_used == 1


def test_findings_are_sorted_by_doc_path_then_start_byte() -> None:
    transport = _FakeTransport([_stale(_SECOND_QUOTE), _stale(_FIRST_QUOTE)])

    findings, _ = SectionSemanticDetector().detect(
        [_section(path="docs/b.md"), _section(path="docs/a.md")],
        _client(transport),
        repository_id="repository",
        max_calls=4,
    )

    assert [
        (finding.doc_evidence.path, finding.doc_evidence.start_byte) for finding in findings
    ] == [
        ("docs/a.md", _SECTION_START + 17),
        ("docs/b.md", _SECTION_START + 48),
    ]


def test_max_calls_zero_makes_no_model_calls() -> None:
    transport = _FakeTransport([])

    findings, calls_used = SectionSemanticDetector().detect(
        [_section()],
        _client(transport),
        repository_id="repository",
        max_calls=0,
    )

    assert findings == []
    assert calls_used == 0
    assert transport.requests == []


@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        ("架构说明", "架构说明"),
        ("Run Loop (v2)", "run-loop--v2"),
        ("!!!", "section"),
    ],
)
def test_symbol_id_uses_normalized_heading_slug(heading: str, expected: str) -> None:
    claim = _claim().model_copy(update={"heading": heading})
    section = ResolvedSection(claim=claim, evidence=_section().evidence)
    transport = _FakeTransport([_stale(_FIRST_QUOTE)])

    findings, _ = SectionSemanticDetector().detect(
        [section],
        _client(transport),
        repository_id="repository",
        max_calls=4,
    )

    assert findings[0].component_id == expected
    assert findings[0].symbol_id == f"section:docs/architecture.md#{expected}"
