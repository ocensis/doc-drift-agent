import stat
from pathlib import Path, PurePosixPath


class UnsafeInputPathError(RuntimeError):
    pass


def repository_path_without_symlinks(
    repo_path: Path,
    relative_path: str,
    *,
    allow_missing: bool = False,
) -> Path:
    relative = PurePosixPath(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise UnsafeInputPathError(f"input path must stay inside the repository: {relative_path}")

    target = repo_path.joinpath(*relative.parts)
    candidate = repo_path
    if not relative.parts:
        return candidate
    for part in relative.parts:
        candidate /= part
        try:
            mode = candidate.lstat().st_mode
        except FileNotFoundError as error:
            if allow_missing:
                return target
            raise UnsafeInputPathError(f"input path is unavailable: {relative_path}") from error
        except OSError as error:
            raise UnsafeInputPathError(f"input path is unavailable: {relative_path}") from error
        if stat.S_ISLNK(mode):
            raise UnsafeInputPathError(f"symlink inputs are not supported: {relative_path}")
    return target
