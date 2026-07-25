# Doc-Code Drift Maintenance Agent

一个面向个人 Python 开发流程的仓库级文档维护 Agent：在代码发生变化后，发现受影响的文档，生成文档修复，并在验证通过后交付可审阅的 patch。

项目采用单 Agent、有界修复循环，CLI、stdio MCP 与只读 CI adapter 共用同一个 Agent Core。Griffe、结构/语义 detector、模型 client 和 validator 都是普通工具，而不是额外 Agent。

## 文档

- [当前架构、技术选型与核心算法](docs/arch/)
- [当前主设计](docs/superpowers/specs/2026-07-12-doc-code-drift-agent-design.md)
- [Stage 2 技术 Spec](docs/spec/stage-2-structural-hardening-spec.md)
- [Stage 2 测试 Spec](docs/spec/stage-2-structural-hardening-spec-test.md)
- [Stage 3 技术 Spec](docs/spec/stage-3-executable-semantic-spec.md)
- [Stage 3 测试 Spec](docs/spec/stage-3-executable-semantic-spec-test.md)
- [Stage 4 技术 Spec](docs/spec/stage-4-adapters-evaluation-spec.md)
- [Stage 4 测试 Spec](docs/spec/stage-4-adapters-evaluation-spec-test.md)
- [Stage 4 Codex Benchmark 运行设计](docs/spec/stage-4-codex-benchmark-run-design.md)
- [历史方案与调研资料](docs/reference/)
- [真实运行反馈](docs/field-reports/)

`docs/reference/` 中的文件只作为决策背景，不代表当前架构。

## 当前状态

Stage 1、Stage 2、Stage 3 与 Stage 4 已完成。当前支持：

- 对 public Python function/method 的参数、默认值、标注、返回值、删除和确定性重命名进行细粒度检查；
- 修复 exact-FQN Markdown signature stub 与受支持的 Google-style `Args`/`Returns` docstring；
- 对多个 finding 分组处理，保留独立成功修复，并对冲突、过期证据和不安全写入保守失败；
- 使用 workspace OS lock、原子事务和回滚保护写入；
- 在 SQLite 中持久化运行记录、人工 decision 与 symbol alias；
- 离线重放 Click、HTTPX、Pydantic、Rich 的 8 个结构评测案例；
- 在 repair group 内部重检通过后，按 allowlist 运行 required doctest/pytest，并在最终快照重跑；
- 在 `check` 中把 configured doctest/pytest 作为全局 required oracle 运行，单显式目标的真实测试失败形成稳定 `broken_example` finding；
- 在显式 opt-in 的 `check` 中，对唯一 exact-FQN 对齐的常量返回值声明形成 V3 `semantic_drift` finding；
- 在显式 opt-in 的 `repair` 中，只把同一份唯一对齐证据交给 strict structured-output 模型边界，并对 code-derived Markdown literal 执行有界语义修复；
- 使用 run-level budget 限制 patch attempt、验证命令和 wall-clock time，并按 provider 实际 usage 记录模型 prompt/completion token 与 cost；
- 提供 provider-neutral `ModelClient`、受约束的 OpenRouter adapter 与显式 `model probe`，用于验证 key、模型和 strict structured-output 通路；
- 用 `--since REV` 检查 merge-base 到当前 worktree 的 committed、staged、unstaged 与相关 untracked 变化；默认 `changed` 语义保持不变；
- 通过绑定单一仓库的 stdio MCP server 暴露 typed `check_drift` 与 `repair_drift`；
- 通过 check-only CI adapter 产生固定 V3 JSON、SARIF 2.1.0 与有界 Markdown 摘要；
- 离线导入专用 Agent/Codex 的规范化 observation 并生成确定性对照报告；默认路径不会启动 Codex 或外部模型。

结构、docstring、executable 和确定性 semantic detection 路径都保持零模型调用；`.env` 的存在不会隐式启用网络。只有显式 `repair --semantic` 且存在唯一、code-derived、可修复的 semantic finding 时才进入模型路径。通用散文理解与全库语义搜索仍不在范围内。

## Quickstart

```bash
uv sync --dev
uv run drift-agent init --repo /path/to/python-repo
uv run drift-agent check --repo /path/to/python-repo --format json
uv run drift-agent check --repo /path/to/python-repo --format json --output-version 2
uv run drift-agent check --repo /path/to/python-repo --since origin/main --format json --output-version 3
uv run drift-agent check --repo /path/to/python-repo --semantic --format json --output-version 3
uv run drift-agent repair --repo /path/to/python-repo --format json --output-version 2
uv run --env-file .env drift-agent repair --repo /path/to/python-repo --semantic --format json --output-version 3
```

目标仓库需要提供 `drift-agent.toml`；`drift-agent init` 可以生成一份带注释的 starter
配置：推断 `source_roots`（含 package 的 `src/` layout，或以仓库根锚定的顶层 package）
与 `docs_roots`，symlink 候选一律跳过，无法推断的布局保守拒绝而不是生成必失败的配置；
`[truth]` 分类留空待人工确认，目标已存在（含 symlink）时拒绝覆盖。配置缺失或无效时，`check`/`repair` 不再
暴露裸异常，而是返回 `status: failed` 加一条 `check="config"` 的 validation receipt，
summary 以稳定 reason code（`config.missing`/`config.invalid`/`config.unreadable`）
开头并包含修复指引。默认 `changed` scope 是该仓库相对 `HEAD` 的
staged、unstaged 与相关 untracked 变化；显式 `--since REV` 先冻结当前 `HEAD`，再以
`merge-base(REV, HEAD)` 为 before side，覆盖此后已提交及当前 worktree 变化。CLI 仍不提供
`--file` 或 `--symbol`。
默认 JSON 输出继续使用 Stage 1 兼容的 V1 schema；显式传入 `--output-version 2` 才输出
Stage 2 additive 字段。`check --semantic` 启用只读语义检测，`repair --semantic` 启用有界
语义修复；两者的 JSON 输出都必须同时显式选择 V3。单独选择 V3 不会隐式启用语义能力。
`check`/`repair --help` 还列出 `--state-dir` 与 `--lock-timeout-seconds`。

### MCP 与 CI

MCP server 在启动时绑定仓库，只使用 stdio transport；tool 输入不能改写 repo/state、预算或验证命令。
启动只要求绑定目录是已有至少一个 commit 的 Git 仓库，不要求 `drift-agent.toml` 存在；无配置仓库上两个 tool 保持可用，
并返回上述 `check="config"` 的结构化指引：

```bash
uv run drift-agent-mcp --repo /path/to/python-repo --state-dir /tmp/drift-mcp-state
```

CI 入口固定为只读 `check`，要求显式 revision，以及两个互相分离、位于 worktree 外的目录：

```bash
uv run drift-agent ci check \
  --repo /path/to/python-repo \
  --since origin/main \
  --state-dir /tmp/drift-ci-state \
  --artifacts-dir /tmp/drift-ci-artifacts
```

成功发布后目录中固定包含 `bundle.json`、`results.sarif`、`summary.md` 和
`pr-comment.md`；adapter 本身不上传产物、不发评论，也不执行 Git 写操作。可直接复用
[GitHub Actions 示例](examples/github-actions/drift-check.yml) 与
[pre-push 示例](examples/hooks/pre-push)。workflow 中的项目来源应替换为已审阅并固定的
release 或 commit。CI 会拒绝任何含 symlink component 的目录；macOS 上手工使用 `/tmp`
时应传物理路径 `/private/tmp/...`，示例 hook 已自动规范化为物理路径。

人工 decision 与 symbol alias 通过以下命令管理：

```bash
uv run drift-agent decision --help
uv run drift-agent alias --help
```

OpenRouter 只通过显式探针或 `repair --semantic` 启用。项目不会自动加载 `.env`；示例配置如下：

```dotenv
OPENROUTER_API_KEY=...
OPENROUTER_MODEL=provider/model
# 可选：分别覆盖 fast/strong profile；未设置时回退到 OPENROUTER_MODEL
OPENROUTER_FAST_MODEL=provider/fast-model
OPENROUTER_STRONG_MODEL=provider/strong-model
# 可选：固定 OpenRouter provider（小写 slug），同时禁用 provider fallback
OPENROUTER_PROVIDER=streamlake
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_TIMEOUT_SECONDS=120
```

执行一次小型、可能产生少量费用的 structured-output 请求：

```bash
uv run --env-file .env drift-agent model probe --profile fast --format json
```

探针还支持 `--profile strong` 和一次性的 `--model provider/model` override。设置
`OPENROUTER_PROVIDER` 时，请求会同时设置 `order` 与 `only` 为该 provider，并关闭 fallback；
未设置时仍由 OpenRouter 选择 provider。它不读取仓库内容，只输出连接状态、实际模型、
request id、token 与 cost。OpenRouter client 固定官方 HTTPS endpoint，禁用系统代理、重定向
和自动重试；普通 `check`、未传 `--semantic` 的 `repair` 与离线评测不会因这些变量存在而调用模型。

Stage 3A 已启用 `[validation].commands`。命令必须显式指向仓库内目标，并且只允许 doctest/pytest：

```toml
[project]
source_roots = ["src"]
docs_roots = ["docs"]
include = ["src/**/*.py", "docs/**/*.md"]
exclude = ["**/generated/**", "**/.venv/**"]

[truth]
code_derived = ["docs/api.md", "docs/api/**"]
design = ["docs/design/**"]
contract = ["docs/contracts/**"]

[validation]
commands = [
  "python -m doctest docs/api.md",
  "python -m pytest tests/test_api.py -q",
]
network = false
```

Agent 把当前仓库复制到不含 `.git`、所有名称以 `.env` 开头的文件/目录（`.env*`）、
已知缓存和 symlink 的一次性工作区，用安全启动器预先加载真实 doctest/pytest，再以
当前 Python 解释器和 `shell=False` 运行。验证进程只得到最小 allowlist 环境，不继承
宿主 token、provider key 或代理变量；HOME、cache、bytecode、临时文件与普通 validator
写入都留在一次性环境。
所有暴露给 validator 的普通文件会形成完整 validation-input manifest，并在启动前与副本
逐项核对。任一 required command 失败、不可用、输入变化或超出预算时，未验证 repair
group 会回滚。该隔离用于约束正常项目测试，不等价于针对恶意代码的 OS/container sandbox。

只要配置了命令，`check` 就会运行这些全局 required oracle，即使结构 changed scope 为空或只改了测试 target。首版 check detector 要求每条命令恰好一个显式 target：PASS 只记录 validation receipt；doctest/pytest exit 1 产生 `broken_example` finding；compile、missing target、其他退出码、timeout 或环境不可用返回 `unresolved`，不会被误报成 drift。该路径不进入 repair transaction，也不写源仓库；人工 decision 可以抑制 legacy finding 展示，但 required FAILED receipt 仍使运行保持 `drift_found`。

当前 semantic detector 刻意保持窄边界：Markdown 必须是 exact-FQN 标题、完整 Python signature fence，以及紧随其后的一行 ``Returns `<literal>`.`` 或 ``Always returns `<literal>`.``；代码必须是同步函数，除可选 docstring 外只有一条常量 `return`。literal 只支持单 token 的 `None`、布尔、非负整数或字符串，以及“无空白的单个 `-` + 整数 token”负数；整数整体必须落在 signed-64 范围，字符串必须可 UTF-8 canonicalize。类型标签用于区分 `True` 与 `1`。唯一对齐后，不一致分别产生 `semantic_direct_mismatch` 或 `semantic_over_promise`；歧义、已识别但不支持的 claim/fact 返回 required `semantic_alignment` unavailable，普通未识别散文不作推断。

语义修复沿用同一窄边界。模型响应必须满足 extra-forbid 的 `SemanticRepairProposal`，只能给出 `decision`、单个 literal `replacement_text`、`confidence` 和有界 `rationale`；path、span、command、diff 和 Python 写入均不由模型决定。首次调用固定使用 fast profile；只有 fast 低置信或第一次 patch 验证失败才升级 strong。每个 finding 最多两次 patch attempt，结构化响应失败最多允许一次 schema-only retry，后者计 model usage 但不计 patch attempt。每次写入仍受 source hash、exact text、trusted literal span、workspace lock 和 `WorkspaceTransaction` 保护，并须通过 semantic 重检、无新增 finding、required executable validation、最终 closure 与发布前 snapshot 检查；失败 attempt 会回滚，第二次失败后保守 abstain。

## 评测重放

离线重放两套冻结评测：

```bash
uv run pytest tests/e2e/test_structural_evaluation.py tests/e2e/test_stage3_evaluation.py
```

Stage 4 另提供纯离线 comparison import/report API。它校验 frozen dataset manifest、完整
paired key、显式 accounting completeness 与 provenance，只聚合精确配对的 observation：

```python
from drift_agent.evaluation import (
    build_stage4_comparison,
    import_stage4_observations,
    stage4_comparison_artifacts,
)

observations = import_stage4_observations(observation_json_payloads)
artifacts = stage4_comparison_artifacts(build_stage4_comparison(observations))
```

`artifacts` 固定含 `comparison-report.json` 与 `comparison-report.md` 的确定性 bytes。该导入
API 本身不会启动 Codex；Codex observation 仍只能标为未验证的外部声明，缺少的 measurement
不会被补成零值。完整 schema 与配对规则见
[Stage 4 技术 Spec](docs/spec/stage-4-adapters-evaluation-spec.md)。

已有 18 个 frozen case 中，首轮真实 Codex 对照只把 12 个 repo-observable structural/executable
case 纳入 paired aggregate；另外 5 个依赖 Drift Agent 私有 timeout/budget/validation-failure
注入，1 个 semantic case 注入 frozen golden model answer，六者继续作为 control regression，
不伪装成公平 head-to-head。`benchmark` CLI 已实现 plan → isolated run → trusted score →
offline report；只有 `run --authorize-live-codex` 会启动真实 Codex，smoke 固定为 12 次 Codex
调用且不自动重试：

```bash
uv run drift-agent benchmark plan \
  --codex-model gpt-5.6-sol \
  --reasoning-effort low \
  --trials 1 \
  --output /Users/Shared/doc-code-drift-stage4-smoke/benchmark-plan.json

uv run drift-agent benchmark run \
  --plan /Users/Shared/doc-code-drift-stage4-smoke/benchmark-plan.json \
  --artifacts-dir /Users/Shared/doc-code-drift-stage4-smoke-artifacts \
  --authorize-live-codex

uv run drift-agent benchmark score \
  --plan /Users/Shared/doc-code-drift-stage4-smoke/benchmark-plan.json \
  --artifacts-dir /Users/Shared/doc-code-drift-stage4-smoke-artifacts

uv run drift-agent benchmark report \
  --plan /Users/Shared/doc-code-drift-stage4-smoke/benchmark-plan.json \
  --artifacts-dir /Users/Shared/doc-code-drift-stage4-smoke-artifacts
```

正式 plan/runtime/artifacts 必须位于非系统-temp 的 0700 私有根；macOS 会给系统 temp 特殊访问，
不能把 `/private/tmp` 当作 secret/oracle 隔离边界。Plan 会冻结 CLI/model/runtime/contract digest。
Run 在第一次模型调用前执行无模型 sandbox sentinel，
并使用每批隔离 auth、最小工具链、不可读的 supervisor/oracle/sibling roots、禁网 child profile、
bounded capture 和 secret fail-stop。完整授权边界和报告限制见
[Codex Benchmark 运行设计](docs/spec/stage-4-codex-benchmark-run-design.md)。

## Stage 2 验证

2026-07-15 的 Stage 2 收口检查结果：

- `uv run pytest`：232 passed；
- `uv run ruff check .`：通过；
- `uv run mypy`：49 个源码文件通过 strict 类型检查；
- `structural-v1`：8/8 离线评测案例通过，模型调用与网络调用均为 0。

## Stage 3 完成状态

Stage 3 于 2026-07-15 收口：

- 新增默认 run budget 和 reserve-before-use 账本；
- 接通 allowlisted doctest/pytest、安全启动器、一次性工作区、timeout 和 network=false socket guard；
- group-local 与最终快照验证均纳入 transaction rollback 和稳定 reason code；
- check-mode executable provider/detector 已接通 doctest 与 targeted pytest，包含全局 required-oracle 触发、单 target evidence、stable finding、不可用/预算语义和完整 validation-input manifest guard；
- 显式 V3 wire 与 mode-specific check/repair semantic capability gate 已冻结；V1/V2 对 semantic run fail closed，纯 legacy V1/V2 保持兼容；
- 确定性常量返回值 claim/fact/alignment/detector 已接通 truth policy、Memory suppression/invalidation 和 snapshot guard，全程只读且模型调用为 0；
- provider-neutral structured `ModelClient`、fast/strong profile contract、OpenRouter strict JSON Schema adapter、实际 usage 回填和显式 connectivity probe 已完成；
- `repair --semantic --output-version 3` 已接入唯一 alignment、strict proposal、fast→strong routing、一次 schema retry、最多两次 patch attempt 和完整 transaction/重检/回滚闭环；
- 所有名称以 `.env` 开头的文件/目录（`.env*`）不进入 validation copy，provider credential 不进入验证子进程，OpenRouter 调用禁用 proxy/redirect/retry 并使用有界响应；
- 最终真实 OpenRouter 探针已连接 `deepseek/deepseek-v4-flash`：24 prompt + 13 completion tokens，cost `$0.0000056`；实现期间在 transport 重构前后共执行两次显式探针，合计 78 tokens / `$0.000010244`，均无自动重试；
- 冻结的离线 `stage3-v1` 10/10 通过：7 个 executable case 保持零模型调用，3 个 semantic opportunity 的 `repair_success@1=1/3`、`repair_success@2=2/3`、abstention correctness `1/1`，fast/strong 调用分别为 `3/5` 与 `2/5`；合计 5 次模型调用、35 input tokens、15 output tokens、5 次 validation command 和 50,000 nano-USD 已知费用；
- 最新全量 pytest、Ruff、strict mypy、`structural-v1` 8/8 与 `stage3-v1` 10/10 quality gate 已通过；默认测试、结构/executable 路径和未 opt-in 的 application 路径模型/网络调用均为 0。
