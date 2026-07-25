from __future__ import annotations

import ast
from copy import deepcopy


def _clear_docstrings(node: ast.AST) -> ast.AST:
    copied = deepcopy(node)
    for candidate in ast.walk(copied):
        body = getattr(candidate, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            first.value.value = ""
            first.value.kind = None
    return copied


def docstring_ast_unchanged(before: bytes, after: bytes) -> bool:
    try:
        before_tree = ast.parse(before.decode("utf-8"))
        after_tree = ast.parse(after.decode("utf-8"))
    except (UnicodeDecodeError, SyntaxError):
        return False
    return ast.dump(
        _clear_docstrings(before_tree),
        include_attributes=False,
    ) == ast.dump(
        _clear_docstrings(after_tree),
        include_attributes=False,
    )
