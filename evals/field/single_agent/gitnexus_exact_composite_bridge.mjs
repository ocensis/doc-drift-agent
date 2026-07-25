/**
 * One-shot, one-backend GitNexus exact-composite bridge.
 *
 * The bridge keeps one official LocalBackend alive while it runs the complete
 * detect_changes result, deterministic K=1 exact-UID selection, context,
 * bounded upstream impact, and narrowly conditional trace/process enrichment.
 * It does not reimplement any graph query.
 */

import { createHash } from "node:crypto";
import { writeSync } from "node:fs";
import path from "node:path";
import { performance } from "node:perf_hooks";
import { pathToFileURL } from "node:url";

const BRIDGE_VERSION = "gitnexus-official-structured-k1-exact-composite-v1";
const ALLOWED_KINDS = new Set([
  "Function",
  "Method",
  "Class",
  "Interface",
  "Constructor",
]);
const KIND_PRIORITY = {
  Function: 0,
  Class: 1,
  Interface: 2,
  Method: 3,
  Constructor: 4,
};
const TEST_PATH = /(^|\/)(test|tests|__tests__)(\/|$)|\.(test|spec)\./iu;

function fail(message) {
  process.stderr.write(`${message}\n`);
  process.exitCode = 1;
}

function parseArguments(argv) {
  if (argv.length !== 4) {
    throw new Error(
      "usage: bridge <local-backend.js> <resources.js> <repository-path> <baseline-revision>",
    );
  }
  const [backendModule, resourcesModule, repositoryPath, baselineRevision] = argv;
  if (
    !path.isAbsolute(backendModule) ||
    !path.isAbsolute(resourcesModule) ||
    !path.isAbsolute(repositoryPath)
  ) {
    throw new Error("provider modules and repository path must be absolute");
  }
  if (!/^[0-9a-f]{40,64}$/iu.test(baselineRevision)) {
    throw new Error("baseline revision must be a full hexadecimal object id");
  }
  return { backendModule, resourcesModule, repositoryPath, baselineRevision };
}

function stableObjectKeys(value) {
  if (Array.isArray(value)) {
    return value.map((item) => stableObjectKeys(item));
  }
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value)
        .sort()
        .map((key) => [key, stableObjectKeys(value[key])]),
    );
  }
  return value;
}

function renderedResult(value) {
  return typeof value === "string"
    ? value
    : JSON.stringify(stableObjectKeys(value), null, 2);
}

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

function resultSignals(value) {
  const object = value !== null && typeof value === "object" && !Array.isArray(value);
  const partialFieldPresent = object && Object.hasOwn(value, "partial");
  const paginationFieldPresent = object && Object.hasOwn(value, "pagination");
  const error = object
    ? Boolean(value.error)
    : typeof value === "string" && value.trimStart().startsWith("error:");
  return {
    error,
    partial: object && value.partial === true,
    partial_field_present: partialFieldPresent,
    partial_value_valid:
      !partialFieldPresent || typeof value.partial === "boolean",
    pagination_field_present: paginationFieldPresent,
    pagination: paginationFieldPresent ? stableObjectKeys(value.pagination) : null,
    status: object && typeof value.status === "string" ? value.status : null,
    ambiguity_candidates:
      object && Array.isArray(value.candidates) ? value.candidates.length : 0,
  };
}

function detectIntegrity(result) {
  const object = result !== null && typeof result === "object" && !Array.isArray(result);
  const changed = object && Array.isArray(result.changed_symbols)
    ? result.changed_symbols
    : null;
  const affected = object && Array.isArray(result.affected_processes)
    ? result.affected_processes
    : null;
  const summary = object && result.summary !== null && typeof result.summary === "object"
    ? result.summary
    : null;
  const partialPresent = object && Object.hasOwn(result, "partial");
  const partialValid = !partialPresent || typeof result.partial === "boolean";
  const countsMatch =
    changed !== null &&
    affected !== null &&
    Number.isInteger(summary?.changed_count) &&
    Number.isInteger(summary?.affected_count) &&
    summary.changed_count === changed.length &&
    summary.affected_count === affected.length;
  return {
    clean:
      object &&
      !result.error &&
      partialValid &&
      result.partial !== true &&
      countsMatch,
    error: object ? Boolean(result.error) : true,
    partial: object && result.partial === true,
    partial_field_present: partialPresent,
    partial_value_valid: partialValid,
    changed_symbols_count: changed?.length ?? null,
    affected_processes_count: affected?.length ?? null,
    summary_counts_match_arrays: countsMatch,
  };
}

function processKey(processInfo, index) {
  return String(processInfo?.id ?? processInfo?.name ?? `process-${index}`);
}

function selectExactCandidate(detectResult) {
  const integrity = detectIntegrity(detectResult);
  const empty = {
    policy_version: "k1-cross-community-unique-exact-uid-v1",
    max_selected: 1,
    integrity,
    eligible_count: 0,
    rejection_counts: {},
    selected: null,
  };
  if (!integrity.clean) {
    return { ...empty, status: "skipped", reason: "detect_changes_not_clean" };
  }

  const changed = detectResult.changed_symbols;
  const affected = detectResult.affected_processes;
  const uidsByName = new Map();
  for (const symbol of changed) {
    const name = typeof symbol?.name === "string" ? symbol.name : "";
    const uid = typeof symbol?.id === "string" ? symbol.id : "";
    if (!name || !uid) continue;
    if (!uidsByName.has(name)) uidsByName.set(name, new Set());
    uidsByName.get(name).add(uid);
  }

  const flowStats = new Map();
  affected.forEach((processInfo, processIndex) => {
    const key = processKey(processInfo, processIndex);
    const steps = Array.isArray(processInfo?.changed_steps)
      ? processInfo.changed_steps
      : [];
    for (const step of steps) {
      const name = typeof step?.symbol === "string" ? step.symbol : "";
      if (!name) continue;
      if (!flowStats.has(name)) {
        flowStats.set(name, {
          crossProcesses: new Set(),
          allProcesses: new Set(),
          changedStepOccurrences: 0,
        });
      }
      const stats = flowStats.get(name);
      stats.allProcesses.add(key);
      stats.changedStepOccurrences += 1;
      if (processInfo?.process_type === "cross_community") {
        stats.crossProcesses.add(key);
      }
    }
  });

  const rejection = {
    missing_identity: 0,
    unsupported_uid_kind: 0,
    test_path: 0,
    non_unique_name: 0,
    absent_from_affected_processes: 0,
    duplicate_uid: 0,
  };
  const seenUids = new Set();
  const candidates = [];
  for (const symbol of changed) {
    const uid = typeof symbol?.id === "string" ? symbol.id : "";
    const name = typeof symbol?.name === "string" ? symbol.name : "";
    const filePath = typeof symbol?.filePath === "string" ? symbol.filePath : "";
    if (!uid || !name || !filePath) {
      rejection.missing_identity += 1;
      continue;
    }
    if (seenUids.has(uid)) {
      rejection.duplicate_uid += 1;
      continue;
    }
    seenUids.add(uid);
    const kind = uid.split(":", 1)[0];
    if (!ALLOWED_KINDS.has(kind)) {
      rejection.unsupported_uid_kind += 1;
      continue;
    }
    if (TEST_PATH.test(filePath)) {
      rejection.test_path += 1;
      continue;
    }
    if (uidsByName.get(name)?.size !== 1) {
      rejection.non_unique_name += 1;
      continue;
    }
    const stats = flowStats.get(name);
    if (!stats || stats.allProcesses.size === 0) {
      rejection.absent_from_affected_processes += 1;
      continue;
    }
    candidates.push({
      uid,
      name,
      kind,
      filePath,
      score: {
        cross_community_processes: stats.crossProcesses.size,
        total_processes: stats.allProcesses.size,
        changed_step_occurrences: stats.changedStepOccurrences,
        kind_priority: KIND_PRIORITY[kind],
      },
    });
  }
  candidates.sort(
    (left, right) =>
      right.score.cross_community_processes - left.score.cross_community_processes ||
      right.score.total_processes - left.score.total_processes ||
      right.score.changed_step_occurrences - left.score.changed_step_occurrences ||
      left.score.kind_priority - right.score.kind_priority ||
      left.filePath.localeCompare(right.filePath) ||
      left.uid.localeCompare(right.uid),
  );
  const selected = candidates[0] ?? null;
  return {
    ...empty,
    status: selected ? "selected" : "skipped",
    reason: selected ? "highest_ranked_eligible_exact_uid" : "no_eligible_candidate",
    eligible_count: candidates.length,
    rejection_counts: rejection,
    selected,
    ordering: [
      "cross_community_processes_desc",
      "total_processes_desc",
      "changed_step_occurrences_desc",
      "kind_priority_asc",
      "filePath_asc",
      "uid_asc",
    ],
  };
}

function tracePlan(impactResult, selected) {
  const signals = resultSignals(impactResult);
  if (signals.error) return { perform: false, reason: "impact_error" };
  if (!signals.partial_value_valid || signals.partial) {
    return { perform: false, reason: "impact_partial_or_invalid" };
  }
  if (signals.pagination_field_present) {
    return { perform: false, reason: "impact_paginated" };
  }
  const byDepth = impactResult?.byDepth;
  if (byDepth === null || typeof byDepth !== "object" || Array.isArray(byDepth)) {
    return { perform: false, reason: "impact_has_no_depth_rows" };
  }
  const depths = Object.keys(byDepth)
    .map((value) => Number(value))
    .filter((value) => Number.isInteger(value) && value > 0)
    .sort((left, right) => left - right);
  if (depths.length < 2 || depths.some((depth, index) => depth !== index + 1)) {
    return { perform: false, reason: "impact_not_a_contiguous_multihop_chain" };
  }
  if (depths.some((depth) => !Array.isArray(byDepth[depth]) || byDepth[depth].length !== 1)) {
    return { perform: false, reason: "impact_chain_branches" };
  }
  const deepest = byDepth[depths.at(-1)][0];
  if (typeof deepest?.id !== "string" || typeof deepest?.name !== "string") {
    return { perform: false, reason: "deepest_impact_node_lacks_exact_uid" };
  }
  return {
    perform: true,
    reason: "single_contiguous_unpaginated_upstream_chain",
    arguments: {
      from: deepest.name,
      from_uid: deepest.id,
      to: selected.name,
      to_uid: selected.uid,
      maxDepth: depths.at(-1) + 1,
      includeTests: false,
    },
  };
}

function processPlan(detectResult, selected) {
  const processes = Array.isArray(detectResult?.affected_processes)
    ? detectResult.affected_processes
    : [];
  const candidates = processes.filter(
    (processInfo) =>
      processInfo?.process_type === "cross_community" &&
      Array.isArray(processInfo.changed_steps) &&
      processInfo.changed_steps.some((step) => step?.symbol === selected.name),
  );
  candidates.sort(
    (left, right) =>
      (right.changed_steps?.length ?? 0) - (left.changed_steps?.length ?? 0) ||
      (right.step_count ?? 0) - (left.step_count ?? 0) ||
      String(left.id ?? left.name).localeCompare(String(right.id ?? right.name)),
  );
  const selectedProcess = candidates[0];
  if (!selectedProcess || typeof selectedProcess.name !== "string") {
    return { perform: false, reason: "no_cross_community_process_for_selected_symbol" };
  }
  return {
    perform: true,
    reason: "highest_ranked_cross_community_process_for_selected_symbol",
    process: {
      id: selectedProcess.id ?? null,
      name: selectedProcess.name,
      process_type: selectedProcess.process_type,
      step_count: selectedProcess.step_count ?? null,
      changed_steps: stableObjectKeys(selectedProcess.changed_steps),
    },
  };
}

async function main() {
  const { backendModule, resourcesModule, repositoryPath, baselineRevision } =
    parseArguments(process.argv.slice(2));
  const backendImport = await import(pathToFileURL(backendModule).href);
  const resourceImport = await import(pathToFileURL(resourcesModule).href);
  if (typeof backendImport.LocalBackend !== "function") {
    throw new Error("pinned GitNexus module does not export LocalBackend");
  }
  if (typeof resourceImport.readResource !== "function") {
    throw new Error("pinned GitNexus module does not export readResource");
  }

  const backend = new backendImport.LocalBackend();
  const providerCalls = [];
  const runtimeBindings = { repo: "isolated_index_clone" };
  const recordedCall = async (operation, argumentsValue, invoke) => {
    const started = performance.now();
    let raw;
    let bridgeException = false;
    try {
      raw = await invoke();
    } catch (error) {
      bridgeException = true;
      raw = {
        error: error instanceof Error ? error.message : String(error),
        bridge_exception: true,
      };
    }
    const seconds = (performance.now() - started) / 1000;
    const result = stableObjectKeys(raw);
    const rendered = renderedResult(result);
    providerCalls.push({
      call_index: providerCalls.length + 1,
      operation,
      arguments: stableObjectKeys(argumentsValue),
      runtime_bindings: runtimeBindings,
      seconds: Number(seconds.toFixed(6)),
      output_chars: rendered.length,
      output_sha256: sha256(rendered),
      bridge_exception: bridgeException,
      ...resultSignals(result),
    });
    return result;
  };

  let envelope;
  try {
    if (!(await backend.init())) {
      throw new Error("isolated GitNexus registry contains no indexed repository");
    }
    const detectArguments = { scope: "compare", base_ref: baselineRevision };
    const detectResult = await recordedCall(
      "detect_changes",
      detectArguments,
      () =>
        backend.callTool("detect_changes", {
          ...detectArguments,
          repo: repositoryPath,
        }),
    );
    const selection = selectExactCandidate(detectResult);
    const enrichment = {
      context: { performed: false, reason: "no_selected_symbol" },
      impact: { performed: false, reason: "no_selected_symbol" },
      trace: { performed: false, reason: "no_selected_symbol" },
      process: { performed: false, reason: "no_selected_symbol" },
    };

    if (selection.selected !== null) {
      const selected = selection.selected;
      const contextArguments = {
        uid: selected.uid,
        name: selected.name,
        include_content: false,
      };
      const contextResult = await recordedCall(
        "context",
        contextArguments,
        () => backend.callTool("context", { ...contextArguments, repo: repositoryPath }),
      );
      enrichment.context = {
        performed: true,
        arguments: contextArguments,
        result: contextResult,
      };

      const impactArguments = {
        target: selected.name,
        target_uid: selected.uid,
        direction: "upstream",
        mode: "callgraph",
        maxDepth: 2,
        includeTests: false,
        limit: 8,
        offset: 0,
        summaryOnly: false,
      };
      const impactResult = await recordedCall(
        "impact",
        impactArguments,
        () => backend.callTool("impact", { ...impactArguments, repo: repositoryPath }),
      );
      enrichment.impact = {
        performed: true,
        arguments: impactArguments,
        result: impactResult,
      };

      const plannedTrace = tracePlan(impactResult, selected);
      if (plannedTrace.perform) {
        const traceResult = await recordedCall(
          "trace",
          plannedTrace.arguments,
          () =>
            backend.callTool("trace", {
              ...plannedTrace.arguments,
              repo: repositoryPath,
            }),
        );
        enrichment.trace = {
          performed: true,
          reason: plannedTrace.reason,
          arguments: plannedTrace.arguments,
          result: traceResult,
        };
      } else {
        enrichment.trace = { performed: false, reason: plannedTrace.reason };
      }

      const plannedProcess = processPlan(detectResult, selected);
      if (plannedProcess.perform) {
        const repositories = await backend.listRepos();
        const matching = repositories.filter(
          (repo) => path.resolve(repo.path) === path.resolve(repositoryPath),
        );
        if (matching.length === 1) {
          const resourceUri = `gitnexus://repo/${encodeURIComponent(matching[0].name)}/process/${encodeURIComponent(plannedProcess.process.name)}`;
          const processContent = await recordedCall(
            "process_resource",
            { process_name: plannedProcess.process.name },
            () => resourceImport.readResource(resourceUri, backend),
          );
          enrichment.process = {
            performed: true,
            reason: plannedProcess.reason,
            selected_process: plannedProcess.process,
            resource: "gitnexus://repo/{runtime_repo}/process/{selected_process}",
            content: processContent,
          };
        } else {
          enrichment.process = {
            performed: false,
            reason: "isolated_registry_repo_binding_not_unique",
          };
        }
      } else {
        enrichment.process = { performed: false, reason: plannedProcess.reason };
      }
    }

    envelope = stableObjectKeys({
      protocol_version: BRIDGE_VERSION,
      normalization: "recursive_object_key_sort_arrays_preserved",
      detect_changes: detectResult,
      selection,
      enrichment,
      provider_calls: providerCalls,
    });
  } finally {
    await backend.dispose();
  }
  writeSync(1, `${JSON.stringify(envelope, null, 2)}\n`);
}

main().catch((error) => {
  fail(error instanceof Error ? error.message : String(error));
});
