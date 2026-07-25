"""Tree-sitter fact extraction for exported top-level TypeScript declarations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

import tree_sitter_typescript
from tree_sitter import Language, Node, Parser

from drift_agent.domain.models import (
    CodeFact,
    ParameterFact,
    ProviderIssue,
    SourceAnchor,
    SymbolIdentity,
    UnsupportedSymbolFact,
)
from drift_agent.hashing import sha256_bytes
from drift_agent.path_safety import repository_path_without_symlinks

_Dialect = Literal["typescript", "tsx"]
_CallableCategory = Literal["module_function", "method"]
_DeclarationCategory = Literal["class", "interface", "type_alias", "enum", "value"]

_TS_SUFFIXES = frozenset({".ts", ".tsx"})
_PARSERS: dict[_Dialect, Parser] = {}


class TypeScriptProviderError(RuntimeError):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _parser(dialect: _Dialect) -> Parser:
    parser = _PARSERS.get(dialect)
    if parser is None:
        pointer = (
            tree_sitter_typescript.language_tsx()
            if dialect == "tsx"
            else tree_sitter_typescript.language_typescript()
        )
        parser = Parser(Language(pointer))
        _PARSERS[dialect] = parser
    return parser


@dataclass(frozen=True)
class _Candidate:
    relative_path: str
    source_root: str
    module_name: str
    raw: bytes
    source_hash: str
    source_version: str


def _is_candidate_name(relative_path: str) -> bool:
    return PurePosixPath(relative_path).suffix in _TS_SUFFIXES and not relative_path.endswith(
        ".d.ts"
    )


def _candidate_paths(
    repo_path: Path,
    source_root_name: str,
    changed: set[str] | None,
) -> list[str]:
    source_root = repository_path_without_symlinks(repo_path, source_root_name)
    root = PurePosixPath(source_root_name)
    if changed is None:
        candidates = [
            path.relative_to(repo_path).as_posix()
            for pattern in ("*.ts", "*.tsx")
            for path in source_root.rglob(pattern)
        ]
    else:
        candidates = [path for path in changed if PurePosixPath(path).is_relative_to(root)]
    candidates = [path for path in candidates if _is_candidate_name(path)]
    for relative_path in sorted(candidates):
        repository_path_without_symlinks(repo_path, relative_path)
    return sorted(candidates)


def _module_name(source_root_name: str, relative_path: str) -> str:
    relative = PurePosixPath(relative_path).relative_to(PurePosixPath(source_root_name))
    parts = [*relative.parts[:-1], PurePosixPath(relative.parts[-1]).stem]
    # src/pkg/index.ts addresses the directory; a bare src/index.ts keeps the
    # stem so the module name never becomes empty.
    if len(parts) > 1 and parts[-1] == "index":
        parts.pop()
    return ".".join(parts)


def _accessor_kind(member: Node) -> str | None:
    for child in member.children:
        if child.type in ("get", "set"):
            return child.type
        if child.type == "property_identifier":
            break
    return None


def _text(candidate: _Candidate, node: Node) -> str:
    return candidate.raw[node.start_byte : node.end_byte].decode("utf-8")


def _node_anchor(candidate: _Candidate, node: Node) -> SourceAnchor:
    return _span_anchor(candidate, node.start_point[0] + 1, node.start_byte, node.end_byte)


def _span_anchor(candidate: _Candidate, line: int, start: int, end: int) -> SourceAnchor:
    return SourceAnchor(
        path=candidate.relative_path,
        line=line,
        start_byte=start,
        end_byte=end,
        exact_text=candidate.raw[start:end].decode("utf-8"),
        source_hash=candidate.source_hash,
    )


def _child_of_type(node: Node, type_name: str) -> Node | None:
    for child in node.named_children:
        if child.type == type_name:
            return child
    return None


def _annotation_text(candidate: _Candidate, type_annotation: Node | None) -> str | None:
    if type_annotation is None:
        return None
    return _text(candidate, type_annotation).removeprefix(":").strip()


def _is_async(node: Node) -> bool:
    return any(child.type == "async" for child in node.children)


def _parameter_name(candidate: _Candidate, pattern: Node, position: int) -> str:
    if pattern.type == "identifier":
        return _text(candidate, pattern)
    if pattern.type == "rest_pattern":
        inner = _child_of_type(pattern, "identifier")
        if inner is not None:
            return _text(candidate, inner)
    # Destructured patterns have no single source name; keep a positional
    # placeholder so the annotation stays comparable.
    return f"arg{position}"


def _parameters(candidate: _Candidate, function_node: Node) -> list[ParameterFact]:
    params_node = function_node.child_by_field_name("parameters")
    if params_node is None:
        single = function_node.child_by_field_name("parameter")
        if single is None:
            return []
        return [
            ParameterFact(
                name=_text(candidate, single),
                kind="required",
                required=True,
                default_present=False,
                annotation_present=False,
                position=0,
                anchor=_node_anchor(candidate, single),
            )
        ]
    result: list[ParameterFact] = []
    for index, node in enumerate(params_node.named_children):
        if node.type not in ("required_parameter", "optional_parameter"):
            continue
        pattern = node.child_by_field_name("pattern")
        if index == 0 and pattern is not None and pattern.type == "this":
            # TypeScript's explicit receiver is typing-only, never a call-site
            # parameter.
            continue
        default_node = node.child_by_field_name("value")
        default = None if default_node is None else _text(candidate, default_node)
        annotation = _annotation_text(candidate, node.child_by_field_name("type"))
        if pattern is not None and pattern.type == "rest_pattern":
            kind = "rest"
            required = True
        elif node.type == "optional_parameter" or default is not None:
            kind = "optional"
            required = False
        else:
            kind = "required"
            required = True
        position = len(result)
        name = (
            f"arg{position}" if pattern is None else _parameter_name(candidate, pattern, position)
        )
        result.append(
            ParameterFact(
                name=name,
                kind=kind,
                annotation=annotation,
                default=default,
                required=required,
                default_present=default is not None,
                annotation_present=annotation is not None,
                position=position,
                anchor=_node_anchor(candidate, node),
            )
        )
    return result


def _render_signature(
    name: str,
    parameters: list[ParameterFact],
    return_annotation: str | None,
) -> str:
    rendered = []
    for parameter in parameters:
        if parameter.kind == "rest":
            text = f"...{parameter.name}"
        elif parameter.kind == "optional" and parameter.default is None:
            text = f"{parameter.name}?"
        else:
            text = parameter.name
        if parameter.annotation is not None:
            text += f": {parameter.annotation}"
        if parameter.default is not None:
            text += f" = {parameter.default}"
        rendered.append(text)
    signature = f"{name}({', '.join(rendered)})"
    if return_annotation is not None:
        signature += f": {return_annotation}"
    return signature


def _doc_comment_anchor(candidate: _Candidate, statement: Node) -> SourceAnchor | None:
    sibling = statement.prev_named_sibling
    if sibling is None or sibling.type != "comment":
        return None
    if not _text(candidate, sibling).startswith("/**"):
        return None
    return _node_anchor(candidate, sibling)


def _identity(
    module: str,
    owner: str | None,
    name: str,
    category: _CallableCategory | _DeclarationCategory,
) -> SymbolIdentity:
    return SymbolIdentity(
        version="typescript-symbol-v1",
        module=module,
        owner=owner,
        name=name,
        category=category,
    )


def _callable_fact(
    candidate: _Candidate,
    *,
    statement: Node,
    declaration: Node,
    function_node: Node,
    name_node: Node,
    owner: str | None,
    category: _CallableCategory,
) -> CodeFact:
    name = _text(candidate, name_node)
    parameters = _parameters(candidate, function_node)
    return_type = function_node.child_by_field_name("return_type")
    return_annotation = _annotation_text(candidate, return_type)
    signature_end_node = (
        return_type
        or function_node.child_by_field_name("parameters")
        or function_node.child_by_field_name("parameter")
        or name_node
    )
    return CodeFact(
        symbol_id=".".join(part for part in (candidate.module_name, owner, name) if part),
        symbol_identity=_identity(candidate.module_name, owner, name, category),
        category=category,
        owner=owner,
        is_async=_is_async(function_node),
        language="typescript",
        path=candidate.relative_path,
        line=declaration.start_point[0] + 1,
        signature=_render_signature(name, parameters, return_annotation),
        parameters=parameters,
        return_annotation=return_annotation,
        return_annotation_present=return_annotation is not None,
        source_hash=candidate.source_hash,
        name_anchor=_node_anchor(candidate, name_node),
        signature_anchor=_span_anchor(
            candidate,
            declaration.start_point[0] + 1,
            declaration.start_byte,
            signature_end_node.end_byte,
        ),
        docstring_anchor=_doc_comment_anchor(candidate, statement),
        source_version=candidate.source_version,
    )


def _declaration_fact(
    candidate: _Candidate,
    *,
    statement: Node,
    declaration: Node,
    name_node: Node,
    category: _DeclarationCategory,
    signature: str,
    signature_end: int,
) -> CodeFact:
    name = _text(candidate, name_node)
    return CodeFact(
        symbol_id=f"{candidate.module_name}.{name}",
        symbol_identity=_identity(candidate.module_name, None, name, category),
        category=category,
        language="typescript",
        path=candidate.relative_path,
        line=declaration.start_point[0] + 1,
        signature=signature,
        parameters=[],
        source_hash=candidate.source_hash,
        name_anchor=_node_anchor(candidate, name_node),
        signature_anchor=_span_anchor(
            candidate,
            declaration.start_point[0] + 1,
            declaration.start_byte,
            signature_end,
        ),
        docstring_anchor=_doc_comment_anchor(candidate, statement),
        source_version=candidate.source_version,
    )


class TypeScriptFactProvider:
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
    ) -> _Candidate | None:
        if not _is_candidate_name(relative_path):
            return None
        if raw is None:
            path = repository_path_without_symlinks(repo_path, relative_path)
            try:
                raw = path.read_bytes()
            except OSError as error:
                raise TypeScriptProviderError(
                    "provider.read", f"could not read {relative_path}: {error}"
                ) from error
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError as error:
            self.issues.append(
                ProviderIssue(
                    path=relative_path,
                    reason_code="unsupported.ts_parse",
                    message=f"could not decode {relative_path}: {error}",
                    line=1,
                    source_hash=sha256_bytes(raw),
                )
            )
            return None
        return _Candidate(
            relative_path=relative_path,
            source_root=source_root,
            module_name=_module_name(source_root, relative_path),
            raw=raw,
            source_hash=sha256_bytes(raw),
            source_version=source_version,
        )

    def _collect_candidate(self, candidate: _Candidate) -> list[CodeFact]:
        dialect: _Dialect = "tsx" if candidate.relative_path.endswith(".tsx") else "typescript"
        tree = _parser(dialect).parse(candidate.raw)
        root = tree.root_node
        if root.has_error:
            self.issues.append(
                ProviderIssue(
                    path=candidate.relative_path,
                    reason_code="unsupported.ts_parse",
                    message=f"could not fully parse {candidate.relative_path}",
                    line=1,
                    source_hash=candidate.source_hash,
                )
            )
        facts: list[CodeFact] = []
        for statement in root.named_children:
            # Error-free exported declarations remain usable even when other
            # statements in the file failed to parse.
            if statement.type != "export_statement" or statement.has_error:
                continue
            for declaration in statement.named_children:
                facts.extend(self._declaration_facts(candidate, statement, declaration))
        return facts

    def _declaration_facts(
        self,
        candidate: _Candidate,
        statement: Node,
        declaration: Node,
    ) -> list[CodeFact]:
        # Anonymous default exports, `export *`, and re-export clauses carry no
        # named declaration node and are intentionally skipped without issues.
        if declaration.type == "function_declaration":
            name_node = declaration.child_by_field_name("name")
            if name_node is None:
                return []
            return [
                _callable_fact(
                    candidate,
                    statement=statement,
                    declaration=declaration,
                    function_node=declaration,
                    name_node=name_node,
                    owner=None,
                    category="module_function",
                )
            ]
        if declaration.type in ("class_declaration", "abstract_class_declaration"):
            return self._class_facts(candidate, statement, declaration)
        if declaration.type in ("interface_declaration", "enum_declaration"):
            name_node = declaration.child_by_field_name("name")
            if name_node is None:
                return []
            category: _DeclarationCategory = (
                "interface" if declaration.type == "interface_declaration" else "enum"
            )
            return [
                _declaration_fact(
                    candidate,
                    statement=statement,
                    declaration=declaration,
                    name_node=name_node,
                    category=category,
                    signature=f"{category} {_text(candidate, name_node)}",
                    signature_end=name_node.end_byte,
                )
            ]
        if declaration.type == "type_alias_declaration":
            name_node = declaration.child_by_field_name("name")
            value_node = declaration.child_by_field_name("value")
            if name_node is None or value_node is None:
                return []
            return [
                _declaration_fact(
                    candidate,
                    statement=statement,
                    declaration=declaration,
                    name_node=name_node,
                    category="type_alias",
                    signature=(
                        f"type {_text(candidate, name_node)} = {_text(candidate, value_node)}"
                    ),
                    signature_end=value_node.end_byte,
                )
            ]
        if declaration.type == "lexical_declaration":
            return self._lexical_facts(candidate, statement, declaration)
        return []

    def _class_facts(
        self,
        candidate: _Candidate,
        statement: Node,
        declaration: Node,
    ) -> list[CodeFact]:
        name_node = declaration.child_by_field_name("name")
        body = declaration.child_by_field_name("body")
        if name_node is None:
            return []
        class_name = _text(candidate, name_node)
        facts = [
            _declaration_fact(
                candidate,
                statement=statement,
                declaration=declaration,
                name_node=name_node,
                category="class",
                signature=f"class {class_name}",
                signature_end=name_node.end_byte,
            )
        ]
        members = [] if body is None else body.named_children
        getter_names = {
            _text(candidate, name)
            for member in members
            if member.type == "method_definition" and _accessor_kind(member) == "get"
            for name in [member.child_by_field_name("name")]
            if name is not None
        }
        for member in members:
            if member.type != "method_definition":
                continue
            member_name = member.child_by_field_name("name")
            if member_name is None:
                continue
            # A get/set accessor pair shares one property name; keeping both
            # would collide on symbol_id and silently disable alignment, so
            # the getter is the property's canonical fact.
            if _accessor_kind(member) == "set" and _text(candidate, member_name) in getter_names:
                continue
            facts.append(
                _callable_fact(
                    candidate,
                    statement=member,
                    declaration=member,
                    function_node=member,
                    name_node=member_name,
                    owner=class_name,
                    category="method",
                )
            )
        return facts

    def _lexical_facts(
        self,
        candidate: _Candidate,
        statement: Node,
        declaration: Node,
    ) -> list[CodeFact]:
        facts: list[CodeFact] = []
        for declarator in declaration.named_children:
            if declarator.type != "variable_declarator":
                continue
            name_node = declarator.child_by_field_name("name")
            if name_node is None or name_node.type != "identifier":
                continue
            value_node = declarator.child_by_field_name("value")
            if value_node is not None and value_node.type == "arrow_function":
                facts.append(
                    _callable_fact(
                        candidate,
                        statement=statement,
                        declaration=declarator,
                        function_node=value_node,
                        name_node=name_node,
                        owner=None,
                        category="module_function",
                    )
                )
                continue
            name = _text(candidate, name_node)
            annotation = _annotation_text(candidate, _child_of_type(declarator, "type_annotation"))
            facts.append(
                _declaration_fact(
                    candidate,
                    statement=statement,
                    declaration=declarator,
                    name_node=name_node,
                    category="value",
                    signature=name if annotation is None else f"{name}: {annotation}",
                    signature_end=declarator.end_byte,
                )
            )
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
        if candidate is None:
            return []
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
            for relative_path in _candidate_paths(repo_path, source_root, changed):
                candidate = self._candidate(repo_path, source_root, relative_path)
                if candidate is None:
                    continue
                facts.extend(self._collect_candidate(candidate))
        by_symbol: dict[str, set[str]] = {}
        for fact in facts:
            by_symbol.setdefault(fact.symbol_id, set()).add(fact.path)
        for symbol_id, paths in sorted(by_symbol.items()):
            # x/index.ts and a sibling x.ts both collapse to module "x"; the
            # collision silently disables alignment downstream, so surface it.
            if len(paths) > 1:
                self.issues.append(
                    ProviderIssue(
                        path=min(paths),
                        line=1,
                        symbol_id=symbol_id,
                        source_hash="",
                        reason_code="ambiguity.ts_module",
                        message=(
                            "exported symbol id is produced by multiple files: "
                            + ", ".join(sorted(paths))
                        ),
                    )
                )
        return sorted(
            facts,
            key=lambda fact: (fact.symbol_id, fact.path, fact.line),
        )
