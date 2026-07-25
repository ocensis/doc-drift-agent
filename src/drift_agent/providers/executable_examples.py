from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from drift_agent.domain.models import WorkspaceSnapshot
from drift_agent.normalization import canonical_json_bytes
from drift_agent.path_safety import UnsafeInputPathError, repository_path_without_symlinks
from drift_agent.scope.git import MISSING_INPUT_HASH
from drift_agent.validation.commands import (
    CommandCompileError,
    CommandKind,
    CompiledValidationCommand,
)

CommandCompiler = Callable[[str], CompiledValidationCommand]


@dataclass(frozen=True, slots=True)
class ExecutableExample:
    """One configured, uniquely anchored executable example probe."""

    id: str
    source_index: int
    command: CompiledValidationCommand
    target: str
    target_hash: str
    config_hash: str

    @property
    def kind(self) -> CommandKind:
        return self.command.kind

    @property
    def component_id(self) -> str:
        return f"{self.kind}:{self.target}"

    @property
    def attempt_id(self) -> str:
        return f"check_{self.id}"

    @property
    def normalized_arguments(self) -> tuple[str, ...]:
        return self.command.argv[3:]


@dataclass(frozen=True, slots=True)
class ExecutableExampleIssue:
    """A configured probe that cannot safely become executable evidence."""

    source_index: int
    source: str
    summary: str

    @property
    def attempt_id(self) -> str:
        return f"check_validation_{self.source_index}"


ExecutableExampleEntry = ExecutableExample | ExecutableExampleIssue


@dataclass(frozen=True, slots=True)
class ExecutableExampleCollection:
    entries: tuple[ExecutableExampleEntry, ...]


class ConfiguredExecutableExampleProvider:
    """Turn allowlisted config commands into command-level check evidence.

    Check-mode detection requires exactly one explicit target per command. The
    runner can execute multiple targets as a repair gate, but an aggregate exit
    code cannot honestly anchor a failed check to one document or test file.
    """

    id = "configured.executable_example"
    version = "1"

    def collect(
        self,
        repo_path: Path,
        sources: Sequence[str],
        *,
        snapshot: WorkspaceSnapshot,
        config_hash: str,
        compiler: CommandCompiler,
    ) -> ExecutableExampleCollection:
        entries: list[ExecutableExampleEntry] = []
        seen: set[tuple[CommandKind, tuple[str, ...]]] = set()
        for index, source in enumerate(sources):
            try:
                command = compiler(source)
            except CommandCompileError as error:
                entries.append(
                    ExecutableExampleIssue(
                        source_index=index,
                        source=source,
                        summary=f"validation_unavailable: {error}",
                    )
                )
                continue

            identity = (command.kind, command.argv[3:])
            if identity in seen:
                continue
            seen.add(identity)
            targets = command.targets
            if len(targets) != 1:
                entries.append(
                    ExecutableExampleIssue(
                        source_index=index,
                        source=source,
                        summary=(
                            "validation_unavailable: check-mode executable detection "
                            "requires exactly one explicit target"
                        ),
                    )
                )
                continue
            target = targets[0]
            try:
                target_path = repository_path_without_symlinks(repo_path, target)
            except UnsafeInputPathError as error:
                entries.append(
                    ExecutableExampleIssue(
                        source_index=index,
                        source=source,
                        summary=f"validation_unavailable: {error}",
                    )
                )
                continue
            target_hash = snapshot.input_file_hashes.get(target)
            if (
                not target_path.is_file()
                or target_hash is None
                or target_hash == MISSING_INPUT_HASH
            ):
                entries.append(
                    ExecutableExampleIssue(
                        source_index=index,
                        source=source,
                        summary=(
                            f"validation_unavailable: validation target is unavailable: {target}"
                        ),
                    )
                )
                continue
            material = {
                "schema": "configured-executable-example-v1",
                "provider": self.id,
                "provider_version": self.version,
                "kind": command.kind,
                "arguments": command.argv[3:],
                "target": target,
            }
            digest = hashlib.sha256(canonical_json_bytes(material)).hexdigest()
            entries.append(
                ExecutableExample(
                    id=f"example_{digest}",
                    source_index=index,
                    command=command,
                    target=target,
                    target_hash=target_hash,
                    config_hash=config_hash,
                )
            )
        return ExecutableExampleCollection(entries=tuple(entries))


__all__ = [
    "ConfiguredExecutableExampleProvider",
    "ExecutableExample",
    "ExecutableExampleCollection",
    "ExecutableExampleEntry",
    "ExecutableExampleIssue",
]
