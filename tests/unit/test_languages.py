from __future__ import annotations

import pytest

from drift_agent.languages import is_doc_path, is_source_path, source_language


@pytest.mark.parametrize(
    ("path", "language"),
    [
        ("mod.py", "python"),
        ("mod.ts", "typescript"),
        ("mod.tsx", "typescript"),
        ("src/pkg/mod.py", "python"),
        ("src/pkg/component.ts", "typescript"),
        ("a/b/c/widget.tsx", "typescript"),
        ("types/component.d.ts", "typescript"),
        ("README.md", None),
        ("notes.txt", None),
        ("Makefile", None),
        ("README", None),
        (".gitignore", None),
        ("pkg.dir/file", None),
    ],
)
def test_source_language_maps_only_source_suffixes(path: str, language: str | None) -> None:
    assert source_language(path) == language


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("mod.py", True),
        ("mod.ts", True),
        ("mod.tsx", True),
        ("src/deep/mod.ts", True),
        ("component.d.ts", True),
        ("README.md", False),
        ("notes.txt", False),
        ("Makefile", False),
        ("README", False),
        (".gitignore", False),
        ("pkg.dir/file", False),
    ],
)
def test_is_source_path(path: str, expected: bool) -> None:
    assert is_source_path(path) is expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("README.md", True),
        ("docs/guide/notes.md", True),
        ("mod.py", False),
        ("mod.ts", False),
        ("mod.tsx", False),
        # .txt is deliberately not a doc suffix here.
        ("notes.txt", False),
        ("Makefile", False),
        (".gitignore", False),
    ],
)
def test_is_doc_path(path: str, expected: bool) -> None:
    assert is_doc_path(path) is expected


def test_txt_and_extensionless_paths_are_neither_source_nor_doc() -> None:
    for path in ("notes.txt", "Makefile", "README", ".gitignore", "pkg.dir/file"):
        assert not is_source_path(path)
        assert not is_doc_path(path)
        assert source_language(path) is None


def test_source_and_doc_classifications_are_disjoint() -> None:
    for path in ("mod.py", "mod.ts", "mod.tsx", "README.md"):
        assert is_source_path(path) != is_doc_path(path)


def test_source_language_is_set_exactly_when_path_is_source() -> None:
    for path in ("a.py", "a.ts", "a.tsx", "a.md", "a.txt", "a", "b/c.d.ts"):
        assert (source_language(path) is not None) is is_source_path(path)
