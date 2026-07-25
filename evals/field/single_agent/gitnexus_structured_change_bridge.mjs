/**
 * One-shot bridge to GitNexus' official LocalBackend structured result.
 *
 * This intentionally imports the pinned package's LocalBackend rather than
 * copying detect_changes or using the human-oriented CLI formatter.  The
 * Python runtime supplies every argument; none is model-controlled.
 */

import { writeSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

function fail(message) {
  process.stderr.write(`${message}\n`);
  process.exitCode = 1;
}

function parseArguments(argv) {
  if (argv.length !== 3) {
    throw new Error(
      "usage: bridge <local-backend.js> <repository-path> <baseline-revision>",
    );
  }
  const [backendModule, repositoryPath, baselineRevision] = argv;
  if (!path.isAbsolute(backendModule) || !path.isAbsolute(repositoryPath)) {
    throw new Error("backend module and repository path must be absolute");
  }
  if (!/^[0-9a-f]{40,64}$/iu.test(baselineRevision)) {
    throw new Error("baseline revision must be a full hexadecimal object id");
  }
  return { backendModule, repositoryPath, baselineRevision };
}

async function main() {
  const { backendModule, repositoryPath, baselineRevision } = parseArguments(
    process.argv.slice(2),
  );
  const imported = await import(pathToFileURL(backendModule).href);
  if (typeof imported.LocalBackend !== "function") {
    throw new Error("pinned GitNexus module does not export LocalBackend");
  }

  const backend = new imported.LocalBackend();
  try {
    if (!(await backend.init())) {
      throw new Error("isolated GitNexus registry contains no indexed repository");
    }
    const result = await backend.callTool("detect_changes", {
      scope: "compare",
      base_ref: baselineRevision,
      repo: repositoryPath,
    });
    // GitNexus/LadybugDB may capture process.stdout.  Match the official direct
    // CLI's fd write so the complete structured result reaches the caller.
    writeSync(1, `${JSON.stringify(result, null, 2)}\n`);
  } finally {
    await backend.dispose();
  }
}

main().catch((error) => {
  fail(error instanceof Error ? error.message : String(error));
});
