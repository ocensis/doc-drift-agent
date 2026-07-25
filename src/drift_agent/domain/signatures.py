from collections.abc import Sequence

from drift_agent.domain.models import ParameterFact


def _render_parameter(parameter: ParameterFact) -> str:
    prefix = {
        "variadic positional": "*",
        "variadic keyword": "**",
    }.get(parameter.kind, "")
    rendered = f"{prefix}{parameter.name}"
    if parameter.annotation is not None:
        rendered += f": {parameter.annotation}"
    if not parameter.required:
        rendered += f" = {parameter.default}"
    return rendered


def render_signature(
    name: str,
    parameters: Sequence[ParameterFact],
    return_annotation: str | None,
) -> str:
    pieces: list[str] = []
    has_variadic_positional = any(p.kind == "variadic positional" for p in parameters)
    keyword_separator_inserted = has_variadic_positional
    for index, parameter in enumerate(parameters):
        if parameter.kind == "keyword-only" and not keyword_separator_inserted:
            pieces.append("*")
            keyword_separator_inserted = True
        pieces.append(_render_parameter(parameter))
        next_kind = parameters[index + 1].kind if index + 1 < len(parameters) else None
        if parameter.kind == "positional-only" and next_kind != "positional-only":
            pieces.append("/")
    rendered = f"{name}({', '.join(pieces)})"
    if return_annotation is not None:
        rendered += f" -> {return_annotation}"
    return rendered
