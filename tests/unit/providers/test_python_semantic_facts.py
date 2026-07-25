from pathlib import Path

from drift_agent.providers.python_facts import PythonFactProvider


def _collect(tmp_path: Path, source: str | bytes):
    package = tmp_path / "src/click_demo"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    path = package / "api.py"
    if isinstance(source, str):
        path.write_text(source, encoding="utf-8")
    else:
        path.write_bytes(source)
    facts = PythonFactProvider().collect(
        repo_path=tmp_path,
        source_roots=["src"],
        changed_paths=["src/click_demo/api.py"],
    )
    return path, {fact.symbol_identity.name: fact for fact in facts if fact.symbol_identity}


def test_extracts_only_type_tagged_constant_returns_with_exact_byte_anchors(
    tmp_path: Path,
) -> None:
    source = (
        "# 中文前缀\r\n"
        "def nothing():\r\n"
        "    return None\r\n"
        "\r\n"
        "def truth():\r\n"
        '    """Optional docstring."""\r\n'
        "    return True\r\n"
        "\r\n"
        "def one():\r\n"
        "    return 1\r\n"
        "\r\n"
        "def negative():\r\n"
        "    return -7\r\n"
        "\r\n"
        "def minimum():\r\n"
        "    return -9223372036854775808\r\n"
        "\r\n"
        "def maximum():\r\n"
        "    return 9223372036854775807\r\n"
        "\r\n"
        "def negative_zero():\r\n"
        "    return -0\r\n"
        "\r\n"
        "def label():\r\n"
        '    return "完成"\r\n'
    ).encode()
    path, facts = _collect(tmp_path, source)

    expected = {
        "nothing": ("none", None, "None"),
        "truth": ("bool", True, "True"),
        "one": ("int", 1, "1"),
        "negative": ("int", -7, "-7"),
        "minimum": ("int", -(2**63), "-9223372036854775808"),
        "maximum": ("int", 2**63 - 1, "9223372036854775807"),
        "negative_zero": ("int", 0, "-0"),
        "label": ("str", "完成", '"完成"'),
    }
    raw = path.read_bytes()
    for name, (scalar_type, value, exact_text) in expected.items():
        semantic_facts = facts[name].semantic_facts
        assert len(semantic_facts) == 1
        semantic = semantic_facts[0]
        assert semantic.predicate == "return_literal"
        assert semantic.component_id == "return.literal"
        assert semantic.normalized_value.type == scalar_type
        assert semantic.normalized_value.value == value
        assert semantic.anchor.exact_text == exact_text
        assert (
            raw[semantic.anchor.start_byte : semantic.anchor.end_byte].decode("utf-8") == exact_text
        )


def test_bool_and_int_constants_remain_distinct_semantic_values(tmp_path: Path) -> None:
    _, facts = _collect(
        tmp_path,
        """\
def truth():
    return True

def one():
    return 1
""",
    )

    truth = facts["truth"].semantic_facts[0].normalized_value
    one = facts["one"].semantic_facts[0].normalized_value
    assert truth != one
    assert truth.model_dump(mode="json") == {"type": "bool", "value": True}
    assert one.model_dump(mode="json") == {"type": "int", "value": 1}


def test_negative_zero_canonicalizes_without_losing_its_source_anchor(
    tmp_path: Path,
) -> None:
    _, facts = _collect(
        tmp_path,
        "def negative_zero():\n    return -0\n\ndef zero():\n    return 0\n",
    )

    negative_zero = facts["negative_zero"].semantic_facts[0]
    zero = facts["zero"].semantic_facts[0]
    assert negative_zero.normalized_value == zero.normalized_value
    assert negative_zero.normalized_value.model_dump(mode="json") == {
        "type": "int",
        "value": 0,
    }
    assert negative_zero.anchor.exact_text == "-0"
    assert zero.anchor.exact_text == "0"


def test_excludes_async_multistatement_expression_and_unsupported_constants(
    tmp_path: Path,
) -> None:
    _, facts = _collect(
        tmp_path,
        """\
async def asynchronous():
    return True

def multiple():
    marker = True
    return True

def expression():
    return 1 + 1

def name():
    return SENTINEL

def collection():
    return [1]

def floating():
    return 1.5

def bytes_value():
    return b"x"

def positive_overflow():
    return 9223372036854775808

def negative_overflow():
    return -9223372036854775809

def positive_unary():
    return +1

def spaced_negative():
    return - 1

def parenthesized_negative():
    return -(1)

def nested_negative():
    return --1

def negative_bool():
    return -True

def negative_float():
    return -1.5

def composite_negative():
    return -~1

def negative_expression():
    return -(2**63)-1
""",
    )

    assert set(facts) == {
        "asynchronous",
        "bytes_value",
        "collection",
        "expression",
        "floating",
        "multiple",
        "name",
        "negative_bool",
        "negative_expression",
        "negative_float",
        "negative_overflow",
        "nested_negative",
        "parenthesized_negative",
        "positive_overflow",
        "positive_unary",
        "spaced_negative",
        "composite_negative",
    }
    assert all(not fact.semantic_facts for fact in facts.values())


def test_rejects_noncanonical_scalar_constants_without_provider_failure(
    tmp_path: Path,
) -> None:
    huge_hex = "f" * 5_000
    _, facts = _collect(
        tmp_path,
        f'def surrogate():\n    return "\\ud800"\n\ndef huge():\n    return 0x{huge_hex}\n',
    )

    assert set(facts) == {"huge", "surrogate"}
    assert all(not fact.semantic_facts for fact in facts.values())
