from pathlib import Path

import pytest

from drift_agent.providers.python_facts import (
    PythonFactProvider,
    UnsupportedPythonLayoutError,
)


def test_collects_public_function_signature_without_importing_package(tmp_path: Path) -> None:
    package = tmp_path / "src/click_demo"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "api.py").write_text(
        """\
def echo(message: str, color: bool = True) -> None:
    raise RuntimeError("must never execute")
""",
        encoding="utf-8",
    )

    facts = PythonFactProvider().collect(
        repo_path=tmp_path,
        source_roots=["src"],
        changed_paths=["src/click_demo/api.py"],
    )

    assert len(facts) == 1
    assert facts[0].symbol_id == "click_demo.api.echo"
    assert facts[0].signature == "echo(message: str, color: bool = True) -> None"
    assert [parameter.required for parameter in facts[0].parameters] == [True, False]


def test_variadic_parameters_do_not_receive_griffe_sentinel_defaults(tmp_path: Path) -> None:
    package = tmp_path / "src/click_demo"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "api.py").write_text(
        "def relay(*messages: str, **flags: bool) -> None: ...\n",
        encoding="utf-8",
    )

    fact = PythonFactProvider().collect(
        repo_path=tmp_path,
        source_roots=["src"],
        changed_paths=["src/click_demo/api.py"],
    )[0]

    assert fact.signature == "relay(*messages: str, **flags: bool) -> None"
    assert [parameter.default for parameter in fact.parameters] == [None, None]
    assert [parameter.required for parameter in fact.parameters] == [True, True]


def test_flat_module_layout_fails_explicitly(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "api.py").write_text("def echo() -> None: ...\n", encoding="utf-8")

    with pytest.raises(UnsupportedPythonLayoutError):
        PythonFactProvider().collect(
            repo_path=tmp_path,
            source_roots=["src"],
            changed_paths=["src/api.py"],
        )


def test_scan_all_rejects_flat_module_mixed_with_traditional_package(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src"
    package = source / "click_demo"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (source / "standalone.py").write_text(
        "def unsupported() -> None: ...\n",
        encoding="utf-8",
    )

    with pytest.raises(UnsupportedPythonLayoutError, match=r"src/standalone\.py"):
        PythonFactProvider().collect(
            repo_path=tmp_path,
            source_roots=["src"],
            changed_paths=None,
        )


def test_changed_scope_rejects_namespace_package_mixed_with_traditional_package(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src"
    package = source / "click_demo"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    namespace = source / "plugins"
    namespace.mkdir()
    (namespace / "api.py").write_text(
        "def unsupported() -> None: ...\n",
        encoding="utf-8",
    )

    with pytest.raises(UnsupportedPythonLayoutError, match=r"src/plugins/api\.py"):
        PythonFactProvider().collect(
            repo_path=tmp_path,
            source_roots=["src"],
            changed_paths=["src/plugins/api.py"],
        )


def test_changed_scope_rejects_nested_namespace_package(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src"
    package = source / "click_demo"
    namespace = package / "plugins"
    namespace.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (namespace / "api.py").write_text(
        "def unsupported() -> None: ...\n",
        encoding="utf-8",
    )

    with pytest.raises(UnsupportedPythonLayoutError, match=r"src/click_demo/plugins/api\.py"):
        PythonFactProvider().collect(
            repo_path=tmp_path,
            source_roots=["src"],
            changed_paths=["src/click_demo/plugins/api.py"],
        )


def test_malformed_requested_candidate_fails_explicitly(tmp_path: Path) -> None:
    package = tmp_path / "src/click_demo"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "api.py").write_text("def echo(\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match=r"src/click_demo/api\.py"):
        PythonFactProvider().collect(
            repo_path=tmp_path,
            source_roots=["src"],
            changed_paths=["src/click_demo/api.py"],
        )


def test_empty_requested_module_is_valid(tmp_path: Path) -> None:
    package = tmp_path / "src/click_demo"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "empty.py").write_text("", encoding="utf-8")

    facts = PythonFactProvider().collect(
        repo_path=tmp_path,
        source_roots=["src"],
        changed_paths=["src/click_demo/empty.py"],
    )

    assert facts == []


def test_unrequested_sibling_is_never_read_by_griffe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = tmp_path / "src/click_demo"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    api = package / "api.py"
    api.write_text("def echo(message: str) -> None: ...\n", encoding="utf-8")
    excluded = package / "excluded.py"
    excluded.write_text("def secret() -> None: ...\n", encoding="utf-8")
    read_paths: list[Path] = []
    real_read_text = Path.read_text

    def record_read(path: Path, *args: object, **kwargs: object) -> str:
        read_paths.append(path)
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", record_read)

    facts = PythonFactProvider().collect(
        repo_path=tmp_path,
        source_roots=["src"],
        changed_paths=["src/click_demo/api.py"],
    )

    assert [fact.symbol_id for fact in facts] == ["click_demo.api.echo"]
    assert excluded not in read_paths


@pytest.mark.parametrize("symlink_kind", ["final", "component"])
def test_requested_python_symlink_fails_explicitly(
    tmp_path: Path,
    symlink_kind: str,
) -> None:
    source = tmp_path / "src"
    package = source / "click_demo"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    target_package = tmp_path / "notes/plugins"
    target_package.mkdir(parents=True)
    target = target_package / "api.py"
    target.write_text("def echo() -> None: ...\n", encoding="utf-8")
    if symlink_kind == "final":
        (package / "api.py").symlink_to(target)
    else:
        (package / "plugins").symlink_to(target_package, target_is_directory=True)

    requested = (
        "src/click_demo/api.py" if symlink_kind == "final" else "src/click_demo/plugins/api.py"
    )
    with pytest.raises(RuntimeError, match="symlink"):
        PythonFactProvider().collect(
            repo_path=tmp_path,
            source_roots=["src"],
            changed_paths=[requested],
        )


def test_collects_only_functions_with_fully_public_symbol_paths(tmp_path: Path) -> None:
    package = tmp_path / "src/click_demo"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "_internal.py").write_text(
        "def helper() -> None: ...\n",
        encoding="utf-8",
    )
    (package / "api.py").write_text(
        """\
class _Private:
    def helper(self) -> None: ...

def visible() -> None: ...
""",
        encoding="utf-8",
    )

    facts = PythonFactProvider().collect(
        repo_path=tmp_path,
        source_roots=["src"],
        changed_paths=None,
    )

    assert [fact.symbol_id for fact in facts] == ["click_demo.api.visible"]


def test_collects_supported_function_and_method_matrix_and_rejects_decorators(
    tmp_path: Path,
) -> None:
    package = tmp_path / "src/click_demo"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "api.py").write_text(
        """\
async def fetch(value: int = 0) -> str: ...

@custom
def decorated(value: int) -> str: ...

class API:
    def instance(self, value: int) -> str: ...

    @staticmethod
    def static(value: int) -> str: ...

    @classmethod
    def create(cls, value: int) -> str: ...

    @property
    def label(self) -> str: ...
""",
        encoding="utf-8",
    )
    provider = PythonFactProvider()

    facts = provider.collect(
        repo_path=tmp_path,
        source_roots=["src"],
        changed_paths=["src/click_demo/api.py"],
    )

    assert [fact.symbol_id for fact in facts] == [
        "click_demo.api.API.create",
        "click_demo.api.API.instance",
        "click_demo.api.API.static",
        "click_demo.api.fetch",
    ]
    by_name = {fact.symbol_identity.name: fact for fact in facts if fact.symbol_identity}
    assert by_name["fetch"].is_async is True
    assert by_name["fetch"].category == "module_function"
    assert by_name["instance"].decorator_kind == "plain"
    assert by_name["static"].decorator_kind == "staticmethod"
    assert by_name["create"].decorator_kind == "classmethod"
    assert by_name["instance"].owner == by_name["static"].owner == "API"
    assert [issue.symbol_id for issue in provider.issues] == [
        "click_demo.api.decorated",
        "click_demo.api.API.label",
    ]
    assert {issue.reason_code for issue in provider.issues} == {"unsupported.symbol_kind"}
    unsupported = [
        (item.symbol_identity.name, item.symbol_identity.category)
        for item in provider.unsupported_symbols
    ]
    assert unsupported == [("API", "class")]


def test_default_presence_preserves_none_false_and_original_expression_spelling(
    tmp_path: Path,
) -> None:
    package = tmp_path / "src/click_demo"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "api.py").write_text(
        "def values(required: str, optional: str | None = None, "
        "falsey: bool = False, count: int = 1_000) -> None: ...\n",
        encoding="utf-8",
    )

    fact = PythonFactProvider().collect(
        repo_path=tmp_path,
        source_roots=["src"],
        changed_paths=["src/click_demo/api.py"],
    )[0]

    assert [parameter.default for parameter in fact.parameters] == [
        None,
        "None",
        "False",
        "1_000",
    ]
    assert [parameter.default_present for parameter in fact.parameters] == [
        False,
        True,
        True,
        True,
    ]
    assert [parameter.required for parameter in fact.parameters] == [
        True,
        False,
        False,
        False,
    ]
