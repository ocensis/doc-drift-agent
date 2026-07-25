from __future__ import annotations

from typing import Final

MISSING_PARAMETER: Final[dict[str, str]] = {"type": "missing_parameter"}
MISSING_ANNOTATION: Final[dict[str, str]] = {"type": "missing_annotation"}
MISSING_DEFAULT: Final[dict[str, str]] = {"type": "missing_default"}
MISSING_RETURN: Final[dict[str, str]] = {"type": "missing_return"}
MISSING_SYMBOL: Final[dict[str, str]] = {"type": "missing_symbol"}
MISSING_DOCSTRING_FIELD: Final[dict[str, str]] = {"type": "missing_docstring_field"}
