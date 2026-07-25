"""A stable module body makes Git prove this path rename.

The documentation declaration remains intentionally stale while the function
name, annotation, and default all change together.  Rename and signature
patchers consequently propose different bytes for the exact same declaration
anchor, exercising replacement conflict handling.
"""


def paint(color: str = "blue") -> None:
    ...
