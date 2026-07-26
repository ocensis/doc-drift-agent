# FR-009 pure single-agent tool ablation

The separate three-arm CodeGraph/GitNexus retrieval experiment is documented in
[GRAPH_RETRIEVAL.md](GRAPH_RETRIEVAL.md). It does not change the SPECIAL-tool
protocol or results below.

This directory contains two separately executable experimental agents. The
experiment isolates the algorithm-specific tool bundle; it does not add a mode
to the product detector and it does not test the asynchronous team architecture.

## Treatment and control

Both agents run one persistent model conversation from task to final `submit`.
They receive byte-identical system prompts and initial tasks, and use the same
model profile, temperature, reasoning effort, frozen repository, and launch
window. Version 3 imposes no harness cap on Agent turns, total model calls,
input tokens, whole-run time, conversation history, request body, tool-result
size, repository read/grep/list/git output, FindingStore size, finding string
length, or finding count. It does not trim conversation history. Elapsed time
and usage are observations only.

| agent | tools visible during generation |
|---|---|
| `default_tools_agent.py` (B/control) | `read_file`, `grep`, `list_dir`, `git_changed_files`, `git_diff`, `git_show`, `submit` |
| `seeded_tools_agent.py` (A/treatment) | every B tool, plus `read_briefing`, `extract_claims`, `record_finding`, `list_findings`, and `worklist` |

The invariant is strict: **A = B + the specialized tools**. A does not receive a
seed in its prompt or initial message. It can obtain seeded information only by
choosing to call a treatment tool. The tool definitions are the only intended
difference between the two conversations.

## No generation-time supervisor

The generation phase has no ground-truth access and no content-aware acceptance
loop:

- no alignment-derived coverage checklist is computed for B or returned after
  submission;
- no quote/coverage accept callback rejects a content-valid submission;
- no repairability/model gate runs before artifacts are saved;
- no ground-truth label, expected document, score, or scorer diagnostic is sent
  back to either conversation.

A structurally invalid `submit` terminates the run immediately with
`submit_schema_invalid`; there are no arbitrary string-length or finding-count
limits. The runner does not coach the Agent or offer a retry.

Each process writes a raw artifact containing the direct submission, any
treatment-side FindingStore entries, terminal status, tool trace, token/call
usage, and timing. Exact timing includes artifact- and run-level
`generation_started_at_ns`, `generation_completed_at_ns`, and `completed_at_ns`,
plus `timing.setup_seconds`, `agent_seconds`, and `total_seconds`. Artifacts are
immutable inputs to a separate local scorer.

Only after all model runs finish may the scorer open the ground-truth file. It
first reads and freezes exactly six raw artifacts, records their SHA-256 hashes
and `st_mtime_ns`, and completes the protocol preflight. Only if preflight
passes does it record `gt_read_started_at_ns`, open ground truth, and compute
recall, extras, quote diagnostics, and aggregate resource metrics. Batch launch
spread and makespan use the runner-recorded nanosecond timestamps; filesystem
mtime minus recorded duration is an explicitly approximate cross-check. The
scorer never makes another model call or modifies a raw artifact.

## Generate raw artifacts

The generation executables intentionally have no `--ground-truth` or resource-
budget options. Run each arm once per process. Use this
template, replacing `AGENT_FILE`, `PAIR_ID`, and `ARTIFACT` from the schedule
below:

```bash
set -a && source .env && set +a
unset LANGFUSE_PUBLIC_KEY LANGFUSE_SECRET_KEY LANGFUSE_BASE_URL

.venv/bin/python evals/field/single_agent/AGENT_FILE \
  --fixture path/to/fixture \
  --repeats 1 --pair-id PAIR_ID \
  --output ARTIFACT
```

The runtime still uses a finite timeout and retry count for each individual
HTTP request, plus the provider's context/output constraints. Those are
transport liveness requirements, not an Agent-level experiment budget; a
successful request always returns control to the same unbounded conversation.

| job | arm | `AGENT_FILE` | `PAIR_ID` | `ARTIFACT` |
|---:|---|---|---|---|
| 1 | A | `seeded_tools_agent.py` | `pair-1` | `/tmp/fr009-v3-seeded-pair-1.json` |
| 2 | B | `default_tools_agent.py` | `pair-1` | `/tmp/fr009-v3-default-pair-1.json` |
| 3 | B | `default_tools_agent.py` | `pair-2` | `/tmp/fr009-v3-default-pair-2.json` |
| 4 | A | `seeded_tools_agent.py` | `pair-2` | `/tmp/fr009-v3-seeded-pair-2.json` |
| 5 | A | `seeded_tools_agent.py` | `pair-3` | `/tmp/fr009-v3-seeded-pair-3.json` |
| 6 | B | `default_tools_agent.py` | `pair-3` | `/tmp/fr009-v3-default-pair-3.json` |

Launch all six jobs concurrently when the provider has sufficient capacity.
Concurrent launch puts A and B in the same provider time window; `PAIR_ID`
still defines the three paired comparisons, not process ordering.
`--repo PATH --baseline BASELINE_REVISION` may be used instead of `--fixture`
for a prepared local target, but that worktree must be clean so HEAD fully
identifies the snapshot. A single completed pair is only a runner smoke test;
component evidence requires all three pairs.

## Score after generation

After every raw artifact has been written and all model conversations have
ended, run the local scorer. The expected scorer interface is:

```bash
.venv/bin/python evals/field/single_agent/score_ablation.py \
  --ground-truth path/to/ground-truth.json \
  --artifact /tmp/fr009-v3-seeded-pair-1.json \
  --artifact /tmp/fr009-v3-default-pair-1.json \
  --artifact /tmp/fr009-v3-default-pair-2.json \
  --artifact /tmp/fr009-v3-seeded-pair-2.json \
  --artifact /tmp/fr009-v3-seeded-pair-3.json \
  --artifact /tmp/fr009-v3-default-pair-3.json \
  --output /tmp/fr009-v3-single-ablation.score.json
```

`--artifact` is repeatable. The report should preserve per-run raw submission
scores as well as side-level mean, variance, union recall, extras, calls, tokens,
cost, and wall time. A useful treatment claim requires a repeatable quality gain
whose resource tradeoff is reported; a tie or regression is evidence against
binding the whole specialized bundle by default, not a verdict on every tool
inside it.

The older `fr009_agent_baseline.py --mode unconstrained` remains an external
agent ceiling. It uses a different model/runtime and is therefore not the
control for this tool ablation.
