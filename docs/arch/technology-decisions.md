# 技术选型与演进

本文件回答两个问题：当前为什么这样实现，以及早期 reference 里的方案后来如何处理。

## 1. 决策原则

技术选型遵循以下顺序：

1. 先保证证据可定位、可复验、可失效，再讨论模型能力；
2. 让确定性工具负责搜索、对齐、span 和验证，让模型只处理无法用规则表达但边界已证明的窄判断；
3. 对个人 Python 仓库选择足够小的基础设施，不提前建设分布式或多语言平台；
4. 自动化权限跟随 truth direction，发现 drift 不自动等于有权改写；
5. 所有外部副作用都必须显式启用、可预算、可审计并能 fail closed；
6. 对照评测和生产 Agent 分离，避免 benchmark oracle、prompt 或 live authorization 泄漏到日常路径。

## 2. 当前技术栈

当前依赖声明见 [pyproject.toml](../../pyproject.toml)。

| 关注点 | 当前选择 | 选择原因 |
| --- | --- | --- |
| 语言/runtime | Python 3.11+ | 与目标仓库、AST、doctest/pytest 生态一致 |
| Agent orchestration | 普通 Python 有界流程（`agent/pipeline.py`） | 五节点、一个条件分支、无环无 checkpointer；曾用 LangGraph `StateGraph`，但它承担的正是这十行控制流，收益不抵一层依赖与间接性 |
| 数据合同 | Pydantic v2 | strict typed domain、public wire、adapter input 和 model structured output 共用可验证合同 |
| Python API 提取 | Griffe + Python AST | Griffe 提供 public symbol/signature 视图；AST 补 source span、docstring guard 和窄语义事实 |
| Markdown 提取 | markdown-it-py + 自有 source mapping | 保留 heading/fence/inline literal 的精确 byte anchor |
| Docstring | 自有 Google-style provider + AST guard | 只实现支持矩阵内的 `Args`/`Returns`，同时证明可写字符串范围 |
| 结构检测 | 自有 deterministic detector | exact-FQN、typed component diff、稳定 fingerprint，零模型调用 |
| Executable oracle | stdlib doctest + pytest | 复用 Python 项目已有可执行事实，命令经过 allowlist 与隔离 |
| Git 范围 | Git CLI argv + `shell=False` | 原生支持 merge-base、rename、index/worktree/untracked，并保持明确进程边界 |
| 持久状态 | SQLite | 个人单仓规模足够，事务、可迁移 schema 和精确索引简单可靠 |
| CLI | Typer | typed Python CLI，易与 Pydantic request 对接 |
| MCP | Python MCP SDK `>=1.28.1,<2` | stdio typed tools，版本边界固定，避免自建协议 |
| 模型抽象 | provider-neutral `ModelClient` | application 不依赖厂商 wire；profile、usage 和 schema 约束可测试 |
| 首个模型 transport | OpenRouter + HTTPX | 显式 HTTPS endpoint、strict structured output、可记录实际 usage/cost |
| 质量门 | pytest + Ruff + strict mypy | 行为、风格、类型三个独立可执行检查 |
| 评测 | 冻结 fixture/oracle + trusted scorer | 可离线重放，真实对照也不依赖 subject 自报指标 |

## 3. 架构决策记录

### ADR-001：单 Agent，不采用 Multi-Agent

**状态：采用。**

当前只有一个 Drift Maintenance Agent。Scope Analyzer、provider、detector、planner、validator、memory 和 adapter 都是工具或边界组件，不是拥有独立目标的 Agent。

原因是问题域已经被 Git scope、Python/Markdown grammar 和 exact-FQN 对齐显著收窄。额外的 Planner/Analyzer/Verifier Agent 会增加消息协议、状态同步、成本和失败组合，却不会让 evidence 更确定。编排本身只负责有界状态流。

### ADR-002：evidence-first，模型不参与搜索和对齐

**状态：采用。**

结构和窄语义 detection 必须先由 parser 产出 typed facts/claims，再以唯一 exact-FQN 对齐。模型不能选择 symbol、路径、span 或 truth direction。

这保留了旧调研中“先对齐，再判断”的核心思想，但放弃了 RAG/fuzzy retrieval 作为自动修复前置条件。歧义进入 unresolved，比猜中一个看似合理的目标更安全。

### ADR-003：Python-first，Griffe + AST 取代首版 tree-sitter

**状态：采用；多语言延后。**

早期方案选择 tree-sitter 以覆盖多语言。当前产品范围明确是 Python，因此 Griffe 的 public API 模型加标准 AST 已经覆盖 signature、docstring、source span 和窄常量返回事实，还能减少跨语言 grammar/normalization 负担。

tree-sitter 不是被永久否定；只有出现真实多语言需求、并为每种语言建立等价的 symbol identity、source mapping、truth policy 和 validator 后才值得引入。

### ADR-004：精确索引和 SQLite，暂不使用 RAG/向量库

**状态：采用。**

当前查找键是 repository/workspace identity、FQN、path、source hash、finding fingerprint 和 detector version。个人仓库规模下，这些键由内存 map 与 SQLite 精确索引即可处理。

Embedding、GraphRAG、Chroma、Qdrant 或模糊全文召回会引入模型版本、召回阈值、索引新鲜度和不可复验匹配问题。通用散文或全库语义搜索若未来进入范围，应作为低置信候选层，不能绕过唯一 evidence gate。

### ADR-005：docs-only 自动写，truth policy 决定权限

**状态：采用。**

`code_derived` 文档可按代码自动修复；`design`/`contract` 产生 approval；未知真值 unresolved。业务 Python AST 永远只读，只有 AST 已证明的 docstring 字符串范围是受限例外。

因此系统不自动创建/提交/合并 PR，也不把“代码当前行为”默认提升为所有文档的真值。

### ADR-006：CAS 局部 patch + workspace transaction

**状态：采用。**

Patch 携带 repo-relative path、byte span、source hash、exact expected text、replacement 和 target kind。写入前检查全部前置条件，使用 workspace OS lock、临时文件、fsync 和 `os.replace`，并保留可选择回滚的 journal。

该选择解决 stale overwrite、并发写、重叠 patch 和“一个 group 失败导致已验证成功被无条件抹掉”的问题。

### ADR-007：验证命令来自配置，不来自模型或文档

**状态：采用。**

只允许定向 doctest/pytest；显式 target、flag 和 repo-local path 均经过编译检查。执行使用 argv、`shell=False`、一次性副本、最小环境、timeout、完整 input manifest 和默认断网 guard。

不采用任意 shell、LLM 选命令、隐式全仓测试或 validator 继承宿主 provider credential。一次性副本是正常项目验证的副作用边界，不代替恶意代码 sandbox。

### ADR-008：模型只做窄语义 repair proposal

**状态：采用。**

Detector 保持零模型；只支持 exact-FQN、单常量 return 和受限 Markdown literal。Repair 才可以调用模型，且使用严格 schema、fast→strong profile、一次 schema-only retry、每 finding 最多两次 patch attempt。

OpenRouter 是 transport 实现，不是领域依赖。`.env` 不自动加载，proxy、redirect、transport auto-retry 和 response healing 被禁用，以保留调用授权、费用和失败归因。

### ADR-009：CLI/MCP/CI 是薄 adapter

**状态：采用。**

三个入口只负责边界 contract、request 映射和输出交付，都调用唯一 `application.run`。MCP server-bound/stdio-only；CI check-only 且只写 worktree 外 artifacts。

不采用 daemon、文件监听、editor/web UI 或 adapter 自有业务逻辑，避免与 Coding Agent 争写和形成多个行为真相源。

### ADR-010：评测平面独立、严格配对、缺失值不补零

**状态：采用。**

Stage 4 复用 12 个 subject-neutral portable case 做 Codex/Drift Agent paired aggregate，另外 6 个依赖私有 fault/golden injection 的 case 只作 control。配对键必须精确匹配 dataset、case manifest、trial、snapshot、task 和 scope。

Scorer 从原始 workspace/stream/public result 计算 neutral finding、changed bytes、abstention 和完整性。未知 telemetry 标为 `not_measured` 或 `accounting_incomplete`，不会伪造成 0、PASS 或精确费用。真实 run 需要单独授权且不自动 retry。

## 4. Reference 方案迁移表

早期材料位于 [docs/reference](../reference/)。以下表格只说明决策演进，不修改历史原文。

| 早期方案/调研项 | 当前结论 | 去向 |
| --- | --- | --- |
| Planner / Code Analyzer / Doc Analyzer / Consistency Judge / Repair / Verifier Multi-Agent | 替换 | 单 Agent + 普通 typed tools；保留职责分层，不保留 Agent 身份 |
| Supervisor/角色消息协议 | 替换 | 五节点有界流程和 Pydantic domain state |
| RAG / GraphRAG / embedding / vector DB | 未采用 | exact FQN/hash/version + SQLite；通用语义检索延后 |
| tree-sitter 多语言 parser | 延后 | Python 首版使用 Griffe + AST |
| SQLite + FTS5 文本索引 | 部分沿用 | 沿用 SQLite；当前主要使用精确 identity/fingerprint，不依赖 FTS5 fuzzy retrieval |
| “parser 不直接调用 LLM” | 沿用 | provider/detector 全部确定性，模型只在 repair proposal 边界 |
| “先对齐、再判断” | 沿用 | unique exact-FQN alignment 是自动处理前置条件 |
| CASCADE | 借鉴，不依赖 | 借鉴 pipeline、生成候选后执行验证；其 Java/Javadoc/Maven 实现不适合 Python 首版 |
| DocPrism | 借鉴概念 | 借鉴 alignment-before-judgment；公开 artifact 不足以复用源码 |
| Redis/PostgreSQL/消息队列 | 未采用 | 个人单仓 SQLite 足够 |
| daemon / 实时文件监听 | 未采用 | 显式 CLI/MCP/CI 调用，减少争写、抖动和生命周期问题 |
| 自动修改业务代码 | 未采用 | docs-only；design/contract 交给 approval |
| executable example 自动改写 | 延后 | 当前只检测并作为 repair validator |
| 通用散文、异常/Raises、NumPy/Sphinx docstring、dynamic/re-export | 延后 | 不满足唯一证据或尚无完整支持矩阵，保守 unresolved |
| 全 18 case 都做 Codex 对照 | 修正 | 12 portable paired + 6 private-stimulus control |

## 5. 明确不在当前范围

- 多语言统一 IR；
- 组织级文档知识图谱和向量搜索；
- Web/editor UI、后台 daemon、自动 PR 发布；
- 任意 prose 的事实推断和 fuzzy symbol repair；
- 由模型生成路径、命令、diff 或业务代码；
- 将 validator 副本描述为恶意代码 OS sandbox；
- 普通 check/repair/MCP/CI 隐式启动 Codex 或外部模型；
- 把 3 个 trial 当成 3 倍独立 benchmark case；
- 用 subject 自报指标或自然语言 fuzzy scorer 计算 headline 结果。

## 6. 靶子项目与评测材料的选型

历史项目选型没有直接变成生产依赖，而是演化为冻结评测材料：

| 项目/材料 | 当前用途 |
| --- | --- |
| Click | 首个真实靶子和结构案例来源之一 |
| HTTPX、Pydantic、Rich | 提供带 commit、license、fixture hash 和 oracle 的历史结构案例 |
| synthetic mutation | 补足难以从真实 commit 单独隔离的 component-level 变化 |
| Typer | 生产 CLI 框架；尚未成为冻结评测 case |
| Requests 的异常语义、FastAPI | 作为困难集/扩展候选，当前未进入支持矩阵 |
| Doc Detective、pydoclint、interrogate | 竞品/基线调研，当前未作为 runtime 或 benchmark dependency |

因此“调研过”不等于“已集成”。当前评测采用 project-authored fixture 与历史 commit interval 双来源，并冻结 provenance、license、hash 和 oracle。

## 7. 主要决策来源

- [当前主设计](../design/2026-07-12-doc-code-drift-agent-design.md)
- [Stage 2 结构加固 Spec](../spec/stage-2-structural-hardening-spec.md)
- [Stage 3 executable/semantic Spec](../spec/stage-3-executable-semantic-spec.md)
- [Stage 4 adapters/evaluation Spec](../spec/stage-4-adapters-evaluation-spec.md)
- [Stage 4 Codex Benchmark 运行设计](../spec/stage-4-codex-benchmark-run-design.md)
- [历史方案与调研索引](../reference/README.md)

`doc-code-drift-research.md` 中部分行业材料来自二手整理，只能用于提出问题，不能作为相应公司的官方实践对外引用。对外陈述优先使用上游代码/commit、官方工具文档、公开论文和本仓库冻结 provenance。
