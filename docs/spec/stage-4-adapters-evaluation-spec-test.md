# Stage 4 入口与对照评测 – 测试 Spec

**对应技术 Spec**: `docs/spec/stage-4-adapters-evaluation-spec.md`
**Created**: 2026-07-15
**Status**: Complete

## Committed-range Scope

**SC4-001：默认 changed 兼容**

- 旧 `RunRequest` 不传 scope、显式 `{"kind":"changed"}` 与 CLI 不传 `--since` 三者产生与 Stage 3 字节语义相同的 HEAD-to-worktree scope；V1/V2/V3、finding/status 和退出码不变。

**SC4-002：ScopeSpec 严格 tagged contract**

- `changed` 拒绝 `revision`，`since` 必须有唯一非空 `revision`；未知 kind、`--file`、`--symbol`、额外字段、非字符串 revision 均在 Git 调用前被拒绝。

**SC4-003：merge-base 到 worktree 的完整范围**

- 构造 base branch、observed HEAD 上的 committed change、index 中的 staged change、unstaged change、delete、Git rename 和相关 untracked 文件；`scope(kind=since)` 必须以 `merge-base(REV, observed HEAD)` 作 before evidence，以当前 worktree 作 after evidence，一次返回全部预期变化，而 clean checkout 中已提交变更不得假 `clean`。

**SC4-004：REV 偏离、分叉与固定证据**

- 使用非 ancestor REV 创建分叉历史，断言 baseline 是 merge-base 而非 REV tree 或 `REV..HEAD`；解析后移动原 ref 不改变当次 `resolved_revision/base_revision`，但运行中移动 HEAD 使结果 `stale/global_snapshot_changed`。

**SC4-005：非法 revision fail closed**

- 空值、option-like 值由 adapter schema 直接拒绝；不存在 ref、blob/tree object、revision-range 语法、无 common ancestor 或多个 best merge base 均不运行模型/validator、不写 target，并得到 `failed/scope.invalid_revision|scope.no_merge_base`；两类错误都退出 `2`，Git 始终以 argv、`shell=False` 调用。

**SC4-006：config/validation closure 与路径安全**

- committed config-only 变更会扩展到新配置覆盖的证据，configured validation target 仍进入 snapshot；include/exclude、rename/delete、untracked、`.env*` 排除、symlink 拒绝和发布前 snapshot guard 与 `changed` 一致。

**SC4-007：CLI --since 映射**

- `check --since REV` 与 `repair --since REV` 精确构造 typed since scope；不提供时仍是 changed，且没有 CI 环境变量、remote 或默认分支会隐式改变 scope。

## MCP Adapter

**SC4-008：依赖与 stdio-only 启动**

- lock/metadata 将 MCP 限定在 `>=1.28.1,<2`；`drift-agent-mcp --repo PATH [--state-dir PATH]` 只建立 stdio server，无 HTTP/SSE listener、后台 daemon、文件监听或隐式网络，非法 repo/state 在 protocol loop 前失败。

**SC4-009：单仓库绑定与最小 tool schema**

- server 启动后恰好列出 `check_drift`/`repair_drift`；两者必填 typed `scope`，省略 `semantic` 时精确默认为 `false`，对 `repo_path`、`state_dir`、budgets、credential、command、`file` 或 `symbol` 等额外入参返回 schema error，不进入 application。

**SC4-010：check/repair request 映射**

- mock 唯一 application 边界，验证 `check_drift` 映射 `mode=check/semantic_analysis`，`repair_drift` 映射 `mode=repair/apply_policy=docs_only/semantic_repair`，并且两者都使用 server-bound repo/state 与 tool-supplied scope。

**SC4-011：每 tool 恰好一次 application.run**

- clean、finding、unresolved、stale、failed 和 repair 案例都精确断言 `application.run` 调用次数为 1，无 CLI subprocess、无 adapter retry，且 detector/provider/validator/store 没有被 adapter 直接调用。

**SC4-012：PublicBundleV3 payload 等价**

- 对所有 run status、semantic finding、suppression、memory event、repair group 与 residual change 组合，MCP `structuredContent` 必须递归等于 `bundle_to_wire(bundle,3)`，`schema_version` 固定为 3，不允许只返回 JSON string/text summary。

**SC4-013：PublicBundleV3 schema 不泄漏内部字段**

- MCP 声明 output schema 必须来自独立 `PublicBundleV3`，而非 `VerifiedRepairBundle.model_json_schema()`；递归检查 schema properties/$defs 和 runtime payload，确认都无 `semantic_analysis`、runtime、model client、budget ledger、store handle、internal temp path 及任何非 V3 wire 字段，且 recursive extra fields 被拒绝。

**SC4-014：错误语义与 stdio 安全**

- malformed input 是 protocol/schema error；application 产生的 `unresolved|stale|failed` 仍是 typed bundle，不被改写成 MCP success text。stdout 只包含 protocol frame，stderr 诊断不含 key、prompt、provider output 或无界 traceback/body。

## CI Adapter and Artifacts

**SC4-015：check-only 与必填 committed range**

- `drift-agent ci check` 缺少 `--since`、`--state-dir` 或 `--artifacts-dir` 时在 application 前失败；合法调用恰好一次传入 `mode=check/scope=since`，CLI 不提供 repair/apply 开关。

**SC4-016：state/artifact 必须在 worktree 外**

- 拒绝 worktree 内目录、通过 symlink 或 `..` 解析回 worktree 的目录、既有普通文件和 symlink component；接受两个外部临时目录，SQLite/cache/temp/artifacts 均未出现在 repo/Git common state。

**SC4-017：四个固定 artifact**

- clean、drift_found、unresolved、stale 和 failed bundle 都原子产生且只产生 `bundle.json`、`results.sarif`、`summary.md`、`pr-comment.md`；模拟中途写入/发布失败时不留下被当作完成产物的混合集，并退出 `2`。

**SC4-018：V3 bundle artifact**

- `bundle.json` 以 UTF-8 解析后与 `bundle_to_wire(bundle,3)` 递归相等，覆盖 Unicode、所有 status、semantic V3 finding 和 additive memory/repair/residual 字段，同时证明没有 internal field。

**SC4-019：SARIF 2.1.0 确定性映射**

- 根据 SARIF 2.1.0 schema 验证 `results.sarif`；每个 active finding 恰好一个稳定 result，rule/message/disposition/reason/fingerprint 与 V3 bundle 一致，doc 是 primary location、code 是 related location，level 符合冻结映射，URI 全为 repo-relative 且无 source text/secret/absolute temp path。

**SC4-020：Markdown 摘要同源且有界**

- `summary.md` 与 `pr-comment.md` 的 status、scope/revision、计数、finding disposition、validation 和 usage 均可回链 V3 bundle；大量 finding/诊断被稳定截断，文本不宣称已上传 artifact 或发布 PR comment。

**SC4-021：CI 退出码矩阵**

- `clean|fixed` 为 0，`drift_found|partial|needs_approval|unresolved` 为 1，`stale|failed` 为 2；对 check 中不应常规出现的 status 仍用同一函数验证，不引入 CI 专用状态。

**SC4-022：零仓库副作用**

- 运行前后精确比较 worktree bytes、untracked set、index、HEAD/refs、Git config 和 Git common state，确认 CI adapter 无任何变化；mock forge/network/process 证明无 comment/upload/annotation、`git add/commit/push`、hook 安装或修复。

## Comparison Harness and Report

**SC4-023：离线 strict observation import**

- valid `ComparisonObservationV1` 可导入；未知字段、非 relative path、非法 digest、重复 id、缺失 completeness、prompt/secret/raw repository payload 和非冻结 case manifest hash 被拒绝，全过程无 Codex/provider/network 调用。

**SC4-024：严格 paired key**

- 只有 dataset/case/manifest/trial/snapshot/task/scope 全部相同的 Codex/Drift Agent observation 被配对；任一 key 不同进入 `incomparable`，重复/冲突 observation fail closed，不改变 paired metric 分母。

**SC4-025：缺失数据不伪造零值**

- 对 enclosing usage、validation 和 safety group 构造 `measured`、`not_measured`、`accounting_incomplete` observations，并在各 nullable metric 上混合 known/unknown 值；报告保留 group 标记和 `null`/已知 subtotal，不补 0、PASS 或伪精确比率。

**SC4-026：质量与效率公式**

- 用小型手算 oracle 验证 TP/FP/FN、precision/recall/F1、abstention、repair@1/@2、validation/regression/safety、per-success calls/tokens/cost、p50/p95、tool calls 和 strong ratio；分层聚合与总计数遵守守恒公式，零分母显式 `not_measured`。

**SC4-027：Memory 固定 not_measured**

- 首版报告无条件将 Memory 章节标为 `not_measured`，原因明示缺少跨运行 suppression/expiry/alias 配对案例，不输出 precision=0/1、stale reuse=0 或 gain=0 等伪结论。

**SC4-028：deterministic report bytes**

- 对同一 observation 集以不同文件/输入顺序重放，`comparison-report.json` 与 `comparison-report.md` 必须分别 byte-identical；输出不含时间、随机 id、绝对 temp path、PID 或 raw provider text。

**SC4-029：不预填胜负**

- 构造 Codex 优于、等于、劣于专用 Agent 三类观测，报告必须如实陈述；样本不足时使用 `not_measured|insufficient_samples`，不将主设计假设当作验收预置结论。

**SC4-030：真实 Codex 需外部授权**

- 默认 CLI/library/tests 不存在隐式 live path，不读取 provider/Codex credential；导入自声明 real observation 时报告将其 provenance 标为未验证外部声明，不声称 harness 已获权或执行。

## Compatibility, Security and Quality Gate

**SC4-031：Stage 1–3 wire/status/security 回归**

- 默认 V1 strict consumer、显式 V2/V3、semantic capability fail-closed、八终态聚合、Memory 失效、truth approval、budget、lock/transaction/rollback、docstring AST guard、required validator、`.env*`/credential 隔离和 stale snapshot 既有测试全部继续通过。

**SC4-032：新输入不扩大命令、网络或写权限**

- 将 Markdown/docstring/tool text/SARIF/PR comment/observation 注入 revision、command、path、network 和 apply-policy 指令，断言它们始终是不可信数据；业务 Python AST 无写入，模型不扩展 scope，命令仍只来自既有 allowlist/config。

**SC4-033：全量门禁**

- 最新全量 pytest、Ruff、strict mypy、`structural-v1` 8/8 和 `stage3-v1` 10/10 全部通过；MCP/CI/comparison 测试离线，Git worktree 除预期实现/测试/文档外无 state、artifact、cache 或其他运行产物。
