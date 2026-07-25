"""Single source of truth for source-file language classification.

Every suffix-based dispatch (scope resolution, evidence collection, provider
selection) must route through this module instead of inlining suffix literals.
"""

from __future__ import annotations

from pathlib import PurePosixPath

SOURCE_SUFFIXES: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
}
DOC_SUFFIXES: frozenset[str] = frozenset({".md"})


def source_language(path: str) -> str | None:
    """Return the language id for a source path, or None for non-source files."""

    return SOURCE_SUFFIXES.get(PurePosixPath(path).suffix)


def is_source_path(path: str) -> bool:
    return PurePosixPath(path).suffix in SOURCE_SUFFIXES


def is_doc_path(path: str) -> bool:
    return PurePosixPath(path).suffix in DOC_SUFFIXES


__all__ = ["DOC_SUFFIXES", "SOURCE_SUFFIXES", "is_doc_path", "is_source_path", "source_language"]
