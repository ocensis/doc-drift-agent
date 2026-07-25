from pathlib import Path

from drift_agent.hashing import sha256_bytes, sha256_file


def test_file_hash_matches_byte_hash(tmp_path: Path) -> None:
    path = tmp_path / "example.md"
    path.write_bytes("你好\n".encode())

    assert sha256_file(path) == sha256_bytes("你好\n".encode())
    assert sha256_file(path).startswith("sha256:")
