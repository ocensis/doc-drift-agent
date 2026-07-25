# Feature Specification: Stage 2 结构路径强化

**Implementation Target**: `main`
**Created**: 2026-07-14
**Completed**: 2026-07-15
**Status**: Implemented and locally verified
**Implementation**: merged into `main` in `7d188bf` and completed by `0cc9674`
**Verification**: 232 pytest tests passed; Ruff passed; strict mypy passed for 49 source files; all 8 `structural-v1` offline cases passed with zero model and network calls
**Input**: 用户要求继续实现当前主设计中的“阶段 2：结构路径做精”，且不混入后续阶段能力。

## Requirements

### Functional Requirements

#### 范围与兼容性

- **FR-001**: 系统 MUST 仅交付阶段 2 的结构路径能力：结构差异识别、确定性文档修复、多 finding 部分成功、写入互斥、持久化人工判断与必要重命名映射，以及可重放的结构评测集。
- **FR-002**: 系统 MUST 保持既有检查模式、修复模式及“兼容性合同”列出的 Stage 1 JSON 字段、类型、默认值与含义；运行状态继续使用 `clean|drift_found|fixed|partial|needs_approval|unresolved|stale|failed`，不得引入同义的 `repaired`；退出码固定为 `clean|fixed -> 0`、`drift_found|partial|needs_approval|unresolved -> 1`、`stale|failed -> 2`。细粒度信息只能作为带安全默认值的 additive 字段；为满足冻结的 V1 literal consumer，本阶段所有 finding 的 legacy `type` 均保持 `"signature_drift"`，具体 family 只由 V2 `kind` 表达。
- **FR-003**: 系统 MUST 将每次运行限制在一个 Python 仓库和一个有界的 Drift Maintenance Agent 工作流内；detector、validator、持久化记忆及评测能力 MUST NOT 表现为独立 Agent。
- **FR-004**: 本阶段的结构检查、对齐、修复、记忆检索和评测 MUST 全程不调用语言模型，且每个结构运行报告的模型调用数 MUST 为 0。
- **FR-005**: 系统 MUST NOT 在本阶段引入可执行示例验证、语义差异检测、模型路由与预算、MCP、CI 适配器、Codex 对照实验、多语言支持、向量检索、daemon 或 Web UI。

#### 结构事实、对齐与 finding

- **FR-006**: 系统 MUST 按本规范“支持矩阵”及“基线与扫描闭包”从受影响的公开 Python function/method 和受支持文档声明中提取可比较的结构事实，并保留足以定位事实来源及判断来源是否变化的证据。
- **FR-007**: 系统 MUST 将参数新增、参数删除、参数顺序变化、参数种类变化、参数标注变化、默认值变化、必填性变化及返回标注变化识别为可区分、可单独处置的结构 finding，而不是只报告一个不透明的整签名差异；比较、粒度、fingerprint 与排序 MUST 遵守“Canonical comparison 与 finding identity”。默认值 presence 变化只发射 `parameter_requiredness_changed`，仅当两侧默认值都存在而 normalized value 不同时才发射 `parameter_default_changed`。
- **FR-008**: 系统 MUST 正确区分“没有默认值”和“默认值为空值”，并正确处理空参数列表、位置参数、仅限位置参数、仅限关键字参数、可变位置参数及可变关键字参数。
- **FR-009**: 系统 MUST 对受支持公开符号名称及其 exact-FQN 文档引用进行确定性检查，并在代码符号已经删除而文档仍保留其声明时报告残留声明 finding；只有 Git 明确报告且存在唯一结构对应的 rename，或仍有效的人工 alias，才可建立 rename alignment。
- **FR-010**: 系统 MUST 检查受支持 Google-style docstring 的 `Args` 与 `Returns` 声明是否与当前代码结构事实一致，并为每类不一致给出可区分的 finding 和证据；异常/`Raises` drift 不属于 Stage 2。
- **FR-011**: 系统 MUST 只在代码事实与文档声明之间存在唯一、高置信、可复验的确定性对应关系时自动修复；重复事实、重复声明、多个候选或仅模糊相似的名称 MUST 阻止自动修复。
- **FR-012**: 系统 MUST 以 `HEAD` 为 before baseline、以原子采样的当前工作树字节为 after snapshot，覆盖 staged、unstaged、untracked、delete 与 Git rename，并按扫描闭包识别删除与重命名；只有 Git 明确 rename 的唯一 old/new symbol 对应或有效人工 alias 可作为 rename 证据，普通 delete+add、名称相似度及模糊推断 MUST NOT 被当作 rename。
- **FR-013**: 系统 MUST 仅允许经人工确认且仍与其来源版本一致的符号 alias 参与重命名对齐；alias 过期、证据不完整或与当前事实冲突时 MUST 不参与自动对齐。
- **FR-014**: 对删除或重命名后的残留文档，系统 MUST 仅在待改文档范围完整、唯一且可验证时自动删除或改名；否则 MUST 保留内容并将 finding 标记为未解决。
- **FR-015**: 系统 MUST 将 code-derived 且满足确定性安全条件的 finding 作为可修复项；design/contract-derived finding MUST 生成明确的人工审批请求；unknown truth finding MUST 保持未解决。
- **FR-016**: 当前代码、文档及 detector 证据 MUST 始终优先于历史运行、人工 decision 或 alias；历史信息不得覆盖当前可复验事实。

#### 安全写入与验证

- **FR-017**: 自动写入 MUST 默认仅限文档；对 Python 文件的唯一允许写入是受支持 public function/method 中能够精确证明为 docstring 字符串的范围，业务可执行结构及 module/class 自身的 docstring MUST 永远只读。
- **FR-018**: 每个候选修复 MUST 绑定待改来源版本、精确范围和预期原文；任一组级前置条件不再成立时 MUST 不覆盖当前内容，并将该组 findings 置为 `unresolved/precondition_changed`，而不是把整个运行伪装为 global stale。
- **FR-019**: docstring 修复 MUST 在写入后证明除该 function/method docstring 外的 Python 抽象语法结构完全不变；无法建立该证明的组 MUST 被回退并标记为 `unresolved/validation_failed`，不得仅因该组问题把运行升级为 `failed`。
- **FR-020**: 每个候选修复 MUST 重检原 finding，并证明其 validation scope 内没有引入任何新的活跃 finding；只有这些断言均通过时，该修复才可记为已应用。本阶段不以未定义的 severity 排序放宽该规则。
- **FR-021**: 系统 MUST 在运行结束前基于最终仓库快照执行整体一致性验证；只有 HEAD、扫描 closure、config hash、effective decision/alias revision、writer generation 或多文件最终证据变化到无法信任整次运行时，运行才返回 `stale`。普通 run/event append 不改变 manual-state revision。state、lock、路径安全或无法恢复的 rollback 等基础设施问题返回 `failed`；普通组级冲突、前置条件或验证问题 MUST 使用 `unresolved` 加稳定 reason code。
- **FR-022**: 回退 MUST 只恢复能够唯一定位并证明为本次运行实际写入的 Agent bytes；不相交的用户或其他进程编辑 MUST 保留。无法唯一证明 inverse 时 MUST 保留当前外部内容、报告 `unresolved/rollback_unavailable`，并将无法保证结果完整性的运行置为 `failed`。
- **FR-023**: 不受支持的 package/symbol/Markdown/docstring 形态、无法精确定位的字符串或不完整删除范围 MUST 保守地返回 `unresolved/unsupported.*` 且不写入；无法可靠建立输入 snapshot 的编码、语法、路径逃逸或符号链接安全错误 MUST 返回 `failed/provider.*` 或 `failed/unsafe_path`。

#### 多 finding 与部分成功

- **FR-024**: 修复模式 MUST 评估本次运行中所有可修复 finding，不得因阶段 1 的单 finding 限制而只处理排序后的第一项。
- **FR-025**: 系统 MUST 按“Repair group 与 ownership”合同建立独立 repair groups，使同一文件中的不重叠修复和跨文件修复都能按确定顺序独立验证、保留或回退。
- **FR-026**: 指向重叠范围、具有不兼容前置条件/replacement 或会彼此改变 alignment/validation read-set 的候选修复 MUST 以稳定 conflict key 被识别为冲突；同一 anchor 且 replacement 完全相同的细粒度 findings MUST 合并为一个 repair group，同一 anchor 但 replacement 不同的候选 MUST 冲突。冲突组 MUST 保守跳过并标记 `unresolved/conflict.*`，不得被报告为已修复；不相关组继续。
- **FR-027**: 某一修复组验证失败、需要审批或无法解决时，系统 MUST 仅回退该组拥有的修改，以 `unresolved/validation_failed` 或相应稳定 reason code 记录，并继续评估不依赖于它的其他组。
- **FR-028**: 运行状态 MUST 遵守“结果代数”：全 fixed 为 `fixed`；至少一个 fixed 且至少一个非 fixed 为 `partial`；没有 fixed 时，`unresolved` 优先于 `needs_approval`；仅 needs-approval 为 `needs_approval`；无 active finding 为 `clean`；只有 global workspace 不可信或基础设施失败可分别覆盖为 `stale` 或 `failed`。
- **FR-029**: 仅当所有已保留修复都通过逐项验证和最终整体一致性验证时，系统才可将它们报告为已应用；失败/冲突 findings 以及被 global `stale`/`failed` 覆盖且未验证的 findings MUST NOT 伪装为 fixed。
- **FR-030**: 在输入、normalized finding key 和人工状态均未变化的条件下重复运行修复，MUST 不产生第二次内容变化，也不得重复制造等价 finding；相同 identity material MUST 产生相同 fingerprint。

#### 写入互斥与并发

- **FR-031**: 检查模式 MUST 对版本控制工作树保持只读；它 MAY 只在 Git common state 目录追加本次运行记录，不得创建、修改或删除 tracked/untracked worktree 文件。
- **FR-032**: 修复模式 MUST 在整个写事务期间持有按 writable workspace identity 隔离的 OS 独占锁；linked worktrees 共享 repository decision/alias state，但各自使用不同 repair lock。
- **FR-033**: 获取锁 MUST 使用 monotonic clock，默认等待上限为 5.0 秒，并允许请求给出非负有限 override；超时返回 `failed/lock_timeout`，且目标工作树与第二个运行开始时一致。
- **FR-034**: OS lock MUST 在正常完成、普通验证失败、异常、可捕获中断以及进程 crash/SIGKILL 后由显式 release 或操作系统回收，使后续运行能够取得；可捕获中断在发布点前还 MUST 回退 owned bytes，SIGKILL/掉电只承诺 lock crash-release 与单文件原子写，不声称内存 rollback 必然完成。
- **FR-035**: 不同 workspace 的修复 MUST 互不阻塞；检查与修复并发观察同一 workspace 时，检查 MUST 在证据收集前后采样 active writer/generation，任一采样有 writer、generation 改变或 evidence hash 改变时只能返回 global `stale`。
- **FR-036**: 锁、owner 与 generation 元数据 MUST 存放在用户 runtime root 的 Agent namespace 中并按 workspace identity 分片，不得污染版本控制工作树或随 `state_dir` override 改变；owner 仅作诊断，OS lock 才是所有权真相。

#### 持久化运行、人工 decision 与 alias

- **FR-037**: 系统 MUST 默认将 schema-versioned SQLite 状态存放于 `<resolved git-common-dir>/drift-agent/state-v1.sqlite3`，使 linked worktrees 共享运行、decision 与 alias；显式 `state_dir` 表示 override 目录，DB 固定为 `<resolved state_dir>/state-v1.sqlite3`，且该目录不得位于版本控制工作树。状态文件不得成为 worktree path；已知旧 schema 只可原子迁移，损坏或更高版本 MUST 安全失败且不得自动删除重建。
- **FR-038**: 持久化记录 MUST 按 versioned repository identity 隔离，并将可复用结论绑定到代码来源版本、文档来源版本、symbol identity、normalized finding fingerprint 及 detector identity/version；workspace-specific lock identity MUST 与 repository identity 分离。
- **FR-039**: 系统 MUST 提供人工 decision 的 `add|list|revoke` typed operation，支持 `ignore|false_positive`，并能在后续运行中解释某条告警因哪一有效 decision 被抑制；只有有效 suppression、没有 active finding 时运行状态 MUST 为 `clean`。
- **FR-040**: 只有人工确认的 ignore 或 false-positive decision MAY 抑制重复告警；Agent 历史成功、失败、排序偏好或未确认建议 MUST NOT 单独抑制当前 finding。
- **FR-041**: 当 decision 绑定的代码版本、文档版本、symbol identity、normalized old/new value、finding kind 或 detector identity/version 任一不再匹配时，该 decision MUST 失效，当前 finding MUST 再次正常报告。
- **FR-042**: 系统 MUST 提供 symbol alias 的 `add|list|revoke` typed operation；alias 仅在 repository identity、Git 可查询且仍为当前历史祖先的 old commit/blob/symbol evidence、当前 new symbol evidence 及 aligner version 全部匹配时参与确定性对齐。Git 历史重写、object 丢失、new evidence 改变或 revoke 均使其失效。
- **FR-043**: 持久化状态不可访问、损坏或无法安全更新时，系统 MUST 返回明确失败；修复运行 MUST 在首个目标写入前证明状态可写，并在中途或最终状态写入失败时于放弃 rollback ownership 前回退本次运行拥有的修改；检查运行的状态写入失败 MUST 返回失败且目标仓库不变。

#### 结构评测集

- **FR-044**: 系统 MUST 提供 `structural-v1` 小型、版本化、可审计、可本地重放的 Click、HTTPX、Pydantic 和 Rich fixtures，并冻结“评测 manifest 与 oracle”中的 case catalog、provenance 与 license。
- **FR-045**: 每个评测案例 MUST 通过 versioned manifest 明确输入前后状态、operation、coverage tags、预期 normalized finding multiset、预期 disposition/reason/status、预期修复字节及 `model_calls=0`/offline 要求，使单案例可直接判定。
- **FR-046**: 评测 coverage union MUST 覆盖参数与默认值、Git/alias rename、符号删除、Google `Args/Returns`、same/cross-file multi finding、partial、conflict 和 conservative rejection。
- **FR-047**: 每个 case MUST 使用独立临时 Git repository/common-state；相同版本和输入的 normalized projection MUST 一致，且不得依赖网络或外部模型服务。run/finding/repository ids、时间、duration、PID、绝对临时路径与 lock generation 不进入确定性 projection。
- **FR-048**: 评测报告 MUST 按冻结公式给出 total、passed、failed、TP、FP、FN、repair_successes、conservative_rejections 与 zero-model/offline compliance，并定位每个失败 case 的 expected/actual normalized key。
- **FR-049**: 每案最多 16 个 fixture files、64 KiB，`structural-v1` 总计最多 64 files、256 KiB；historical copied bytes MUST 记录固定 source URL、commit 与非 `NOASSERTION` SPDX license，project-authored synthetic fixture MUST 声明 copied bytes 为 0，禁止复制完整上游仓库。

### Normative Contracts

#### 支持矩阵

| 维度 | Stage 2 支持 | Stage 2 延后/拒绝 |
|---|---|---|
| Python layout/encoding | 配置 source root 下的 UTF-8 传统 package；每级 package directory 有 `__init__.py` | flat module、namespace package、非 UTF-8、语法错误 |
| Symbol | 直接声明的 public sync/async module function，以及 public class 中直接声明的 sync/async method；FQN 任一 segment 以 `_` 开头即非 public | re-export/alias、`@overload` 集、nested/local/dynamic definition、property/descriptor、class signature |
| Markdown signature | exact-FQN heading 后紧邻一个 top-level `python` fence；fence 仅含一个无 decorator/comment、body 为 `...` 的 `def`/`async def` | 参数表、plain-text/模糊名称、container fence、多个 stub 或其他可执行 body |
| Markdown symbol reference | heading 或 inline-code token 的全部内容恰为 exact FQN | token 子串、相似名称、自然语言推断 |
| Docstring | 受支持 public function/method 首条语句中的单一 AST-proven plain string literal；Google-style `Args:`/`Returns:` 唯一字段及 exact byte anchor；`Raises:` 若存在则忽略且不影响其他受支持字段 | module/class 自身 docstring、NumPy/Sphinx/mixed style、raw/f-string/隐式拼接 literal、重复/模糊的 `Args/Returns` 字段；`Raises` drift 本身不检测 |
| Missing/empty | 空参数集合、缺失 annotation/default/return、空 docstring 均是显式可检测状态 | 空 docstring 或没有唯一字段 anchor 时不得自动生成整份 docstring |

矩阵外但仍可安全隔离到单一 claim/symbol 的输入 MUST 返回 `unresolved/unsupported.package_layout|symbol_kind|markdown_claim|docstring_style|literal` 并保持字节不变。编码/语法使 snapshot 不可读时返回 `failed/provider.encoding|syntax`；路径逃逸或任一 symlink component 返回 `failed/unsafe_path`。

#### Symbol identity 与声明 grammar

- module FQN 从 configured source root 下的 relative path 推导：每级目录必须有 `__init__.py`，`pkg/__init__.py` 对应 `pkg`，`pkg/mod.py` 对应 `pkg.mod`。symbol identity 的 canonical object 为 `{version:"python-symbol-v1",module,owner:null|class-name,name,category:"module_function"|"method"}`；asyncness 不进入 identity。owner class 必须直接声明在 module，method 必须直接声明在该 class；只接受无 decorator、`@staticmethod` 或 `@classmethod` 的声明，其他 decorator、sync↔async transition 与 function↔method transition 为 `unresolved/unsupported.symbol_kind`。
- public 判定对 module、owner 与 symbol 的每个 FQN segment 逐一执行 `not segment.startswith("_")`。同 identity 的多份事实或同一参数名的重复事实均为 ambiguity，不按 import/re-export resolution 合并。
- Markdown signature claim 的 heading 必须是非 list/blockquote 内的 ATX heading，去除 heading marker 与首尾空白后的全部文本恰为 symbol FQN；其下一非空 block 必须是同一 top-level 的 fenced `python` block，且中间没有其他 block。fence 经 Python AST 解析后只能含一个与 FQN 最末 segment 同名的 `def`/`async def`，无 decorator/type comment，body 仅一个 `...` expression。heading 与 fence 的联合范围是可删除的完整 declaration；单独 token 不是可删除 declaration。
- Markdown symbol-reference claim 只接受 heading 全文本或单个 inline-code token 全文本恰为 FQN。rename 可只替换完整 token；delete 只有在该 token 属于上述完整 declaration 或另一个 provider 能证明完整声明范围时才可自动执行。
- Google docstring 先按 universal newline 解析但保留原始 byte offsets。`Args:`/`Returns:` header 必须在 literal content 的同一基准 indentation 上各至多一个。`Args` field 必须是下一缩进层的 `name: description` 或 `name (annotation-expression): description`，continuation 必须更深缩进；name 必须是 Python identifier 且唯一。无 annotation 的形式只声明参数 presence，不主张 `MISSING_ANNOTATION`；有 annotation 时才比较该 expression。Google Args comparison 对 direct instance/class method 排除声明中的第一个 receiver parameter，对 staticmethod 与 module function 不排除；Markdown signature comparison 仍保留全部显式参数。`Returns` 必须恰有一个下一缩进层的 `annotation-expression: description` field。annotation expression 使用本规范 AST normalization；description 为 opaque bytes，修复不得重写。
- docstring 自动写只允许替换已有 name/annotation token 或删除含 continuation 的完整 stale field；不得合成 section、description 或整份空 docstring。缺失 entry/section 仍可报告 finding，但没有完整现有 anchor 时为 `unresolved/unsupported.literal`。`Raises:` section 完全跳过，不进入 claim、fingerprint 或 unsupported 判定。

#### 基线、扫描闭包与 rename

- before 固定为运行开始时的 `HEAD` tree，after 固定为同一原子采样中的当前工作树；index 只发现 staged 状态，不形成第三套 truth。staged-only、unstaged、同一路径 staged+unstaged 使用 HEAD/current，untracked 只有 after，delete 只有 before，Git rename 同时保留 old/new path。
- changed Python 的 before/after public symbol identity 并集反查所有 eligible current claims；changed Markdown/docstring claim 即使代码路径未变化也解析其 exact-FQN current fact；新发现的 fact/claim key 继续扩张，直至 closure 不再增长。删除保留 HEAD fact 作为 old-side evidence。
- Git 明确 rename 只有在 old/new path 中存在唯一一对结构对应的 public symbol 时才建立 rename alignment；有效人工 alias 是另一条允许路径。普通 delete+add、编辑距离、历史成功或模型推断永不构成 rename。

#### Canonical comparison 与 finding identity

- 参数按 exact name 一一匹配；重复同名参数事实或声明为 `unresolved/ambiguity.parameter`。annotation/default/return expression 以 Python `eval` expression AST 的无位置信息结构序列化作为 normalized value；保留 raw text，但不得导入目标包、求值、做 name resolution 或把不同 type spelling 猜成等价。
- `MISSING_PARAMETER`、`MISSING_ANNOTATION`、`MISSING_DEFAULT`、`MISSING_RETURN`、`MISSING_SYMBOL` 与 `MISSING_DOCSTRING_FIELD` 均为带类型哨兵，并与 normalized literal `None` 区分。missing↔present default 只发一个 `parameter_requiredness_changed`，携带 sentinel 与具体 normalized value；present↔present 不同才发 `parameter_default_changed`。
- V2 `old_value` 固定表示当前 document/docstring claim 中待处置的 normalized value，`new_value` 固定表示 current worktree code fact 的 normalized value，不随 truth policy 改变。code 有/doc 无时 old 使用对应 missing sentinel，doc 有/code 无时 new 使用 missing sentinel；symbol rename 使用 old/new FQN。`HEAD` before fact 只作为 delete/rename/evidence discovery，不改变该方向。
- added/removed、kind、annotation、requiredness、default 每参数每 kind 最多一个 finding；共同参数的相对序列不同时每 symbol 发一个 `parameter_order_changed`；return 每 symbol 一个；symbol delete/rename 每完整 claim 一个；docstring parameter 每参数一个、docstring return 每 symbol 一个。
- finding fingerprint 固定为 SHA-256 canonical serialization，材料含 `finding-v1`、repository identity、symbol identity、legacy type、kind、component、normalized old/new values、双侧 evidence 的 relative path/range/hash 及 detector identity/version；不得包含 run id、时间或排序位置。canonical serialization 固定为 UTF-8 JSON、lexicographic key order、无 insignificant whitespace、`ensure_ascii=false`，所有 sentinel/AST/enum 均带显式 type tag；digest 是该 byte string 的 lowercase hex SHA-256。
- findings 固定按 `(symbol identity, kind rank, component, code path/start, doc path/start, detector identity/version, fingerprint)` 排序。kind rank 为 added、removed、order、kind、annotation、requiredness、default、return、symbol deleted、symbol renamed、docstring parameter、docstring return、unsupported。

#### 结果代数与稳定 reason code

Finding disposition 继续只使用 Stage 1 的 `detected|fixed|needs_approval|unresolved`；group-local 问题不得扩展为 `stale` 或 `failed` disposition。稳定 reason code 至少包括 `ambiguity.*`、`unsupported.*`、`precondition_changed`、`conflict.overlap|base|expected_text|replacement|validation_dependency`、`validation_failed`、`validation_new_finding`、`final_validation_failed`、`rollback_unavailable`、`truth_requires_approval`、`unknown_truth`、`global_snapshot_changed`、`state_store.*`、`lock_timeout` 与 `unsafe_path`。

| 条件 | Finding 结果 | Run status |
|---|---|---|
| check snapshot 有效且有 active finding | `detected/<detector reason>` | `drift_found` |
| 只有有效 suppression、无 active finding | 不进入 active findings；保留 suppression audit | `clean` |
| 所有 active finding 最终验证通过 | `fixed/validated` | `fixed` |
| 至少一个 fixed 且至少一个非 fixed | 各 finding 保留真实 disposition/reason | `partial` |
| group precondition/CAS 改变、冲突或普通 validation 失败 | `unresolved/precondition_changed|conflict.*|validation_*`；其他独立组继续 | 无 fixed 时 `unresolved` |
| design/contract truth 且无 unresolved | `needs_approval/truth_requires_approval` | `needs_approval` |
| unknown truth、ambiguity 或 unsupported | `unresolved/<stable reason>` | 无 fixed 时 `unresolved` |
| HEAD、closure、config、effective manual-state revision、writer generation 或最终多文件 evidence 使整次 workspace snapshot 不可信 | 相关非 fixed finding 为 `unresolved/global_snapshot_changed` | `stale` 覆盖普通聚合 |
| state/lock/path 或无法恢复的 ownership failure | 相关非 fixed finding 为 `unresolved/<infrastructure reason>` | `failed` 覆盖普通聚合 |

#### 兼容性合同

- `RunRequest` 既有字段保持：`mode: check|repair` 与 `repo_path: path` 为必填；`scope: {kind:"changed"}` 与 `apply_policy:"docs_only"` 为既有默认。新增 `state_dir: path|null=null` 和 `lock_timeout_seconds:number=5.0` 均有安全默认；未知字段继续拒绝。
- `VerifiedRepairBundle` 既有顶层类型保持：`status:RunStatus`、`run_id:string`、`snapshot:WorkspaceSnapshot`、`scope:list<string>`、`findings:list<DriftFinding>` 必填；`changes:ChangeSet={applied:false,files:[],patch:""}`、`validation:list<ValidationResult>=[]`、`approval_required:list<ApprovalRequest>=[]`、`usage:Usage` 使用既有默认。`snapshot` 继续含必填 `head_revision:string`、`workspace_fingerprint:string`、`input_file_hashes:map<string,string>`。
- legacy finding 继续含必填 `id:string,symbol_id:string,type:"signature_drift",disposition:detected|fixed|needs_approval|unresolved,truth_source:code|human|unknown,code_evidence:EvidenceAnchor,doc_evidence:EvidenceAnchor,reason:string`；所有 Stage 2 structural finding 的 `type` 均固定为 `signature_drift`，signature、symbol、docstring 或 unsupported 的具体 family 只写入 V2 `kind`/`reason_code`，且不得再发一个 opaque whole-signature duplicate。`EvidenceAnchor` 继续含必填 `path:string,line:integer,source_hash:string`。
- validation 继续含 `finding_ids,attempt_id,check,required,status,summary,duration_ms=0`；approval request 继续含 `id,finding_id,kind="truth_direction",reason,input_file_hashes,candidate_diff=null,suggested_validation=[]`；usage 继续含 `model_calls=0,model_calls_by_profile={},tool_calls=0,validation_commands=0,input_tokens=0,output_tokens=0,estimated_cost_usd=0.0,duration_ms=0`。
- additive finding 字段 `kind="signature_changed"`、`component_id=""`、`old_value=null`、`new_value=null`、`detector_id=""`、`detector_version=""`、`fingerprint=""`、`reason_code=""` 以及 bundle 字段 `repository_id=""`、`workspace_id=""`、`suppressed_findings=[]`、`memory_events=[]`、`repair_groups=[]`、`residual_changes=[]` MUST 有上述 model 默认。默认 CLI V1 serializer MUST 无条件省略 `schema_version` 和全部 additive 字段，即使其运行时值非默认，也只能使用 legacy 字段表达结果；显式 `--output-version 2` 才输出 `schema_version=2` 和全部 additive 字段/默认值。

#### 人工操作、identity、SQLite 与 lock

- Typed operations 固定为 `decision add|list|revoke` 与 `alias add|list|revoke`。add 输入 MUST 引用 repository、已完成 run id、finding/alignment id、action/target、非空 reason、actor 与显式 confirmation；服务从 run record 推导 evidence hashes。相同完整 validity key 的重复 add 返回既有 id；冲突 active record 必须先 revoke；revoke 追加 tombstone 且重复 revoke 为成功 no-op。
- current facts/claims 与 ambiguity 优先于有效 decision/alias，有效 decision/alias 优先于历史 run/event。suppression audit 至少含 decision id、action、reason、actor/confirmation、evidence key、created/revoked time。alias 保存 old commit/blob/symbol evidence；只有当前 HEAD 仍 descendant of confirmation point、old object 可查询、old claim/new evidence/aligner version 匹配且未 revoke 时有效。
- `repository identity v1 = SHA-256("repo-v1" + real Git common-dir path + initial root commit)`；`workspace identity v1 = SHA-256("workspace-v1" + repository identity + real worktree root)`。linked worktrees 共享 repository identity、SQLite decisions/aliases，但 workspace identity 与 repair lock 不同；independent clone、move、copy、re-init 均不共享 memory 或 lock。持久化 full material 与 digest；digest 相同而 material 不同返回 `failed/repository_identity_collision`。
- 默认 SQLite 路径为 `<resolved git-common-dir>/drift-agent/state-v1.sqlite3`；显式 `state_dir` 表示目录且 DB 固定为 `<resolved state_dir>/state-v1.sqlite3`，override 优先，但目录不得解析到 version-controlled worktree 内，已有非目录路径必须 `failed/state_store.path`，且 lock root/key 不随其改变。Stage 2 initial schema 固定 `PRAGMA user_version=1`、`foreign_keys=ON` 与 WAL；known migration 必须在单一 transaction 中原子有序，newer/corrupt store 不得自动 reset；事件 `(run_id,seq)` 唯一且 seq 连续。
- 成功 check 的 required event 顺序为 `run_started,snapshot_captured,facts_collected,findings_detected,decisions_applied,run_finished`。repair 在 decisions 后依次增加 `repair_planned,lock_acquired`，每组一个 `group_started` 及恰好一个 `group_retained|group_rolled_back|group_skipped`，然后 `final_validation_completed,run_finished`。首个 target write 前必须成功提交初始 run/event probe；任一 required write failure 在 target write 前返回 failed，已有 edits 时必须在放弃 journal/lock 前 rollback；final `run_finished` 是 publication prerequisite。
- repair lock root 固定为 `platformdirs.user_runtime_path("drift-agent")/locks-v1`，key 为 workspace identity，默认 monotonic timeout 为 5.0 秒。owner 至少含 schema version、repository/workspace identity、run id、PID、hostname、process-start token、generation、acquired time；owner 仅诊断，OS advisory lock 是唯一所有权真相。check 的 active-writer sample MUST 用不改 owner/generation 的 nonblocking OS-lock probe，不能把 stale sidecar 当作 holder；可获取 repair OS lock 时 stale owner 被覆盖且 generation 递增。SIGKILL/crash 释放 OS lock但不承诺内存 rollback，下一运行不得把中断残留伪报为 fixed。

#### Repair group 与 ownership

- group id 由 sorted finding fingerprints 与 replacement-anchor key 确定；共享同一 anchor/replacement 的 findings coalesce。稳定执行顺序为 `(relative path,start,end,group id)`，每个通过组形成独立 savepoint。
- conflict key 固定为 `overlap`、`base`、`expected_text`、`replacement`、`validation_dependency`；同一插入点归入 `overlap`。冲突组统一 skipped/unresolved，独立组继续。
- 每组依次完成 CAS apply、重新提取、原 finding 消失、validation scope 无任意新 finding、docstring AST guard（如适用）。最终重检从 finding delta/read-write dependency 得到 deterministic failing group set，一次移除该 set，再验证剩余组直到 fixed point；无法归因的 failure 使所有 implicated groups unresolved，global snapshot 不可信则 run stale。
- inverse rollback 只有在 exact Agent replacement 仍能唯一定位且 external hunks 不触碰该 replacement 时才可执行；不相交外部 edits 必须映射并保留。无法证明时不覆盖 current bytes，`changes.applied` 不得包含 residual edit，V2 `residual_changes` 记录 path/range/hash，finding 为 `unresolved/rollback_unavailable` 且 run failed。

#### 评测 manifest 与 oracle

dataset id 固定为 `structural-v1`。改变任一 case fixture 或 oracle 必须提升 dataset version。最低 catalog 与 case-level oracle 为：

| Case ID | Provenance / license | Operation 与 coverage | Expected oracle |
|---|---|---|---|
| `click.parameter-default.v1` | project-authored synthetic；copied bytes 0；`LicenseRef-Project-Authored` | repair；parameter/default；same-file | 一个 `parameter_default_changed` 最终 `fixed/validated`；run `fixed`；只有 signature anchor 发生预期变化 |
| `click.multi-group-partial.v1` | project-authored synthetic；copied bytes 0；`LicenseRef-Project-Authored` | repair；Git unique function rename、function delete、same/cross-file multi、partial | rename 与完整 delete groups `fixed/validated`，一个 design-derived signature group `needs_approval/truth_requires_approval`；run `partial`；只保留前两组预期 bytes |
| `click.conflict.v1` | project-authored synthetic；copied bytes 0；`LicenseRef-Project-Authored` | repair；conflict | 同 anchor 不同 replacement 的 findings 均 `unresolved/conflict.replacement`；run `unresolved`；target bytes 不变 |
| `httpx.responseclosed-streamclosed.v1` | `https://github.com/encode/httpx`；code `9b8f5af7596ab2208375a4d26b5b585d51b82b01`；doc `7d3a5347a9717169c00c73b71ba7c560e9a04443`；`BSD-3-Clause` | repair；historical rename；conservative rejection | 该历史符号为本阶段不支持的 class/exception symbol；`unresolved/unsupported.symbol_kind`；run `unresolved`；target bytes 不变 |
| `pydantic.apply-validators-field-name.v1` | `https://github.com/pydantic/pydantic`；`080c741ecf4e113b9c7487de16ffbba5182f03bf`；`MIT` | repair；Google Args parameter removal | 一个 `docstring_parameter_changed` 最终 `fixed/validated`；run `fixed`；只删除 `field_name` field bytes |
| `pydantic.google-returns.v1` | project-authored synthetic；copied bytes 0；`LicenseRef-Project-Authored` | repair；Google Returns | 一个 `docstring_return_changed` 最终 `fixed/validated`；run `fixed`；AST guard 通过 |
| `rich.iteration-speed-column.v1` | `https://github.com/Textualize/rich`；`669b5006b3bbfe6fb023d76cda62c59773141cf5`；`MIT` | repair；historical delete；conservative rejection | 该历史符号为本阶段不支持的 class symbol；`unresolved/unsupported.symbol_kind`；run `unresolved`；target bytes 不变 |
| `rich.incomplete-declaration-reject.v1` | project-authored synthetic；copied bytes 0；`LicenseRef-Project-Authored` | repair；public function delete；conservative rejection | 唯一引用不是完整 declaration；`unresolved/unsupported.incomplete_declaration`；run `unresolved`；target bytes 不变 |
- 每个 manifest MUST 含 `schema_version,dataset_id,case_id,project_family,provenance{kind,repository,code_revision,doc_revision,source_urls,license_spdx,copied_bytes},files,operation,coverage_tags,expected{status,finding_multiset,dispositions,reason_codes,changed_bytes},model_calls=0,offline=true`。`files` 每项固定 relative path、role、SHA-256 与 byte size；historical `copied_bytes` 必须等于所有来源于 upstream 的 fixture raw bytes 之和，synthetic 必须为 0，catalog audit 重新计算而不是信任声明值。
- evaluation matching key 是 repo-independent multiset key `(symbol identity,kind,component,normalized old,normalized new,relative code path,relative doc path,detector identity/version)`，与包含 repository identity 的 persisted fingerprint 不同。`TP = Σ min(expected[k],actual[k])`、`FP = |actual|-TP`、`FN = |expected|-TP`；总指标逐案求和。
- case pass 要求 FP=FN=0、status/disposition/reason exact match、target bytes/diff exact match、无额外 mutation、model_calls=0 且 offline。`repair_successes` 只计期望 fixed 且 exact repair 全通过的 repair case；`conservative_rejections` 只计带该 tag、target bytes 不变且 non-fixed disposition/reason exact 的 case。
- 每案使用全新 repo、Git-state DB 与 runtime lock root。deterministic projection 仅保留 status、matching keys、disposition/reason、relative changed bytes 和 metrics；排除 run/finding/repository ids、timestamp、duration、PID、absolute temp path、SQLite row id 与 lock generation。文件/许可 cap 遵守 FR-049。

### Key Entities

- **Repository Identity**: 由 versioned Git common-dir material 与初始 root commit 形成的 memory 隔离身份；linked worktrees 共享，独立 clone、move、copy 和 re-init 不共享。
- **Workspace Identity**: 由 Repository Identity 与 writable worktree root 形成的写锁隔离身份；每个 linked worktree 独立。
- **Structural Fact**: 从当前或变更前的 Python 符号中提取的可比较事实，包括符号、参数、返回值和 docstring 结构声明。
- **Document Claim**: 文档或 docstring 中关于代码结构的可定位声明，关联来源版本和精确证据范围。
- **Alignment**: Structural Fact 与 Document Claim 之间唯一、可复验的确定性对应关系；可引用仍有效的人工 alias。
- **Drift Finding**: 一项可独立处置的结构不一致，关联 normalized old/new value、双侧证据、truth 分类、detector identity/version、稳定 fingerprint、reason code 和终态。
- **Repair Group**: 一个 finding 或一组互不冲突、可一起验证的候选文档修改；不同组之间可独立成功或回退。
- **Validation Outcome**: 对原 finding 消失、validation scope 内无任何新增活跃 finding、最终快照一致及 docstring 安全边界的可断言结果。
- **Run Record**: Git state SQLite 中一次检查或修复的持久记录，关联 repository/workspace、输入证据、连续关键事件、finding 处置、最终状态和用量。
- **Manual Decision**: 用户对特定证据版本下 finding 作出的 ignore 或 false-positive 结论；证据或 detector 版本变化时失效。
- **Symbol Alias**: 用户确认的旧符号与新符号关系；只在绑定证据仍有效时参与重命名对齐。
- **Evaluation Case**: 符合 versioned manifest 的可重放结构样本，包含 normalized oracle、provenance/license、coverage、修复字节和零模型/离线结果。

### Technical Constraints

- **架构边界**: 只有一个 Drift Maintenance Agent；provider、detector、validator 和 Memory 均不是子 Agent。（来源：`codebase-context.md`“项目硬约束”第 1 条）
- **语言与产品边界**: 仅支持 Python 仓库，不引入多语言、GraphRAG、embedding、向量库、daemon 或 Web UI。（来源：`codebase-context.md`“项目硬约束”第 2 条）
- **确定性边界**: 结构路径不调用 LLM，且 LLM 不得参与符号搜索、对齐或扫描范围扩张。（来源：`codebase-context.md`“项目硬约束”第 3 条；`assumptions.md` L2-02）
- **写权限边界**: 默认只自动修改文档；Python 仅允许修改可精确认定的 docstring 字符串，业务 AST 永远只读。（来源：`codebase-context.md`“项目硬约束”第 4 条；`assumptions.md` L2-04）
- **安全与一致性**: 所有 patch 必须有来源版本和预期原文保护，逐 finding 重检并在最终快照上验证；stale 时不得覆盖外部内容。（来源：`codebase-context.md`“项目硬约束”第 7 至 9 条）
- **兼容性**: 默认 V1 JSON 必须通过冻结的 Stage 1 strict consumer；V2 只可显式选择，并保留所有 legacy 字段、状态和退出结果。（来源：`assumptions.md` L2-12；`codebase-context.md`“现有可观测行为”）
- **质量基线**: 项目支持 Python 3.11 及以上，并要求自动化测试、lint 与 strict 类型检查全部通过。（来源：`codebase-context.md`“依赖与质量”及“可复用接口与测试基础设施”）

## Success Criteria

### Measurable Outcomes

- **MO-001**: 版本化评测集中所有已标注结构 finding 的检测结果均与预期一致，误报数和漏报数均为 0。
- **MO-002**: 所有标记为可安全自动修复的评测案例均得到预期修复；所有标记为需审批、未解决、冲突或 stale 的案例均不发生越权写入。
- **MO-003**: 同文件多 finding、跨文件多 finding及“一项失败、一项成功”案例均产生逐 finding 可核验结果；部分成功案例的运行状态为 `partial`。
- **MO-004**: 同一 writable workspace 并发修复测试中同时进入写事务的运行数始终不超过 1；默认 5 秒超时、普通失败与可捕获中断不留下 owned edits，SIGKILL/crash 后 OS lock 可再次取得且残留状态不会被伪报为 fixed。
- **MO-005**: 有效人工 decision 和 alias 在后续运行中生效；代码、文档或 detector 版本改变后 100% 失效，且当前证据重新产生预期 finding。
- **MO-006**: 所有结构检查、修复及评测运行报告的模型调用数均为 0，且不发起网络或外部模型请求。
- **MO-007**: 所有自动 Python 写入均只改变经确认的 docstring；去除 docstring 后的抽象语法结构在修复前后 100% 相同。
- **MO-008**: 既有阶段 1 签名检查与修复回归场景、默认 V1 strict JSON consumer 和退出结果全部通过；SQLite 只出现在 Git administrative state，锁只出现在 runtime root，版本控制工作树不出现 Agent artifact。
