import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from drift_agent.config import ProjectConfig
from drift_agent.domain.models import WorkspaceSnapshot
from drift_agent.hashing import sha256_bytes, sha256_file
from drift_agent.languages import is_doc_path, is_source_path
from drift_agent.path_safety import repository_path_without_symlinks
from drift_agent.patterns import matches_any

MISSING_INPUT_HASH = "missing"


def _run_git(repo_path: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_path,
        check=True,
        capture_output=True,
        shell=False,
    )
    return result.stdout


def _nul_paths(output: bytes) -> set[str]:
    return {item.decode() for item in output.split(b"\0") if item}


def _nul_status_path_groups(output: bytes) -> list[tuple[str, ...]]:
    fields = [item.decode() for item in output.split(b"\0") if item]
    groups: list[tuple[str, ...]] = []
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        path_count = 2 if status.startswith("R") else 1 if status == "D" else 0
        if path_count == 0 or index + path_count > len(fields):
            raise ValueError("unexpected git diff --name-status output")
        groups.append(tuple(fields[index : index + path_count]))
        index += path_count
    return groups


@dataclass(frozen=True)
class GitChange:
    status: str
    old_path: str | None
    new_path: str | None

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or ""


class SnapshotChangedError(RuntimeError):
    def __init__(self, message: str, snapshot: WorkspaceSnapshot) -> None:
        super().__init__(message)
        self.snapshot = snapshot


class GitRevisionError(ValueError):
    """Raised when a committed-range baseline cannot be frozen safely."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(f"{reason_code}: {message}")
        self.reason_code = reason_code


class GitScopeResolver:
    def __init__(
        self,
        repo_path: Path,
        config: ProjectConfig,
        *,
        since: str | None = None,
        baseline_revision: str | None = None,
        observed_head_revision: str | None = None,
        head_revision: str | None = None,
    ) -> None:
        self.repo_path = repo_path.resolve()
        self.config = config
        if head_revision is not None:
            if baseline_revision is not None or observed_head_revision is not None:
                raise ValueError("legacy head_revision cannot be combined with frozen revisions")
            baseline_revision = head_revision
            observed_head_revision = head_revision
        if since is not None and (baseline_revision is not None or head_revision is not None):
            raise ValueError("since and a frozen baseline revision are mutually exclusive")
        self._observed_head_revision = observed_head_revision or self._live_head_revision()
        self._resolved_revision: str | None = None
        frozen_baseline = baseline_revision or head_revision
        if since is not None:
            requested_commit = self._resolve_commit(since)
            self._resolved_revision = requested_commit
            frozen_baseline = self._merge_base(
                requested_commit,
                self._observed_head_revision,
            )
        self._baseline_revision = frozen_baseline or self._observed_head_revision

    def _live_head_revision(self) -> str:
        return _run_git(self.repo_path, "rev-parse", "HEAD").decode().strip()

    def _resolve_commit(self, revision: str) -> str:
        try:
            raw = (
                _run_git(
                    self.repo_path,
                    "rev-parse",
                    "--verify",
                    "--end-of-options",
                    f"{revision}^{{commit}}",
                )
                .decode()
                .strip()
            )
        except subprocess.CalledProcessError as error:
            raise GitRevisionError(
                "scope.invalid_revision",
                "since revision is not an available commit",
            ) from error
        if not raw or "\n" in raw:
            raise GitRevisionError(
                "scope.invalid_revision",
                "since revision did not resolve to one commit",
            )
        return raw

    def _merge_base(self, revision: str, head_revision: str) -> str:
        try:
            raw = _run_git(
                self.repo_path,
                "merge-base",
                "--all",
                revision,
                head_revision,
            ).decode()
        except subprocess.CalledProcessError as error:
            raise GitRevisionError(
                "scope.no_merge_base",
                "since revision has no merge base with the observed HEAD",
            ) from error
        merge_bases = tuple(line for line in raw.splitlines() if line)
        if len(merge_bases) != 1:
            raise GitRevisionError(
                "scope.no_merge_base",
                "since revision did not produce exactly one safe merge base",
            )
        return merge_bases[0]

    def _included(self, path: str) -> bool:
        relative = PurePosixPath(path)
        if path == ".env.example":
            # Root-level configuration manifest: always scoped so key
            # additions/removals reach the config-key coverage check even when
            # no globbed source or doc file changed.
            return True
        if is_source_path(path):
            roots = self.config.source_roots
        elif is_doc_path(path):
            roots = self.config.docs_roots
        else:
            return False
        inside_root = any(relative.is_relative_to(PurePosixPath(root)) for root in roots)
        included = matches_any(path, self.config.include)
        excluded = matches_any(path, self.config.exclude)
        return inside_root and included and not excluded

    def changed_paths(self) -> list[str]:
        tracked = _nul_paths(
            _run_git(
                self.repo_path,
                "diff",
                "--name-only",
                "-z",
                self._baseline_revision,
                "--",
            )
        )
        untracked = _nul_paths(
            _run_git(self.repo_path, "ls-files", "--others", "--exclude-standard", "-z")
        )
        return sorted(path for path in tracked | untracked if self._included(path))

    def changes(self) -> list[GitChange]:
        fields = [
            item.decode()
            for item in _run_git(
                self.repo_path,
                "diff",
                "--name-status",
                "-z",
                "--find-renames",
                self._baseline_revision,
                "--",
            ).split(b"\0")
            if item
        ]
        changes: list[GitChange] = []
        index = 0
        while index < len(fields):
            status = fields[index]
            index += 1
            if status.startswith("R"):
                old_path, new_path = fields[index : index + 2]
                index += 2
                if self._included(old_path) or self._included(new_path):
                    changes.append(GitChange(status="R", old_path=old_path, new_path=new_path))
            else:
                path = fields[index]
                index += 1
                if not self._included(path):
                    continue
                changes.append(
                    GitChange(
                        status=status[:1],
                        old_path=path if status[:1] in {"D", "M"} else None,
                        new_path=path if status[:1] != "D" else None,
                    )
                )
        tracked_paths = {
            path
            for change in changes
            for path in (change.old_path, change.new_path)
            if path is not None
        }
        untracked = _nul_paths(
            _run_git(self.repo_path, "ls-files", "--others", "--exclude-standard", "-z")
        )
        for path in sorted(untracked - tracked_paths):
            if self._included(path):
                changes.append(GitChange(status="A", old_path=None, new_path=path))
        return sorted(changes, key=lambda item: (item.path, item.status))

    def head_bytes(self, path: str) -> bytes | None:
        result = subprocess.run(
            ["git", "show", f"{self._baseline_revision}:{path}"],
            cwd=self.repo_path,
            check=False,
            capture_output=True,
            shell=False,
        )
        return result.stdout if result.returncode == 0 else None

    def head_blob(self, path: str) -> str | None:
        result = subprocess.run(
            ["git", "rev-parse", f"{self._baseline_revision}:{path}"],
            cwd=self.repo_path,
            check=False,
            capture_output=True,
            shell=False,
            text=True,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def explicit_renames(self) -> list[tuple[str, str]]:
        return [
            (change.old_path, change.new_path)
            for change in self.changes()
            if change.status == "R" and change.old_path is not None and change.new_path is not None
        ]

    def unsupported_python_changes(self) -> list[str]:
        path_groups = _nul_status_path_groups(
            _run_git(
                self.repo_path,
                "diff",
                "--name-status",
                "-z",
                "--diff-filter=DR",
                "--find-renames",
                self._baseline_revision,
                "--",
            )
        )
        paths: set[str] = set()
        for path_group in path_groups:
            included_python_paths = [
                path for path in path_group if path.endswith(".py") and self._included(path)
            ]
            if included_python_paths:
                paths.add(included_python_paths[-1])
        return sorted(paths)

    def changed_line_ranges(self) -> dict[str, list[tuple[int, int]]]:
        """Head-side line ranges touched by baseline..head, with hunk context.

        Drift attributed to a change should be demonstrable by the code that
        changed. Ranges are taken from the head side of each hunk so callers can
        require counter-evidence to come from there rather than from
        pre-existing code that happens to sit in the same file.
        """

        if self._baseline_revision == self._observed_head_revision:
            return {}
        try:
            output = _run_git(
                self.repo_path,
                "diff",
                "--unified=2",
                "--no-color",
                self._baseline_revision,
                self._observed_head_revision,
            )
        except subprocess.CalledProcessError:
            return {}
        ranges: dict[str, list[tuple[int, int]]] = {}
        current: str | None = None
        for line in output.decode("utf-8", "replace").splitlines():
            if line.startswith("+++ b/"):
                candidate = line[len("+++ b/") :]
                current = candidate if self._included(candidate) else None
            elif line.startswith("+++ "):
                current = None
            elif line.startswith("@@") and current is not None:
                header = line.split("+", 1)
                if len(header) < 2:
                    continue
                span = header[1].split("@@", 1)[0].strip()
                start_text, _, count_text = span.partition(",")
                try:
                    start = int(start_text)
                    count = int(count_text) if count_text else 1
                except ValueError:
                    continue
                if count > 0:
                    ranges.setdefault(current, []).append((start, start + count - 1))
        return ranges

    def head_revision(self) -> str:
        return self._observed_head_revision

    def baseline_revision(self) -> str:
        return self._baseline_revision

    def resolved_revision(self) -> str | None:
        return self._resolved_revision

    def head_is_current(self) -> bool:
        return self._live_head_revision() == self._observed_head_revision

    def snapshot(
        self,
        paths: list[str],
        *,
        expected_hashes: dict[str, str] | None = None,
    ) -> WorkspaceSnapshot:
        head = self.head_revision()
        hashes: dict[str, str] = {}
        for path in sorted(set(paths)):
            input_path = repository_path_without_symlinks(
                self.repo_path,
                path,
                allow_missing=True,
            )
            if not input_path.exists() and not input_path.is_symlink():
                hashes[path] = MISSING_INPUT_HASH
                continue
            if input_path.is_file():
                hashes[path] = sha256_file(input_path)
        hashes = dict(sorted(hashes.items()))
        fingerprint_material = "\n".join(
            [head, *(f"{path}:{digest}" for path, digest in hashes.items())]
        ).encode()
        snapshot = WorkspaceSnapshot(
            head_revision=head,
            workspace_fingerprint=sha256_bytes(fingerprint_material),
            input_file_hashes=hashes,
        )
        if not self.head_is_current():
            raise SnapshotChangedError("HEAD changed during evidence collection", snapshot)
        if expected_hashes is not None:
            mismatches = [
                path for path, expected in expected_hashes.items() if hashes.get(path) != expected
            ]
            if mismatches:
                raise SnapshotChangedError(
                    f"input bytes changed during evidence collection: {sorted(mismatches)}",
                    snapshot,
                )
        return snapshot
