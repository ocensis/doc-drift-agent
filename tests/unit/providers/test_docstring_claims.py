from __future__ import annotations

from pathlib import Path

import pytest

from drift_agent.detectors.structural import StructuralDetector
from drift_agent.domain.models import CodeFact
from drift_agent.providers.docstring_claims import GoogleDocstringClaimProvider
from drift_agent.providers.python_facts import PythonFactProvider


def _write_module(tmp_path: Path, source: str) -> Path:
    package = tmp_path / "src/demo"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    path = package / "api.py"
    path.write_text(source, encoding="utf-8")
    return path


def _facts(tmp_path: Path) -> list[CodeFact]:
    return PythonFactProvider().collect(
        repo_path=tmp_path,
        source_roots=["src"],
        changed_paths=["src/demo/api.py"],
    )


def test_google_args_and_returns_produce_precise_findings_and_ignore_raises(
    tmp_path: Path,
) -> None:
    path = _write_module(
        tmp_path,
        '''\
def convert(value: bytes) -> str:
    """Convert a payload.

    Args:
        value (str): Input payload.

    Returns:
        bytes: Converted text.

    Raises:
        ValueError: This section is outside Stage 2.
    """
    return value.decode()
''',
    )
    facts = _facts(tmp_path)
    provider = GoogleDocstringClaimProvider()

    claims = provider.collect(tmp_path, facts)
    findings = StructuralDetector().detect_docstrings(
        facts,
        claims,
        repository_id="repository-a",
    )

    assert provider.issues == []
    assert [(finding.kind, finding.component_id) for finding in findings] == [
        ("docstring_parameter_changed", "value"),
        ("docstring_return_changed", "return"),
    ]
    raw = path.read_bytes()
    by_component = {finding.component_id: finding for finding in findings}
    parameter = by_component["value"]
    returned = by_component["return"]
    assert (
        raw[parameter.code_evidence.start_byte : parameter.code_evidence.end_byte]
        == b"value: bytes"
    )
    assert raw[parameter.doc_evidence.start_byte : parameter.doc_evidence.end_byte] == b"str"
    assert raw[returned.doc_evidence.start_byte : returned.doc_evidence.end_byte] == b"bytes"
    assert all("raise" not in finding.kind for finding in findings)


def test_instance_receiver_is_excluded_but_staticmethod_parameter_is_retained(
    tmp_path: Path,
) -> None:
    _write_module(
        tmp_path,
        '''\
class Service:
    def render(self, value: bytes) -> str:
        """Render.

        Args:
            value (str): Value.

        Returns:
            str: Rendered value.
        """
        return value.decode()

    @staticmethod
    def parse(value: bytes) -> str:
        """Parse.

        Args:
            value (str): Value.

        Returns:
            str: Parsed value.
        """
        return value.decode()
''',
    )
    facts = _facts(tmp_path)
    provider = GoogleDocstringClaimProvider()

    claims = provider.collect(tmp_path, facts)
    findings = StructuralDetector().detect_docstrings(facts, claims)

    assert {finding.symbol_id for finding in findings} == {
        "demo.api.Service.parse",
        "demo.api.Service.render",
    }
    assert {finding.component_id for finding in findings} == {"value"}
    assert all(finding.component_id != "self" for finding in findings)


@pytest.mark.parametrize(
    "literal",
    [
        'r"""Args:\n    value (str): Value.\n"""',
        '"Args:\\n" "    value (str): Value.\\n"',
    ],
)
def test_non_plain_or_transformed_literals_are_rejected(
    tmp_path: Path,
    literal: str,
) -> None:
    _write_module(
        tmp_path,
        f"def convert(value: bytes) -> str:\n    {literal}\n    return value.decode()\n",
    )
    provider = GoogleDocstringClaimProvider()

    claims = provider.collect(tmp_path, _facts(tmp_path))

    assert claims == []
    assert [issue.reason_code for issue in provider.issues] == ["unsupported.literal"]


def test_duplicate_google_sections_are_rejected_without_partial_claims(
    tmp_path: Path,
) -> None:
    _write_module(
        tmp_path,
        '''\
def convert(value: bytes) -> str:
    """Convert.

    Args:
        value (str): First.

    Args:
        value (bytes): Second.
    """
    return value.decode()
''',
    )
    provider = GoogleDocstringClaimProvider()

    claims = provider.collect(tmp_path, _facts(tmp_path))

    assert claims == []
    assert [issue.reason_code for issue in provider.issues] == ["unsupported.docstring_style"]


def test_empty_docstring_surfaces_missing_fields_without_synthesizing_anchors(
    tmp_path: Path,
) -> None:
    _write_module(
        tmp_path,
        '''\
def convert(value: bytes) -> str:
    """"""
    return value.decode()
''',
    )
    facts = _facts(tmp_path)
    provider = GoogleDocstringClaimProvider()

    claims = provider.collect(tmp_path, facts)

    assert [(claim.kind, claim.component_id) for claim in claims] == [
        ("docstring_parameter", "value"),
        ("docstring_return", "return"),
    ]
    assert all(claim.extraction_error == "unsupported.literal" for claim in claims)
    assert all(claim.value_anchor is None for claim in claims)
