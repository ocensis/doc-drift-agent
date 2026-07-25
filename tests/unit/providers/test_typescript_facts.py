from __future__ import annotations

from pathlib import Path

from drift_agent.domain.models import CodeFact
from drift_agent.providers.typescript_facts import TypeScriptFactProvider


def _write(tmp_path: Path, relative: str, source: str) -> Path:
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def _collect(
    tmp_path: Path,
    changed_paths: list[str] | None = None,
) -> tuple[TypeScriptFactProvider, list[CodeFact]]:
    provider = TypeScriptFactProvider()
    facts = provider.collect(
        repo_path=tmp_path,
        source_roots=["src"],
        changed_paths=changed_paths,
    )
    return provider, facts


def _by_symbol(facts: list[CodeFact]) -> dict[str, CodeFact]:
    return {fact.symbol_id: fact for fact in facts}


def test_exported_function_parameters_types_optional_default_and_rest(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "src/api.ts",
        "export function greet(name: string, times?: number, "
        "flag: boolean = true, ...rest: string[]): string {\n"
        "  return name;\n"
        "}\n",
    )

    provider, facts = _collect(tmp_path)

    assert provider.issues == []
    assert len(facts) == 1
    fact = facts[0]
    assert fact.symbol_id == "api.greet"
    assert fact.language == "typescript"
    assert fact.category == "module_function"
    assert fact.is_async is False
    assert fact.symbol_identity is not None
    assert fact.symbol_identity.version == "typescript-symbol-v1"
    assert fact.signature == (
        "greet(name: string, times?: number, flag: boolean = true, ...rest: string[]): string"
    )
    assert [
        (parameter.name, parameter.kind, parameter.annotation, parameter.required)
        for parameter in fact.parameters
    ] == [
        ("name", "required", "string", True),
        ("times", "optional", "number", False),
        ("flag", "optional", "boolean", False),
        ("rest", "rest", "string[]", True),
    ]
    assert fact.parameters[2].default == "true"
    assert fact.return_annotation == "string"
    assert fact.return_annotation_present is True


def test_exported_arrow_const_is_a_module_function(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/api.ts",
        "export const add = async (a: number, b: number): Promise<number> => a + b;\n",
    )

    provider, facts = _collect(tmp_path)

    assert provider.issues == []
    assert len(facts) == 1
    fact = facts[0]
    assert fact.symbol_id == "api.add"
    assert fact.category == "module_function"
    assert fact.is_async is True
    assert fact.signature == "add(a: number, b: number): Promise<number>"
    assert fact.return_annotation == "Promise<number>"


def test_exported_class_produces_class_fact_and_all_method_facts(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/adapters/slack.ts",
        """\
export class SlackAdapter {
  send(message: string): void {}
  private hidden(count: number): void {}
  #secret(token: string): void {}
  async pull(): Promise<string> { return ""; }
}
""",
    )

    provider, facts = _collect(tmp_path)

    assert provider.issues == []
    by_symbol = _by_symbol(facts)
    assert set(by_symbol) == {
        "adapters.slack.SlackAdapter",
        "adapters.slack.SlackAdapter.send",
        "adapters.slack.SlackAdapter.hidden",
        "adapters.slack.SlackAdapter.#secret",
        "adapters.slack.SlackAdapter.pull",
    }
    assert by_symbol["adapters.slack.SlackAdapter"].category == "class"
    send = by_symbol["adapters.slack.SlackAdapter.send"]
    assert send.category == "method"
    assert send.owner == "SlackAdapter"
    assert send.signature == "send(message: string): void"
    assert by_symbol["adapters.slack.SlackAdapter.pull"].is_async is True


def test_interface_type_alias_enum_and_value_categories(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/types/message.ts",
        """\
export interface UnifiedMessage {
  id: string;
}
export type Channel = "slack" | "email";
export enum Color { Red, Green }
export const MAX_RETRIES: number = 5;
""",
    )

    provider, facts = _collect(tmp_path)

    assert provider.issues == []
    by_symbol = _by_symbol(facts)
    interface = by_symbol["types.message.UnifiedMessage"]
    assert interface.category == "interface"
    assert interface.signature == "interface UnifiedMessage"
    alias = by_symbol["types.message.Channel"]
    assert alias.category == "type_alias"
    assert alias.signature == 'type Channel = "slack" | "email"'
    assert by_symbol["types.message.Color"].category == "enum"
    value = by_symbol["types.message.MAX_RETRIES"]
    assert value.category == "value"
    assert value.parameters == []
    assert value.signature == "MAX_RETRIES: number"


def test_explicit_this_receiver_parameter_is_skipped(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/api.ts",
        "export function attach(this: Window, url: string): void {}\n",
    )

    _, facts = _collect(tmp_path)

    assert facts[0].signature == "attach(url: string): void"
    assert [parameter.name for parameter in facts[0].parameters] == ["url"]
    assert facts[0].parameters[0].position == 0


def test_destructured_parameter_uses_positional_placeholder(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/api.ts",
        "export function configure({retries, delay}: Options, name: string): void {}\n",
    )

    _, facts = _collect(tmp_path)

    assert [(parameter.name, parameter.annotation) for parameter in facts[0].parameters] == [
        ("arg0", "Options"),
        ("name", "string"),
    ]


def test_index_module_naming_drops_trailing_index_segment(tmp_path: Path) -> None:
    _write(tmp_path, "src/index.ts", "export function boot(): void {}\n")
    _write(tmp_path, "src/utils/index.ts", "export function helper(): void {}\n")

    _, facts = _collect(tmp_path)

    assert {fact.symbol_id for fact in facts} == {"index.boot", "utils.helper"}
    by_symbol = _by_symbol(facts)
    assert by_symbol["index.boot"].symbol_identity is not None
    assert by_symbol["index.boot"].symbol_identity.module == "index"
    assert by_symbol["utils.helper"].symbol_identity is not None
    assert by_symbol["utils.helper"].symbol_identity.module == "utils"


def test_declaration_files_are_skipped(tmp_path: Path) -> None:
    _write(tmp_path, "src/api.d.ts", "export function ambient(): void;\n")
    _write(tmp_path, "src/api.ts", "export function real(): void {}\n")

    provider, facts = _collect(tmp_path)

    assert provider.issues == []
    assert [fact.symbol_id for fact in facts] == ["api.real"]

    changed_provider, changed_facts = _collect(
        tmp_path, changed_paths=["src/api.d.ts", "src/api.ts"]
    )
    assert changed_provider.issues == []
    assert [fact.symbol_id for fact in changed_facts] == ["api.real"]


def test_non_exported_and_indirect_exports_are_ignored_without_issues(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "src/api.ts",
        """\
function local(): void {}
const hidden = (): void => {};
export default function () {}
export * from "./other";
export { local } from "./other";
export function visible(): void {}
""",
    )
    _write(tmp_path, "src/other.ts", "export const local = 1;\n")

    provider, facts = _collect(tmp_path, changed_paths=["src/api.ts"])

    assert provider.issues == []
    assert [fact.symbol_id for fact in facts] == ["api.visible"]


def test_parse_error_records_issue_and_keeps_error_free_declarations(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "src/api.ts",
        "export function ok(a: number): void {}\n\nexport function broken(( {\n",
    )

    provider, facts = _collect(tmp_path)

    assert [issue.reason_code for issue in provider.issues] == ["unsupported.ts_parse"]
    assert provider.issues[0].path == "src/api.ts"
    assert provider.issues[0].line == 1
    assert [fact.symbol_id for fact in facts] == ["api.ok"]


def test_anchors_are_byte_exact_including_doc_comment(tmp_path: Path) -> None:
    source = (
        "/** Greets loudly. */\nexport function greet(name: string): string {\n  return name;\n}\n"
    )
    path = _write(tmp_path, "src/api.ts", source)

    _, facts = _collect(tmp_path)

    raw = path.read_bytes()
    fact = facts[0]
    assert fact.name_anchor is not None
    assert raw[fact.name_anchor.start_byte : fact.name_anchor.end_byte] == b"greet"
    assert fact.signature_anchor is not None
    assert (
        raw[fact.signature_anchor.start_byte : fact.signature_anchor.end_byte]
        == b"function greet(name: string): string"
    )
    assert fact.docstring_anchor is not None
    assert (
        raw[fact.docstring_anchor.start_byte : fact.docstring_anchor.end_byte]
        == b"/** Greets loudly. */"
    )
    assert fact.docstring_anchor.exact_text == "/** Greets loudly. */"
    parameter = fact.parameters[0]
    assert parameter.anchor is not None
    assert raw[parameter.anchor.start_byte : parameter.anchor.end_byte] == b"name: string"


def test_multibyte_comment_keeps_anchors_byte_accurate(tmp_path: Path) -> None:
    source = "/** 发送消息到 Slack 频道。 */\nexport function send(text: string): void {}\n"
    path = _write(tmp_path, "src/api.ts", source)

    _, facts = _collect(tmp_path)

    raw = path.read_bytes()
    fact = facts[0]
    assert fact.docstring_anchor is not None
    doc = raw[fact.docstring_anchor.start_byte : fact.docstring_anchor.end_byte]
    assert doc.decode("utf-8") == "/** 发送消息到 Slack 频道。 */"
    assert fact.name_anchor is not None
    assert raw[fact.name_anchor.start_byte : fact.name_anchor.end_byte] == b"send"
    assert fact.name_anchor.exact_text == "send"
    assert fact.line == 2


def test_tsx_files_are_parsed_with_the_tsx_dialect(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/app.tsx",
        "export function App(props: { title: string }): JSX.Element "
        "{ return <div>{props.title}</div>; }\n",
    )

    provider, facts = _collect(tmp_path)

    assert provider.issues == []
    assert facts[0].symbol_id == "app.App"
    assert facts[0].parameters[0].annotation == "{ title: string }"


def test_collect_bytes_matches_collect(tmp_path: Path) -> None:
    source = (
        "/** Docs. */\n"
        "export function greet(name: string, times?: number): string {\n"
        "  return name;\n"
        "}\n"
        "export const MAX: number = 3;\n"
    )
    path = _write(tmp_path, "src/api.ts", source)

    provider, collected = _collect(tmp_path, changed_paths=["src/api.ts"])
    from_bytes = TypeScriptFactProvider().collect_bytes(
        repo_path=tmp_path,
        source_root="src",
        relative_path="src/api.ts",
        raw=path.read_bytes(),
        source_version="head",
    )

    assert provider.issues == []
    assert [fact.source_version for fact in collected] == ["worktree", "worktree"]
    assert [fact.source_version for fact in from_bytes] == ["head", "head"]
    normalize = [
        fact.model_copy(update={"source_version": "worktree"})
        for fact in sorted(from_bytes, key=lambda fact: (fact.symbol_id, fact.path, fact.line))
    ]
    assert normalize == collected


def test_collect_bytes_skips_declaration_files(tmp_path: Path) -> None:
    facts = TypeScriptFactProvider().collect_bytes(
        repo_path=tmp_path,
        source_root="src",
        relative_path="src/api.d.ts",
        raw=b"export function ambient(): void;\n",
    )

    assert facts == []
