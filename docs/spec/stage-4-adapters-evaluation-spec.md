# Feature Specification: Stage 4 入口与对照评测

**Implementation Target**: `main`
**Created**: 2026-07-15
**Status**: Complete
**Input**: 当前主设计“阶段 4：入口与对照实验”，以及 Stage 1–3 已冻结的兼容性、安全性和评测合同。

## Goal

在不复制 Agent Core、不扩大自动写权限、不伪造外部模型结果的前提下，将已完成的文档维护能力接入 Coding Agent 与 CI，并建立可复现的 Codex 对照观测导入和报告边界。

Stage 4 分为四个可独立验收的切片：

1. **Committed-range scope**：在既有 dirty-worktree scope 之外增加显式 `since` scope，使 pre-push/CI 能分析已提交变更。
2. **MCP adapter**：提供绑定单一仓库的 stdio-only MCP server，暴露 `check_drift` 与 `repair_drift`。
3. **CI artifacts**：提供 check-only CI adapter，安全产生固定 V3 JSON、SARIF 和 Markdown artifacts。
4. **Comparison harness/report**：先交付离线 normalized observation import 和确定性报告；任何真实 Codex 执行均需单独的显式外部授权。

## Requirements

### Functional Requirements

#### 范围、架构与兼容性

- **FR-001**: 系统 MUST 仅交付本 Spec 的 committed-range scope、MCP 薄 adapter、check-only CI artifacts 与离线 comparison harness/report；四个切片 MUST 共用现有领域合同和唯一 `application.run` 边界。
- **FR-002**: 系统 MUST 保持 Stage 1–3 的状态、finding disposition、V1/V2/V3 wire、退出码、真值策略、预算、Memory 失效、workspace transaction 和 validation 语义；除本 Spec 明确定义的 additive 输入与 adapter 产物外，旧调用方行为 MUST 不变。
- **FR-003**: MCP、CI 和 comparison 代码 MUST NOT 直接调用 detector、provider、validator、model transport、workspace transaction 或 SQLite repository；它们只能校验自己的公开 DTO、调用 application service，并转换已发布结果。
- **FR-004**: 系统 MUST 继续使用 `clean|drift_found|fixed|partial|needs_approval|unresolved|stale|failed` 且不引入 adapter 专用同义状态；退出码仍固定为 `clean|fixed -> 0`、`drift_found|partial|needs_approval|unresolved -> 1`、`stale|failed -> 2`。
- **FR-005**: Stage 4 MUST NOT 引入多语言、embedding/向量检索、全库语义搜索、daemon、Web UI、自动业务代码修改、自动 PR 创建/提交/合并，也 MUST NOT 将 SARIF 或 PR comment 格式放入 Agent Core。

#### Committed-range scope

- **FR-006**: `ScopeSpec` MUST 成为 extra-forbid 的严格 tagged contract，接受 legacy 空对象或 `{"kind":"changed"}` 作为默认 changed scope，以及 `{"kind":"since","revision":"REV"}`；序列化后的 changed scope 必须规范化为 `{"kind":"changed"}`，不得输出 `revision:null`，且 `changed` 继续是 `RunRequest` 默认值。
- **FR-007**: `since` scope MUST 在运行开始时原子观测当前 `HEAD` 为 `observed_head`，将非空 `revision` 解析为唯一 commit `resolved_revision`，再以 `merge-base --all` 要求恰好一个 best common ancestor 作为 `base_revision`；零个或多个 best base 都必须 fail closed。
- **FR-008**: `since` 的 before/after sides MUST 分别是 `base_revision` 与当前 worktree；change discovery、old fact 与 rename/delete evidence 必须读 `base_revision`，`observed_head` 只用作当前 commit 身份和 snapshot guard。有效范围因此包含 tracked committed、staged、unstaged、delete 与 Git rename 变化，再加相关 untracked 文件；不得只比较 `base_revision..observed_head` 而遗漏 worktree 变化。
- **FR-009**: `since` 的 tracked diff MUST 使用单个已解析 commit id 作 baseline，以 argv 且 `shell=False` 调用 Git；用户输入 MUST NOT 被拼接为 shell、pathspec 或隐式 revision range。
- **FR-010**: `since` MUST 复用现有 include/exclude、source/docs roots、config-only closure、显式 validation target、rename/delete、symlink 拒绝、source hash、Memory 与最终 snapshot 规则；不得建立宽于 `changed` scope 的写权限。
- **FR-011**: 本次运行 MUST 固定 `resolved_revision`、`observed_head` 与 `base_revision`；运行中原始 ref 名称移动不得改写已解析证据，但 `HEAD`、扫描 closure 或输入字节改变仍 MUST 遵守既有 `stale/global_snapshot_changed` 语义。
- **FR-012**: 空/option-like revision MUST 由 CLI/MCP schema 在 application 前拒绝并返回 adapter 的输入错误退出码 `2`；语法有效但无法解析为 commit 的 revision，以及没有唯一 safe merge base 的 revision MUST 在任何模型、validator 或 target write 之前返回 `failed` bundle 与稳定 reason `scope.invalid_revision|scope.no_merge_base`，并对应退出码 `2`。
- **FR-013**: CLI `check` 与 `repair` MUST 增加显式 `--since REV`，且它只映射到 `ScopeSpec(kind="since", revision=REV)`；未提供时 MUST 仍使用 `changed`，不从 CI 环境变量、remote 或默认分支猜测 revision。
- **FR-014**: `--file` 与 `--symbol` 在 Stage 4 明确 defer，CLI、MCP 与 CI schema MUST NOT 假装接受或通过未类型化字段实现这两种 scope。

#### MCP adapter

- **FR-015**: 项目 MUST 使用稳定依赖 `mcp>=1.28.1,<2`；MCP server MUST 只支持 stdio transport，不启动 HTTP/SSE 监听、不自后台化，也不实现常驻文件监听。
- **FR-016**: MCP server 的固定入口 MUST 为 `drift-agent-mcp --repo PATH [--state-dir PATH]`；启动时 MUST 将自身绑定到一个显式 Python Git worktree，完成仓库和 state-dir 安全校验后才进入 MCP loop。
- **FR-017**: server MUST 只暴露 `check_drift` 与 `repair_drift`；两者入参均固定为必填 typed `scope` 与可选 `semantic:boolean=false`，tool schema MUST NOT 接受 `repo_path`、`state_dir`、模型凭据、validation command 或可扩大的预算字段。
- **FR-018**: `check_drift` MUST 构造绑定 repo/state 的 `RunRequest(mode=check, scope=scope, semantic_analysis=semantic)`；`repair_drift` MUST 构造 `RunRequest(mode=repair, scope=scope, apply_policy=docs_only, semantic_repair=semantic)`；未显式设置 `semantic=true` 时 MUST 保持零隐式模型/网络语义。
- **FR-019**: 每个合法 tool invocation MUST 直接且恰好一次调用 `application.run`；adapter MUST NOT 通过 CLI subprocess 调用 core，MUST NOT 重试完整 run，也 MUST NOT 在 tool 外再运行 detector、validator 或 repair。
- **FR-020**: MCP 输出 MUST 使用独立、extra-forbid 的 `PublicBundleV3` DTO，其公开字段、枚举值和 payload 值 MUST 与 `bundle_to_wire(bundle, 3)` 递归等同；MUST NOT 将 `VerifiedRepairBundle.model_json_schema()` 或其他领域模型 validation schema 直接注册为 MCP output schema。
- **FR-021**: `PublicBundleV3` 顶层 MUST 只含 `schema_version=3,status,run_id,snapshot,scope,findings,changes,validation,approval_required,usage,repository_id,workspace_id,suppressed_findings,memory_events,repair_groups,residual_changes`；DTO schema 与实际 payload MUST 都不含 `semantic_analysis`、runtime、model client、budget ledger、SQLite handle、absolute internal temp path 或任何未经 V3 serializer 发布的内部字段。
- **FR-022**: MCP tool MUST 将 `PublicBundleV3` 对象作为声明 output schema 的 `structuredContent` 返回，不得只返回 JSON string 或自然语言摘要；SDK 为兼容客户端生成的 text content MAY 是同一 structured result 的 JSON 镜像，但不得混入额外诊断、provider 原文或与 structured result 不一致的结论。
- **FR-023**: malformed tool input MUST 由 MCP schema 在调用 application 前拒绝；已进入 application 的业务、工具或环境失败 MUST 保留为结构化 bundle status/evidence，adapter 不得用成功文本掩盖 `unresolved|stale|failed`。
- **FR-024**: stdio protocol data MUST 只写 stdout，诊断信息只能写 stderr 且必须遵守 Stage 3 凭据、prompt、provider output 和错误脱敏合同。

#### CI check 与 artifacts

- **FR-025**: 系统 MUST 提供平台无关的 `drift-agent ci check` 薄 adapter，固定构造 `RunRequest(mode=check, scope=since)` 并恰好一次调用 `application.run`；CI adapter MUST NOT 暴露 repair mode 或调用 `repair_drift`。
- **FR-026**: CI 入参 MUST 显式提供 `--repo PATH --since REV --state-dir PATH --artifacts-dir PATH`；`--since` 不得省略，不得从 GitHub/GitLab 环境变量、remote tracking branch 或网络自动推断。
- **FR-027**: CI `state_dir` 与 `artifacts_dir` 在词法路径和解析路径上 MUST 都位于绑定 worktree 之外；任一 symlink component、`..` 逃逸、既有非目录路径或解析后落入 worktree MUST 在 application run 前 fail closed。
- **FR-028**: CI run 不得修改目标仓库中的 tracked/untracked 文件、Git index、refs、config 或 Git administrative state；run state 只能写入显式外部 `state_dir`，artifact 只能写入显式外部 `artifacts_dir`。
- **FR-029**: 只要 application 产生可序列化 bundle，CI adapter MUST 在 `artifacts_dir` 原子生成四个固定文件：`bundle.json`、`results.sarif`、`summary.md` 和 `pr-comment.md`；不得根据 run status 省略失败产物。
- **FR-030**: `bundle.json` MUST 是 UTF-8 V3 wire object，解析后与 `bundle_to_wire(bundle, 3)` 递归等同，且不得包含 MCP/CI 内部字段。
- **FR-031**: `results.sarif` MUST 是 SARIF `2.1.0` object，每个 active finding 恰好对应一个 deterministic result；`ruleId`、message、disposition/reason、doc primary location、code related location 与 stable fingerprint MUST 来自 V3 bundle，路径 MUST 为 repo-relative URI，不得夹带 source contents、凭据或临时绝对路径。
- **FR-032**: SARIF level 固定为 `unresolved|needs_approval -> error`、`detected -> warning`、`fixed -> note`；SARIF 只是 bundle 的纯转换，不得重新聚合 run status 或改变 CI 退出码。
- **FR-033**: `summary.md` MUST 包含 status、scope/revision、finding/disposition 计数、validation 摘要和 usage/accounting completeness；`pr-comment.md` MUST 是有界、可直接展示的同源摘要，两者不得声称 adapter 已发布评论或上传产物。
- **FR-034**: CI adapter MUST 沿用 FR-004 的 `0/1/2` exit matrix，并在 artifact 成功发布后才返回对应结果；artifact 生成/原子发布失败必须返回 `2`，不得将不完整输出宣称为成功。
- **FR-035**: CI adapter MUST NOT 自动上传 artifact、调用 forge API、发布 PR comment、写 GitHub/GitLab annotation、执行 `git add/commit/push`、安装 hook 或修改目标文档；这些动作由外部 workflow 显式决定。
- **FR-036**: 仓库 MAY 提供 pre-push 和 CI workflow 示例，但示例 MUST 显式传入 revision 和 worktree 外目录、调用同一 `drift-agent ci` 合同，且不得以平台特定默认值替代 core 语义。

#### Comparison observation import 与报告

- **FR-037**: Stage 4 comparison harness MUST 仅导入离线、extra-forbid 的 `ComparisonObservationV1` JSON 并产生 deterministic report；默认实现和自动化测试 MUST NOT 启动 Codex、调用任何外部模型/API 或把 fake observation 标成真实运行。
- **FR-038**: `ComparisonObservationV1` MUST 至少包含 `schema_version=1`、唯一 `observation_id`、`subject=codex|drift_agent`、`dataset_id`、`case_id`、`case_manifest_sha256`、`trial_id`、`snapshot_digest`、`task_digest`、`scope_digest`、normalized outcome、normalized changed-bytes/safety outcome、validation outcome、usage/accounting 及 runner/model provenance；路径必须 repo-relative，不得嵌入 prompt、secret 或整仓库内容。
- **FR-039**: 每个可空指标或其严格 enclosing metric group MUST 显式标记 `measured|not_measured|accounting_incomplete`；group-level incomplete 只能产生单独的 known subtotal/unknown count，不得混入 measured 分母。缺失的 token、cost、tool-call、duration、validation 或 safety 数据 MUST NOT 被填为 `0`、PASS 或从自然语言推断。
- **FR-040**: comparison harness 只能对 `dataset_id + case_id + case_manifest_sha256 + trial_id + snapshot_digest + task_digest + scope_digest` 完全一致的 Codex/Drift Agent observations 建立 paired comparison；不匹配、重复或冲突观测 MUST fail closed 或显式列入 `incomparable`，不得进入聚合分母。
- **FR-041**: report MUST 输出 `comparison-report.json` 与 `comparison-report.md`，并固定按 dataset/case/trial/subject/observation id 排序；相同输入 bytes MUST 产生 byte-identical JSON 和 Markdown，不得写入当前时间、随机 id、绝对路径或未规范化的 provider 文本。
- **FR-042**: 质量报告 MUST 在数据可用时计算 detection TP/FP/FN/precision/recall/F1、abstention correctness、repair success@1/@2、validation pass、regression-free patch rate、业务代码误改次数和 stale/conflict 误覆盖次数，并按 structural/executable/semantic 分层。
- **FR-043**: 效率报告 MUST 在数据可用时计算每成功修复的模型调用、input/output token、known cost、tool calls，p50/p95 wall-clock 以及 strong-profile 比例；不同 accounting completeness 不得无标记混合。
- **FR-044**: 首个 comparison report 的 Memory 章节 MUST 固定输出 `status=not_measured`，并说明当前 paired dataset 不含足以计算误报抑制 precision、过期 memory 误复用和 alias/decision 增益的跨运行样本；不得输出伪造的零值或正向结论。
- **FR-045**: report MUST 把主设计的“质量不低于通用 Codex，并降低上下文/模型/工具/时延”作为待检验假设，而不是预填结论；样本不足、指标缺失或结果不支持假设时 MUST 诚实报告。
- **FR-046**: 任何真实 Codex 执行 MUST 在当次运行前获得显式外部授权，并记录模型/工具/任务/快照/范围/预算来源；本阶段的默认工具不实现隐式 `--live`，导入一份自声明 observation 也不得被解释为已验证授权或真实性。

真实运行的 proposed protocol 单独冻结在
[`stage-4-codex-benchmark-run-design.md`](stage-4-codex-benchmark-run-design.md)。首轮只将现有
18 例中的 12 个 repo-observable structural/executable case 纳入 paired aggregate；5 个依赖专用
Agent 私有 fault injection、1 个注入 frozen golden model answer 的 case 保留为 control regression，
直到存在 subject-neutral fault injector 或显式授权的真实 Drift-model run。该设计不改变
FR-037/FR-046 的默认离线与显式授权边界。

#### Security, determinism 与质量门禁

- **FR-047**: `changed` 和 `since` 的 check/repair MUST 继承 Stage 1–3 的路径词法拒绝、symlink 禁止、snapshot/hash guard、workspace lock、Agent-owned rollback、docstring AST guard、required validation、预算与最终 closure 合同。
- **FR-048**: MCP 与 CI 不得从 Markdown、docstring、tool text、SARIF、PR comment 或 observation 中生成命令、扩张 scope、开启网络或改变 truth/apply policy。
- **FR-049**: 现有 `.env*` 排除、validator 凭据隔离、OpenRouter 脱敏、无隐式 dotenv/代理/重试、有界输出和 `shell=False` 合同 MUST 对新 scope 与 adapter 保持有效。
- **FR-050**: 引入 Stage 4 后，最新全量 pytest、Ruff、strict mypy、`structural-v1` 8/8 与 `stage3-v1` 10/10 MUST 继续通过；新 MCP/CI/comparison 默认测试 MUST 离线。MCP 测试 MAY 使用本地 SDK stdio client，但不得依赖外部 MCP 服务、CI forge 或 Codex/provider 服务。

### Public Data Contracts

#### `ScopeSpec`

```json
{"kind":"changed"}
```

```json
{"kind":"since","revision":"origin/main"}
```

`revision` 是一个必须解析为 commit 的单一 Git revision，不是 `A..B`、pathspec 或 shell fragment。

#### MCP tool input

server 在启动时绑定 repo/state，tool 不再接受路径：

```json
{
  "scope": {"kind":"since","revision":"origin/main"},
  "semantic": false
}
```

`semantic` 默认为 `false`，并按 tool mode 分别映射到 Stage 3 的 `semantic_analysis` 或 `semantic_repair`。

#### `PublicBundleV3`

`PublicBundleV3` 是 MCP/CI 的独立公开 DTO，不是领域模型 schema 的别名。其 payload 与以下投影等价：

```python
PublicBundleV3.model_validate(bundle_to_wire(bundle, 3))
```

DTO 至少应用 recursive `extra="forbid"` 和 `schema_version: Literal[3]`；发布测试必须同时检查 JSON Schema 与 runtime payload，因为领域字段的 serializer exclusion 不等于 validation schema 不会泄漏该字段。

#### `ComparisonObservationV1`

normalized observation 不保存原始 prompt/output，只保存可对齐、计分和审计的投影：

```json
{
  "schema_version": 1,
  "observation_id": "obs_...",
  "subject": "codex",
  "dataset_id": "structural-v1",
  "case_id": "click.parameter-default.v1",
  "case_manifest_sha256": "...",
  "trial_id": "trial-1",
  "snapshot_digest": "sha256:...",
  "task_digest": "sha256:...",
  "scope_digest": "sha256:...",
  "outcome": {},
  "changed_bytes": [],
  "validation": {"status": "measured", "passed": true},
  "safety": {"status": "measured"},
  "usage": {"status": "accounting_incomplete"},
  "provenance": {}
}
```

具体 nested DTO 必须使用已冻结 evaluation matching keys、relative changed-byte hashes 和显式完整性标记；不允许 adapter 从非结构化文本补齐缺失字段。

## Acceptance Boundary

Stage 4 的完成表示四个切片的 deterministic contract、离线测试和现有回归门禁全部通过。它不表示已证明专用 Agent 优于 Codex，也不表示已发布 PR comment、上传 CI artifact 或获得任何外部执行权限。真实 Codex 对照只能在用户显式授权后作为可审计的后续运行；未测量指标必须保持 `not_measured`。
