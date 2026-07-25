# Repository guidance

## Documentation drift

- After changing `src/`, public behavior, or documentation, run the registered
  `doc_code_drift_agent` MCP tool `check_drift` before handoff.
- Use this default input for current worktree changes:
  `{"scope":{"kind":"changed"},"semantic":false}`.
- `check_drift` is the default and must be used for report-only verification.
- Do not call `repair_drift` unless the user explicitly asks to apply documentation
  repairs. The tool being available is not repair authorization.
- Enable `semantic` only when the user explicitly requests the supported narrow
  constant-return semantic check or repair.
- If the MCP server is unavailable, fall back to
  `.venv/bin/drift-agent check --repo . --format json --output-version 3`.
