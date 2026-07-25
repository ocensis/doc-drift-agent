from __future__ import annotations

import ast
import io
import tokenize
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from drift_agent.domain.models import (
    CodeFact,
    ParameterFact,
    ProviderIssue,
    SemanticCodeFact,
    SourceAnchor,
    SymbolIdentity,
    UnsupportedSymbolFact,
)
from drift_agent.domain.semantic import parse_semantic_scalar, semantic_scalar_from_value
from drift_agent.domain.signatures import render_signature
from drift_agent.hashing import sha256_bytes
from drift_agent.path_safety import repository_path_without_symlinks


class UnsupportedPythonLayoutError(RuntimeError):
    pass


class PythonProviderError(RuntimeError):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class _Candidate:
    relative_path: str
    source_root: str
    module_name: str
    raw: bytes
    source: str
    source_hash: str
    source_version: str


def _candidate_paths(
    repo_path: Path,
    source_root_name: str,
    changed: set[str] | None,
) -> list[str]:
    source_root = repository_path_without_symlinks(repo_path, source_root_name)
    root = PurePosixPath(source_root_name)
    if changed is None:
        candidates = [path.relative_to(repo_path).as_posix() for path in source_root.rglob("*.py")]
    else:
        candidates = [
            path
            for path in changed
            if PurePosixPath(path).suffix == ".py" and PurePosixPath(path).is_relative_to(root)
        ]
    for relative_path in sorted(candidates):
        repository_path_without_symlinks(repo_path, relative_path)
    return sorted(candidates)


def _unsupported_python_paths(
    source_root_name: str,
    repo_path: Path,
    candidates: list[str],
) -> list[str]:
    root = PurePosixPath(source_root_name)
    unsupported: list[str] = []
    for relative_path in candidates:
        candidate = PurePosixPath(relative_path).relative_to(root)
        if len(candidate.parts) < 2:
            unsupported.append(relative_path)
            continue
        package_path = root
        for directory in candidate.parts[:-1]:
            package_path /= directory
            marker_name = (package_path / "__init__.py").as_posix()
            marker = repo_path / marker_name
            if marker.is_symlink():
                repository_path_without_symlinks(repo_path, marker_name)
            if not marker.is_file():
                unsupported.append(relative_path)
                break
            repository_path_without_symlinks(repo_path, marker_name)
    return sorted(unsupported)


def _module_name(source_root_name: str, relative_path: str) -> str:
    relative = PurePosixPath(relative_path).relative_to(PurePosixPath(source_root_name))
    parts = list(relative.parts)
    if parts[-1] == "__init__.py":
        parts.pop()
    else:
        parts[-1] = PurePosixPath(parts[-1]).stem
    return ".".join(parts)


def _line_offsets(raw: bytes) -> list[int]:
    offsets = [0]
    for line in raw.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    return offsets


def _node_span(raw: bytes, node: ast.AST) -> tuple[int, int]:
    lineno = getattr(node, "lineno", None)
    col = getattr(node, "col_offset", None)
    end_lineno = getattr(node, "end_lineno", None)
    end_col = getattr(node, "end_col_offset", None)
    if (
        not isinstance(lineno, int)
        or not isinstance(col, int)
        or not isinstance(end_lineno, int)
        or not isinstance(end_col, int)
    ):
        raise PythonProviderError("provider.syntax", "AST node has no exact byte range")
    offsets = _line_offsets(raw)
    return offsets[lineno - 1] + col, offsets[end_lineno - 1] + end_col


def _anchor(candidate: _Candidate, node: ast.AST) -> SourceAnchor:
    start, end = _node_span(candidate.raw, node)
    return SourceAnchor(
        path=candidate.relative_path,
        line=int(getattr(node, "lineno", 1)),
        start_byte=start,
        end_byte=end,
        exact_text=candidate.raw[start:end].decode("utf-8"),
        source_hash=candidate.source_hash,
    )


def _signature_span(
    candidate: _Candidate, node: ast.FunctionDef | ast.AsyncFunctionDef
) -> SourceAnchor:
    start, _ = _node_span(candidate.raw, node)
    fragment = candidate.raw[start:].decode("utf-8")
    depth = 0
    colon_end: tuple[int, int] | None = None
    try:
        tokens = tokenize.generate_tokens(io.StringIO(fragment).readline)
        seen_def = False
        for token in tokens:
            if token.type == tokenize.NAME and token.string == "def":
                seen_def = True
                continue
            if not seen_def:
                continue
            if token.type == tokenize.OP:
                if token.string in "([{":
                    depth += 1
                elif token.string in ")]}" and depth:
                    depth -= 1
                elif token.string == ":" and depth == 0:
                    colon_end = token.end
                    break
    except (tokenize.TokenError, IndentationError) as error:
        raise PythonProviderError("provider.syntax", str(error)) from error
    if colon_end is None:
        raise PythonProviderError("provider.syntax", "function signature has no colon")
    lines = fragment.splitlines(keepends=True)
    local_end = sum(len(line.encode("utf-8")) for line in lines[: colon_end[0] - 1])
    local_end += len(lines[colon_end[0] - 1][: colon_end[1]].encode("utf-8"))
    end = start + local_end
    return SourceAnchor(
        path=candidate.relative_path,
        line=node.lineno,
        start_byte=start,
        end_byte=end,
        exact_text=candidate.raw[start:end].decode("utf-8"),
        source_hash=candidate.source_hash,
    )


def _name_anchor(
    candidate: _Candidate,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    signature: SourceAnchor,
) -> SourceAnchor:
    encoded_name = node.name.encode("utf-8")
    local = candidate.raw[signature.start_byte : signature.end_byte]
    index = local.find(encoded_name)
    if index < 0:
        raise PythonProviderError("provider.syntax", "function name is not locatable")
    start = signature.start_byte + index
    end = start + len(encoded_name)
    return SourceAnchor(
        path=candidate.relative_path,
        line=node.lineno,
        start_byte=start,
        end_byte=end,
        exact_text=node.name,
        source_hash=candidate.source_hash,
    )


def _expression(candidate: _Candidate, node: ast.expr | None) -> str | None:
    if node is None:
        return None
    start, end = _node_span(candidate.raw, node)
    return candidate.raw[start:end].decode("utf-8")


def _body_fingerprint(
    candidate: _Candidate,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> str:
    """Return a position-independent executable-body shape.

    A leading docstring is deliberately excluded: documentation edits must
    not create symbol-rename evidence.  The function declaration/signature is
    excluded so a real rename may still carry signature drift.
    """

    body = list(function.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    try:
        return ast.dump(
            ast.Module(body=body, type_ignores=[]),
            include_attributes=False,
        )
    except (UnicodeError, ValueError):
        # Python's repr guard rejects enormous integer constants.  Preserve a
        # deterministic, signature-independent rename fingerprint without
        # converting those values to decimal text.
        fragments = []
        for statement in body:
            start, end = _node_span(candidate.raw, statement)
            fragments.append(candidate.raw[start:end])
        digest = sha256_bytes(bytes([0]).join(fragments))
        return f"safe-body-v1:{digest}"


def _semantic_facts(
    candidate: _Candidate,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[SemanticCodeFact]:
    """Extract only behavior that is syntactically definite without execution."""

    if isinstance(function, ast.AsyncFunctionDef):
        return []
    body = list(function.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    if len(body) != 1 or not isinstance(body[0], ast.Return):
        return []
    returned = body[0].value
    if isinstance(returned, ast.Constant):
        normalized = semantic_scalar_from_value(returned.value)
    elif isinstance(returned, ast.UnaryOp):
        normalized = parse_semantic_scalar(_anchor(candidate, returned).exact_text)
    else:
        return []
    if normalized is None:
        return []
    return [
        SemanticCodeFact(
            normalized_value=normalized,
            anchor=_anchor(candidate, returned),
        )
    ]


def _parameters(
    candidate: _Candidate,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[ParameterFact]:
    result: list[ParameterFact] = []
    positional = [*function.args.posonlyargs, *function.args.args]
    defaults: list[ast.expr | None] = [None] * (
        len(positional) - len(function.args.defaults)
    ) + list(function.args.defaults)
    for index, (argument, default) in enumerate(zip(positional, defaults, strict=True)):
        result.append(
            ParameterFact(
                name=argument.arg,
                kind=(
                    "positional-only"
                    if index < len(function.args.posonlyargs)
                    else "positional or keyword"
                ),
                annotation=_expression(candidate, argument.annotation),
                default=_expression(candidate, default),
                required=default is None,
                default_present=default is not None,
                annotation_present=argument.annotation is not None,
                position=len(result),
                anchor=_anchor(candidate, argument),
            )
        )
    if function.args.vararg is not None:
        argument = function.args.vararg
        result.append(
            ParameterFact(
                name=argument.arg,
                kind="variadic positional",
                annotation=_expression(candidate, argument.annotation),
                required=True,
                default_present=False,
                annotation_present=argument.annotation is not None,
                position=len(result),
                anchor=_anchor(candidate, argument),
            )
        )
    for argument, default in zip(
        function.args.kwonlyargs,
        function.args.kw_defaults,
        strict=True,
    ):
        result.append(
            ParameterFact(
                name=argument.arg,
                kind="keyword-only",
                annotation=_expression(candidate, argument.annotation),
                default=_expression(candidate, default),
                required=default is None,
                default_present=default is not None,
                annotation_present=argument.annotation is not None,
                position=len(result),
                anchor=_anchor(candidate, argument),
            )
        )
    if function.args.kwarg is not None:
        argument = function.args.kwarg
        result.append(
            ParameterFact(
                name=argument.arg,
                kind="variadic keyword",
                annotation=_expression(candidate, argument.annotation),
                required=True,
                default_present=False,
                annotation_present=argument.annotation is not None,
                position=len(result),
                anchor=_anchor(candidate, argument),
            )
        )
    return result


def _decorator_kind(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    is_method: bool,
) -> Literal["plain", "staticmethod", "classmethod"] | None:
    if not node.decorator_list:
        return "plain"
    if is_method and len(node.decorator_list) == 1:
        decorator = node.decorator_list[0]
        if isinstance(decorator, ast.Name) and decorator.id == "staticmethod":
            return "staticmethod"
        if isinstance(decorator, ast.Name) and decorator.id == "classmethod":
            return "classmethod"
    return None


def _is_public(*parts: str | None) -> bool:
    return all(part is not None and not part.startswith("_") for part in parts)


class PythonFactProvider:
    def __init__(self) -> None:
        self.issues: list[ProviderIssue] = []
        self.unsupported_symbols: list[UnsupportedSymbolFact] = []

    def _candidate(
        self,
        repo_path: Path,
        source_root: str,
        relative_path: str,
        *,
        raw: bytes | None = None,
        source_version: str = "worktree",
    ) -> _Candidate:
        if raw is None:
            path = repository_path_without_symlinks(repo_path, relative_path)
            try:
                raw = path.read_bytes()
            except OSError as error:
                raise PythonProviderError(
                    "provider.read", f"could not read {relative_path}: {error}"
                ) from error
        try:
            source = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise PythonProviderError(
                "provider.encoding", f"could not decode {relative_path}: {error}"
            ) from error
        try:
            ast.parse(source, filename=relative_path)
        except SyntaxError as error:
            raise PythonProviderError(
                "provider.syntax", f"could not parse {relative_path}: {error}"
            ) from error
        return _Candidate(
            relative_path=relative_path,
            source_root=source_root,
            module_name=_module_name(source_root, relative_path),
            raw=raw,
            source=source,
            source_hash=sha256_bytes(raw),
            source_version=source_version,
        )

    def _collect_candidate(self, candidate: _Candidate) -> list[CodeFact]:
        module = ast.parse(candidate.source, filename=candidate.relative_path)
        facts: list[CodeFact] = []

        def add_function(
            function: ast.FunctionDef | ast.AsyncFunctionDef,
            owner: str | None,
        ) -> None:
            category: Literal["module_function", "method"] = (
                "method" if owner is not None else "module_function"
            )
            symbol_id = ".".join(
                part for part in (candidate.module_name, owner, function.name) if part
            )
            if not _is_public(*candidate.module_name.split("."), owner or "public", function.name):
                return
            decorator_kind = _decorator_kind(function, is_method=owner is not None)
            if decorator_kind is None:
                self.issues.append(
                    ProviderIssue(
                        path=candidate.relative_path,
                        line=function.lineno,
                        symbol_id=symbol_id,
                        source_hash=candidate.source_hash,
                        symbol_identity=SymbolIdentity(
                            module=candidate.module_name,
                            owner=owner,
                            name=function.name,
                            category=category,
                        ),
                        reason_code="unsupported.symbol_kind",
                        message="decorated functions are outside the Stage 2 support matrix",
                    )
                )
                return
            parameters = _parameters(candidate, function)
            returns = _expression(candidate, function.returns)
            signature_anchor = _signature_span(candidate, function)
            docstring_anchor: SourceAnchor | None = None
            if (
                function.body
                and isinstance(function.body[0], ast.Expr)
                and isinstance(function.body[0].value, ast.Constant)
                and isinstance(function.body[0].value.value, str)
            ):
                docstring_anchor = _anchor(candidate, function.body[0].value)
            facts.append(
                CodeFact(
                    symbol_id=symbol_id,
                    symbol_identity=SymbolIdentity(
                        module=candidate.module_name,
                        owner=owner,
                        name=function.name,
                        category=category,
                    ),
                    category=category,
                    owner=owner,
                    is_async=isinstance(function, ast.AsyncFunctionDef),
                    decorator_kind=decorator_kind,
                    path=candidate.relative_path,
                    line=function.lineno,
                    signature=render_signature(function.name, parameters, returns),
                    parameters=parameters,
                    return_annotation=returns,
                    return_annotation_present=function.returns is not None,
                    source_hash=candidate.source_hash,
                    name_anchor=_name_anchor(candidate, function, signature_anchor),
                    signature_anchor=signature_anchor,
                    docstring_anchor=docstring_anchor,
                    source_version=candidate.source_version,
                    body_fingerprint=_body_fingerprint(candidate, function),
                    semantic_facts=_semantic_facts(candidate, function),
                )
            )

        for node in module.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                add_function(node, None)
            elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
                category: Literal["class", "exception_class"] = "class"
                base_names = [
                    base.id
                    if isinstance(base, ast.Name)
                    else base.attr
                    if isinstance(base, ast.Attribute)
                    else ""
                    for base in node.bases
                ]
                if node.name.endswith(("Error", "Exception")) or any(
                    name.endswith(("Error", "Exception")) for name in base_names
                ):
                    category = "exception_class"
                class_anchor = _anchor(candidate, node)
                name_bytes = node.name.encode("utf-8")
                class_bytes = candidate.raw[class_anchor.start_byte : class_anchor.end_byte]
                name_offset = class_bytes.find(name_bytes)
                if name_offset < 0:
                    raise PythonProviderError("provider.syntax", "class name is not locatable")
                name_start = class_anchor.start_byte + name_offset
                self.unsupported_symbols.append(
                    UnsupportedSymbolFact(
                        symbol_id=f"{candidate.module_name}.{node.name}",
                        symbol_identity=SymbolIdentity(
                            module=candidate.module_name,
                            name=node.name,
                            category=category,
                        ),
                        path=candidate.relative_path,
                        line=node.lineno,
                        source_hash=candidate.source_hash,
                        name_anchor=SourceAnchor(
                            path=candidate.relative_path,
                            line=node.lineno,
                            start_byte=name_start,
                            end_byte=name_start + len(name_bytes),
                            exact_text=node.name,
                            source_hash=candidate.source_hash,
                        ),
                        source_version=candidate.source_version,
                    )
                )
                if _is_public(*candidate.module_name.split("."), node.name):
                    for member in node.body:
                        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            add_function(member, node.name)
        return facts

    def collect_bytes(
        self,
        *,
        repo_path: Path,
        source_root: str,
        relative_path: str,
        raw: bytes,
        source_version: str = "head",
    ) -> list[CodeFact]:
        candidate = self._candidate(
            repo_path.resolve(),
            source_root,
            relative_path,
            raw=raw,
            source_version=source_version,
        )
        return self._collect_candidate(candidate)

    def collect(
        self,
        *,
        repo_path: Path,
        source_roots: list[str],
        changed_paths: list[str] | None,
    ) -> list[CodeFact]:
        repo_path = repo_path.resolve()
        self.issues = []
        self.unsupported_symbols = []
        changed = None if changed_paths is None else set(changed_paths)
        facts: list[CodeFact] = []
        for source_root in source_roots:
            repository_path_without_symlinks(repo_path, source_root)
            candidates = _candidate_paths(repo_path, source_root, changed)
            if changed is not None and not candidates:
                continue
            unsupported = _unsupported_python_paths(source_root, repo_path, candidates)
            if unsupported:
                paths = ", ".join(unsupported)
                raise UnsupportedPythonLayoutError(
                    f"unsupported Python layout under {source_root}: {paths}"
                )
            if not candidates and changed is None:
                raise UnsupportedPythonLayoutError(
                    f"no traditional package found under {source_root}"
                )
            for relative_path in candidates:
                candidate = self._candidate(repo_path, source_root, relative_path)
                facts.extend(self._collect_candidate(candidate))
        return sorted(
            facts,
            key=lambda fact: (fact.symbol_id, fact.path, fact.line),
        )
