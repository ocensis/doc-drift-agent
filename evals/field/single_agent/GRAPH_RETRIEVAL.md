# FR-009 code-graph retrieval ablation

This experiment compares two code-graph retrieval components against the same
generic single-Agent control. It is independent of the seeded/SPECIAL ablation
and does not exercise the asynchronous team architecture.

## Three isolated arms

All arms use the byte-identical system prompt, initial task, model profile,
temperature, reasoning effort, frozen baseline/HEAD, terminal `submit`, and
unbounded single-conversation runner.

| Agent file | Visible tools |
|---|---|
| `graph_default_agent.py` | generic `read_file`, `grep`, `list_dir`, git diff/show tools, and `submit` |
| `codegraph_agent.py` | every control tool plus only `codegraph_explore` |
| `gitnexus_agent.py` | every control tool plus `gitnexus_query`, `gitnexus_context`, `gitnexus_impact`, and `gitnexus_trace` |

No arm receives a seed, alignment, briefing, claim extractor, finding store,
coverage gate, supervisor feedback, post-submit repair model, turn limit, model
call limit, cumulative token limit, or whole-run timeout. The GitNexus arm does
not receive `detect_changes`; this round tests current-HEAD code retrieval, not
a second change-analysis algorithm.

## Provider integration

The experiment pins CodeGraph `1.5.0` and GitNexus `1.6.9`. Set exact binary
paths before generation:

```bash
export CODEGRAPH_BIN=/absolute/path/to/codegraph-1.5.0
export GITNEXUS_BIN=/absolute/path/to/gitnexus-1.6.9
export OPENROUTER_PROVIDER=streamlake
```

`OPENROUTER_PROVIDER=streamlake` pins every model request to the StreamLake
endpoint with provider fallbacks disabled. The exact routing object is copied
into each raw artifact. Because provider choice affects latency and potentially
generation behavior, a provider-pinned comparison must pin every arm; it cannot
reuse the earlier unpinned default artifacts as a formal control.

The runtime calls provider CLIs through argv arrays, never a shell. It does not
run either interactive installer, `gitnexus setup`, an MCP server, hooks, skills,
or generated Agent instructions. This keeps the treatment equal to the
declared retrieval tools rather than retrieval plus vendor steering.

Each treatment uses two clones:

1. the Agent-visible clone stays clean and backs every generic tool;
2. a same-HEAD/same-tree shadow clone receives `.codegraph` or `.gitnexus` and
   is reachable only by the provider wrapper.

The wrapper validates the provider version and binary hash, records cold clone
and index time, validates index capabilities, records every query's arguments,
latency, exit code and output size, and removes the shadow clone after the Agent
finishes. Tool output is not truncated by the harness.

CodeGraph runs with telemetry and update checks disabled. Its upstream
`maxFiles` hard range is 1–20; this harness defaults to the provider maximum 20.
The component can return unrelated source for a no-match query and path-only
queries may expand to dependants, so those behaviors are left visible rather
than repaired by the wrapper.

GitNexus runs with an isolated registry home, a four-command read-only
allowlist, embeddings disabled, and LadybugDB extension policy `load-only`.
Setup fails unless `meta.json` reports both graph and FTS capabilities available
and zero embeddings. UID inputs are exposed for ambiguous symbol names. Each
tool call currently starts the pinned CLI process; its startup cost is therefore
part of observed Agent time. A later persistent-MCP implementation may measure
warm-query serving separately, but must retain the same four-tool allowlist and
must not inject MCP server instructions.

## Concurrent generation

After loading the OpenRouter environment and setting the two binary paths, run:

```bash
.venv/bin/python evals/field/single_agent/run_graph_ablation.py \
  --fixture path/to/fixture \
  --output-dir /tmp/fr009-graph-v1
```

The launcher starts all `3 pairs × 3 arms = 9` subprocesses before waiting for
any of them. It explicitly removes Langfuse credentials from child environments,
accepts no ground-truth argument, and applies no process timeout. The output
directory contains nine raw artifacts, per-job stdout/stderr logs, and a launch
manifest.

If the three compatible unbounded default artifacts already exist, reuse them
and launch only the six treatments:

```bash
.venv/bin/python evals/field/single_agent/run_graph_ablation.py \
  --fixture path/to/fixture \
  --output-dir /tmp/fr009-graph-v1 \
  --agents codegraph_agent gitnexus_agent
```

The scorer accepts a reused control only when it is the original
`default_tools_agent` artifact under
`single-agent-tool-ablation-v3-unbounded`. It preserves the source artifact and
hash, labels the control as reused, and fails closed unless target revisions,
prompt hashes, model configuration, requested/actual model, unbounded settings,
and base-tool schema all match the graph treatments. Reuse avoids three model
runs, but the control and treatment generations are no longer simultaneous;
provider-time variance is therefore an explicit experiment limitation. Per-run
Agent latency remains comparable, while a nine-run batch makespan is not.

## Offline scoring

Only after all generation jobs finish, invoke the separate scorer with all nine
artifact paths and the ground-truth path:

```bash
.venv/bin/python evals/field/single_agent/score_graph_ablation.py \
  --ground-truth path/to/ground-truth.json \
  --artifact /tmp/fr009-v3-default-pair-1.json \
  --artifact /tmp/fr009-graph-v1/pair-1-codegraph_agent.json \
  --artifact /tmp/fr009-graph-v1/pair-1-gitnexus_agent.json \
  --artifact /tmp/fr009-v3-default-pair-2.json \
  --artifact /tmp/fr009-graph-v1/pair-2-codegraph_agent.json \
  --artifact /tmp/fr009-graph-v1/pair-2-gitnexus_agent.json \
  --artifact /tmp/fr009-v3-default-pair-3.json \
  --artifact /tmp/fr009-graph-v1/pair-3-codegraph_agent.json \
  --artifact /tmp/fr009-graph-v1/pair-3-gitnexus_agent.json \
  --output /tmp/fr009-graph-v1.score.json
```

The scorer freezes and preflights exactly nine artifacts before its first
ground-truth read. It compares each graph arm separately with the paired control.
`submit_schema_invalid` and `no_tool_call` are observable Agent failures scored
as zero delivered findings; model/provider failures invalidate the batch.

Primary quality metrics are evidence-valid delivered recall, paired hit delta,
union@3, extras and quote diagnostics. Resource reporting separates shadow-clone,
cold-index, setup, Agent, cleanup and total wall time, plus calls, tokens, cost,
query adoption and per-query latency.

## Completed neutral-adoption run (2026-07-23)

The completed run reused the three compatible v3 default artifacts and launched
only the six treatments. The six process starts had a `0.007799 s` spread and a
concurrent makespan of `1846.132674 s`. Raw artifacts, stdout/stderr logs, and the
launch manifest are under `/private/tmp/fr009-graph-v1-run1/`.

### What the Agent was told

The treatment Agents could see the graph tools' function schemas and
descriptions in their tool menus. The byte-identical common prompt also told all
Agents to use the available read-only tools. It did **not** name a graph tool,
require or recommend a graph call, impose a minimum treatment-tool call count,
or add post-submit coaching. This round therefore tested neutral mounting and
voluntary adoption; it was not a forced-use test of graph retrieval capability.

### Formal status and diagnostic override

Formal scoring failed closed before reading ground truth because
`pair-3-gitnexus_agent` ended with `model:request_rejected`. Immediately before
that rejection, a generic `grep` result contained `15,002,398` characters. The
harness preserved the preregistered untruncated-output behavior, and the next
provider request was rejected. Under the registered policy this external
model/provider failure invalidates the formal batch.

After all artifacts were complete, a separate local diagnostic run read the
local ground truth and treated that failed run as zero. Its output is
`/private/tmp/fr009-graph-v1-run1.diagnostic.score.json`. These are exploratory
diagnostics, not a replacement formal score:

| Arm | Evidence-valid hits by pair | Mean | Union@3 | Paired mean delta | Graph-tool adoption |
|---|---:|---:|---:|---:|---:|
| reused default | `3 / 8 / 6` | `5.6667` | `9` | — | n/a |
| CodeGraph | `5 / 1 / 7` | `4.3333` | `7` | `-1.333` | `0/3` runs |
| GitNexus | `6 / 3 / 0` | `3.0000` | `7` | `-2.667` | `0/3` runs |

Neither treatment Agent called any graph tool in any repeat. Every successful
finding came from generic repository and git tools. Consequently, the observed
quality differences cannot be attributed to CodeGraph or GitNexus, and the run
does not show that either graph capability is useless. It shows only that neutral
mounting did not produce adoption under this prompt.

| Arm | Mean setup | Mean index | Index size | Per-run total seconds | Mean cost (USD) |
|---|---:|---:|---:|---|---:|
| reused default | `0` | `0` | `0` | mean `600.398` | `0.5178` |
| CodeGraph | `4.2777 s` | `3.4342 s` | `45,281,509 B` | `1267.961 / 990.618 / 1261.913` | `0.4117` |
| GitNexus | `22.0293 s` | `21.4134 s` | about `196.34 MB` | `1218.751 / 1845.478 / 177.681` (failure) | `0.3731` (includes failure) |

The setup figures measure real cold-index overhead. Do not claim graph-assisted
efficiency from the remaining timing or cost differences: graph adoption was
zero, GitNexus includes an early failure, and reused controls came from a
different, non-concurrent provider window.

## Recommended next experiment

Run a separately preregistered explicit graph-assisted manipulation. Require at
least one graph query before `submit` and record that requirement only as a
manipulation check. Keep all other prompts, generic tools, scoring, and failure
policies unchanged, with no post-submit coaching, coverage gate, or repair pass.
That experiment tests graph capability conditional on use; it must not be merged
with the neutral-adoption result above.
