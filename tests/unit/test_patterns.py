from drift_agent.patterns import matches_any, matches_pattern


def test_double_star_matches_zero_or_more_directories() -> None:
    assert matches_pattern("docs/api.md", "docs/**/*.md")
    assert matches_pattern("docs/reference/api.md", "docs/**/*.md")
    assert matches_pattern("generated/api.py", "**/generated/**")
    assert matches_pattern(
        "docs/a/reference/b/api.md",
        "docs/**/reference/**/*.md",
    )
    assert matches_any("src/click_demo/api.py", ["src/**/*.py"])


def test_single_star_never_crosses_a_directory_boundary() -> None:
    assert matches_pattern("docs/api.md", "docs/*.md")
    assert not matches_pattern("docs/contracts/api.md", "docs/*.md")
