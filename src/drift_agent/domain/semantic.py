from __future__ import annotations

import ast
import io
import tokenize
from typing import Any

from drift_agent.domain.models import SemanticScalar


def semantic_scalar_from_value(value: Any) -> SemanticScalar | None:
    """Return a type-tagged scalar for the deliberately tiny semantic grammar."""

    tagged: tuple[str, Any] | None = None
    if value is None:
        tagged = ("none", None)
    elif type(value) is bool:
        tagged = ("bool", value)
    elif type(value) is int:
        tagged = ("int", value)
    elif type(value) is str:
        tagged = ("str", value)
    if tagged is None:
        return None
    try:
        return SemanticScalar(type=tagged[0], value=tagged[1])  # type: ignore[arg-type]
    except ValueError:
        return None


def _has_supported_literal_token_shape(source: str) -> bool:
    if not source or source != source.strip():
        return False
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (IndentationError, tokenize.TokenError):
        return False
    significant = [
        item
        for item in tokens
        if item.type not in {tokenize.NEWLINE, tokenize.NL, tokenize.ENDMARKER}
    ]
    if len(significant) == 1:
        literal = significant[0]
        if literal.start != (1, 0) or literal.end != (1, len(source)):
            return False
        if literal.type == tokenize.NAME:
            return literal.string in {"None", "True", "False"}
        return literal.type in {tokenize.NUMBER, tokenize.STRING}
    if len(significant) != 2:
        return False
    sign, literal = significant
    return (
        sign.type == tokenize.OP
        and sign.string == "-"
        and sign.start == (1, 0)
        and sign.end == literal.start == (1, 1)
        and literal.type == tokenize.NUMBER
        and literal.end == (1, len(source))
    )


def parse_semantic_scalar(source: str) -> SemanticScalar | None:
    """Parse one literal without evaluating names, calls, or expressions."""

    if not _has_supported_literal_token_shape(source):
        return None
    try:
        expression = ast.parse(source, mode="eval").body
    except (SyntaxError, ValueError, OverflowError, UnicodeError):
        return None
    if isinstance(expression, ast.Constant):
        return semantic_scalar_from_value(expression.value)
    if (
        isinstance(expression, ast.UnaryOp)
        and isinstance(expression.op, ast.USub)
        and isinstance(expression.operand, ast.Constant)
        and type(expression.operand.value) is int
    ):
        return semantic_scalar_from_value(-expression.operand.value)
    return None


__all__ = ["parse_semantic_scalar", "semantic_scalar_from_value"]
