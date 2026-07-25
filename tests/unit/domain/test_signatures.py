from drift_agent.domain.models import ParameterFact
from drift_agent.domain.signatures import render_signature


def test_render_signature_preserves_kinds_defaults_and_return() -> None:
    parameters = [
        ParameterFact(name="message", kind="positional or keyword", annotation="str"),
        ParameterFact(
            name="color",
            kind="positional or keyword",
            annotation="bool",
            default="True",
            required=False,
        ),
    ]

    assert render_signature("echo", parameters, "None") == (
        "echo(message: str, color: bool = True) -> None"
    )


def test_render_signature_inserts_one_keyword_only_separator() -> None:
    parameters = [
        ParameterFact(name="color", kind="keyword-only", annotation="bool"),
        ParameterFact(name="style", kind="keyword-only", annotation="str"),
    ]

    assert render_signature("echo", parameters, None) == ("echo(*, color: bool, style: str)")
