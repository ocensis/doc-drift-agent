# 核心算法

本文件描述当前实现采用的主要算法、输入输出、不变量和复杂度。它强调可复验过程，不把“调用模型”当作一个不可解释算法步骤。

## 1. 领域数据模型

Agent 的判断链由四类核心对象组成：

| 对象 | 含义 |
| --- | --- |
| `CodeFact` | 从当前或 baseline Python 源码提取的 symbol、signature、semantic fact 和精确 source anchor |
| `DocClaim` | 从 Markdown/docstring 提取的 symbol 声明、typed normalized value、truth hint 和精确 anchor |
| `Alignment` | 一份唯一 code fact 与一份唯一 doc claim 的确定性对应关系 |
| `DriftFinding` | detector 对一个具体 component 的 old/new 差异、证据、disposition、reason 和稳定 fingerprint |

所有下游写入都必须能回链到 claim/fact anchor；没有 anchor 的“理解”不能变成 patch。

## 2. Git scope 与 snapshot

实现位于 [scope/git.py](../../src/drift_agent/scope/git.py)。

### 2.1 `changed` 范围

默认先冻结 observed `HEAD`，以 `HEAD` tree 作为 before side，再合并：

- index 中的 staged change；
- worktree 中的 unstaged change；
- 匹配配置范围的 untracked 文件。

因此它表示 HEAD-to-current-worktree，而不是只看某一种 Git 状态。

### 2.2 `since REV` 范围

算法如下：

1. 将 `REV` 解析为恰好一个 commit，拒绝 option-like、range、blob/tree 或不存在的值；
2. 冻结 observed `HEAD`；
3. 求 `merge-base(REV, observed_HEAD)`，要求恰好一个 best merge base；
4. 以 merge base tree 作为 before side；
5. 叠加此后 committed、staged、unstaged、rename/delete 和相关 untracked change；
6. 运行期间若 `HEAD` 或已捕获文件 hash 改变，结果标记 stale。

使用 merge base 而非 `REV..HEAD` 使分叉分支仍以共同祖先为正确 before evidence。

### 2.3 文件过滤与路径安全

候选文件必须：

- 后缀为 `.py` 或 `.md`；
- 位于配置的 source/docs root；
- 命中 include 且不命中 exclude；
- 所有 path component 都不是 symlink；
- 使用规范 repo-relative POSIX path。

### 2.4 快照摘要

对排序后的相关路径记录 `SHA256(file bytes)`，并将 observed HEAD、路径和 hash 组成 snapshot fingerprint。Config、validation target 和 repair 读取依赖也进入 closure，避免“目标文件没变但影响判断的输入变了”仍发布旧结论。

若有 `n` 个候选文件、总字节数为 `B`，枚举和 hashing 的时间约为 `O(n log n + B)`，manifest 空间为 `O(n)`；Git diff 本身由 Git 实现决定。

## 3. 证据抽取与规范化

### 3.1 Python fact

[python_facts.py](../../src/drift_agent/providers/python_facts.py) 用 Griffe 构建 public function/method 视图，再用 AST 补充精确 span、docstring 位置和受限 semantic fact。表达式只解析和规范化，不 import 目标仓库，也不执行默认值。

Python annotation/default 使用 AST canonical form，例如 [normalization.py](../../src/drift_agent/normalization.py) 的 `ast.dump(..., include_attributes=False)`。这让格式差异不会被误报为语义差异，并保留 `True` 与 `1` 等 typed value 区别。

### 3.2 Markdown claim

[markdown_claims.py](../../src/drift_agent/providers/markdown_claims.py) 解析 Markdown token，但 patch anchor 仍按 UTF-8 byte offset 指回原始 bytes。受支持的结构 claim 需要 exact-FQN heading 和完整 Python signature fence；semantic claim 还要求紧随其后的受限一行 return literal 声明。

### 3.3 Google-style docstring claim

[docstring_claims.py](../../src/drift_agent/providers/docstring_claims.py) 只解析明确支持的 `Args`/`Returns` 结构。[validation/docstring_ast.py](../../src/drift_agent/validation/docstring_ast.py) 再证明目标确实是 function/method 的 docstring 字符串，而不是任意 Python string。

不支持的格式返回 extraction issue/unresolved；不会退化为正则猜测任意 prose。

## 4. Exact alignment 与歧义

结构对齐位于 [alignment.py](../../src/drift_agent/alignment.py)。

算法：

1. 按 `symbol_id` 统计 fact 数量；
2. 按 `symbol_id` 统计 claim 数量；
3. 只有 `fact_count[id] == 1` 且 `claim_count[id] == 1` 时产生 `Alignment`；
4. 同一 ID 出现多个 claim 或 fact 时，单独形成 ambiguity evidence。

若 facts 数为 `F`、claims 数为 `C`，时间和额外空间都是 `O(F + C)`。

这种规则故意不做 fuzzy name、路径相似度或 embedding nearest-neighbor。自动修复需要的是唯一性证明，而不是较高概率。

## 5. 结构变化检测

[detectors/structural.py](../../src/drift_agent/detectors/structural.py) 对 baseline/current fact 与对齐 claim 做 component-level diff，主要覆盖：

- parameter added/removed/order/kind；
- annotation、requiredness、default change；
- return annotation change；
- symbol deletion 和确定性 rename；
- Google docstring parameter/return change；
- 不支持或歧义的结构变化。

Detectors 输出 typed `old_value`/`new_value`，而不是只生成一段解释文本。Finding 使用固定 sort key，确保输入顺序不同不会改变 bundle 和评测结果。

Signature detector 先用计数器拒绝重复参数，再比较参数集合、公共参数顺序、kind、annotation、requiredness/default 和 return annotation。Google docstring detector 以 symbol/component 建索引；普通 method 忽略第一个 receiver 参数，staticmethod 不忽略。无法解释的 signature 文本差异不会被包装成确定结论，而是进入 unsupported/unresolved。若单个 symbol 参数数为 `P`、findings 数为 `F`，主要本地成本约为 `O(P log P + F log F)`。

### 5.1 Rename 判定

Rename 只利用 Git rename/path transition、baseline/current symbol transition 和已验证 alias 等确定性证据。无法证明一一对应时按 delete/unsupported 处理，不基于名字相似度猜测。

### 5.2 Truth direction

对每个 finding 找到匹配 claim 后，truth 分类顺序是：

1. claim 显式 truth；
2. 配置路径规则中恰好命中一个类别；
3. 否则 `unknown`。

`code_derived` 保留 detected；`design`/`contract` 变为 `needs_approval`；`unknown` 变为 `unresolved`。多条规则命中也属于 unknown，避免隐式优先级。

## 6. 窄语义算法

语义路径只处理“同步函数的唯一常量返回值”和“Markdown 的单行 literal 声明”。

### 6.1 Literal grammar

允许的值是：

- `None`；
- `True` / `False`；
- signed-64 范围内整数；
- 单个 Python 字符串 literal，能规范化为 UTF-8 scalar value。

负整数只允许无额外空白的单个 `-` 加整数 token。函数除可选 docstring 外必须只有一条常量 `return`；async、分支、计算表达式、调用或多 return 都不推断。

### 6.2 Semantic alignment

[semantic_alignment.py](../../src/drift_agent/semantic_alignment.py) 按 `(symbol_id, component_id)` 分组：

1. 要求恰好一个 semantic claim；
2. claim 无 extraction error 且能通过 typed schema；
3. 要求恰好一个当前 `CodeFact`；
4. 要求该 component 恰好一个 `SemanticCodeFact`；
5. 成功则输出高置信 exact-FQN alignment，否则输出带稳定 hash 的 issue。

整体分组、排序复杂度约为 `O(F + C log C)`；通常排序是主项。

### 6.3 Semantic detector

[detectors/semantic.py](../../src/drift_agent/detectors/semantic.py) 比较 typed literal：

- `Returns` 不一致产生 `semantic_direct_mismatch`；
- `Always returns` 与当前常量不一致产生 `semantic_over_promise`。

Detection 全程零模型。模型只在 repair 时看到已经通过上述条件的 candidate。

Executable detector 同样保持简单：只有 validator 的 `FAILED` receipt 会转换为 `broken_example`；`PASSED` 只保留 validation receipt，`UNAVAILABLE` 表示基础设施/输入无法证明，不伪造成 drift。

### 6.4 Semantic repair routing

[repair/semantic.py](../../src/drift_agent/repair/semantic.py) 再次交叉验证 finding、alignment、code/doc evidence hash 和 trusted literal anchor，构造最小模型输入。

路由规则：

1. 首次 proposal 使用 fast profile；
2. fast 高置信且 literal 等于 required typed value，进入 attempt 1；
3. fast 低置信直接升级 strong；
4. 首次响应 schema 不合法，最多一次 schema-only retry；该调用计 usage，不计 patch attempt；
5. attempt 1 验证失败时可升级 strong，进入 attempt 2；
6. 每个 finding 最多两次 patch attempt，之后 abstain。

模型返回值还要经过本地 literal parser 和 expected value 等价检查；看似合法但不是所需 typed value 的输出不能写入。

## 7. 稳定 Finding fingerprint

[normalization.py](../../src/drift_agent/normalization.py) 将以下 material 编码为 key-sorted、紧凑 UTF-8 canonical JSON，再做 SHA-256：

```text
schema + repository_id + symbol_identity + finding kind/component
+ typed old/new value
+ code/doc evidence path/span/source hash
+ detector id/version
```

V3 semantic finding 使用独立 schema tag，避免改变冻结 legacy fingerprint。Fingerprint 的作用包括稳定排序、memory decision 绑定、重复运行对照和 benchmark neutral projection 的 provenance。

任一 evidence byte、detector version、仓库 identity 或 typed value 改变都会自然失效旧 fingerprint。

## 8. Repair planning 与冲突图

[repair/planner.py](../../src/drift_agent/repair/planner.py) 的输入是 findings 和候选 `PatchAttempt`。

### 8.1 合并

按以下完整 replacement tuple 分组：

```text
(path, start, end, source_hash, expected_text, replacement_text, target_kind)
```

多个 finding 指向完全相同替换时只保留一个代表 attempt，并合并 finding IDs。Group ID 是 finding fingerprint 与 anchor material 的 canonical SHA-256。

### 8.2 冲突

对 group 两两比较，标记：

- 同 anchor 不同 replacement；
- 同 anchor 不同 expected text；
- 同文件不同 base source hash；
- byte span overlap，包括同位置零长度插入；
- 跨文件 write/read validation dependency。

最后按 `(path, start, end, group_id)` 稳定排序。若有 `G` 个 group、每组 attempt 数有上界，主要复杂度是 `O(G²)`；当前 group 很小，优先选择清晰可审计的冲突判定。

## 9. Workspace transaction

[workspace/transaction.py](../../src/drift_agent/workspace/transaction.py) 实现 optimistic CAS + 原子发布：

1. 证明 path repo-local、无 symlink，target kind 可写；
2. 读取当前 bytes，验证整文件 source hash；
3. 验证每个 span 内 exact expected bytes；
4. 验证 spans 不重叠；
5. 在内存中按 byte offset 生成 planned bytes；
6. 写临时文件、flush/fsync、继承 mode；
7. 替换前再次检查 target hash；
8. `os.replace` 原子发布并记录 journal。

### 9.1 Rollback

正常 rollback 恢复 original bytes。若发布后出现外部并发改动，selective rollback 使用序列差异把 planned bytes 映射到当前 bytes；只有能证明外部 edit hunks 与 Agent-owned spans 不相交时才反向移除自己的修改。无法证明时返回 residual change/needs attention，不覆盖第三方编辑。

这不是传统数据库事务，而是针对文件 bytes、CAS 前置条件和可证明逆操作的工作区事务。

普通 byte patch 的主要成本来自读写和 span 排序；unified diff 与 selective rollback 使用 `difflib/SequenceMatcher`，病理输入下最坏可能达到 `O(B²)`，不能把整个 rollback 算法描述为严格线性。

## 10. Executable validation

[validation/commands.py](../../src/drift_agent/validation/commands.py) 分为 compile 和 run 两步。

### 10.1 Compile

1. `shlex.split` 解析受信配置；
2. 只接受 `python -m doctest`、`python -m pytest`、`pytest`；
3. 拒绝 shell 控制字符、`@argfile`、absolute/`..` path；
4. 只接受小型 flag allowlist；
5. 要求至少一个显式 repo-local `.py`/文档 target；
6. 丢弃配置中的 executable，统一使用 `sys.executable`。

### 10.2 Run

1. 捕获所有可暴露 regular file 的 validation-input manifest；
2. 复制仓库到临时目录，排除 `.git`、`.env*`、cache 和 symlink；
3. 对副本重算 manifest，任何差异都判 input changed；
4. bootstrap 在仓库路径进入 `sys.path` 前 import 真实 doctest/pytest，防止本地同名文件 shadow；
5. 构造最小环境，清除 provider key、proxy 相关来源和 Python 注入变量；
6. 禁用 pytest plugin autoload/cache，默认通过 `sitecustomize` 阻断常见 socket API；
7. 使用 `shell=False` 和剩余 wall-clock timeout 执行；
8. exit `0` = passed，exit `1` = confirmed failed，其他/timeout/缺依赖 = unavailable；
9. stdout/stderr 截断为有界 summary。

`check` 中 required oracle 失败形成 `broken_example` finding；repair 中 required failure 使对应未验证 group rollback。最终快照还会重新执行 closure validation。

## 11. Budget 与重试

[agent/budget.py](../../src/drift_agent/agent/budget.py) 使用 reserve-before-use：

- model call 与 input-token upper bound 在请求前预留；
- validation command 和 patch attempt 在副作用前预留；
- 已启动但失败的操作不退款；
- provider 实际 usage 回填 input/output token 和 cost，若超预算立即 fail closed；
- wall-clock 使用 monotonic clock，所有阶段共享剩余时间。

这种记账避免“失败调用不算成本”导致的隐式无限 retry。模型 transport 自身不自动重试；允许的 schema retry 和 second patch attempt 都由 Agent 显式记录。

## 12. Memory 复用与失效

Memory 算法位于 [memory](../../src/drift_agent/memory/) 和 [workspace/identity.py](../../src/drift_agent/workspace/identity.py)。

复用人工 decision 或 alias 时至少检查：

- repository identity；
- 当前/历史 symbol 与相关 Git object；
- finding fingerprint 或 evidence hash；
- detector/provider version；
- confirmation/ancestry 等适用条件。

只有全部条件仍成立才抑制 finding 或应用 alias。Memory 是 evidence 的缓存和人工决策索引，不是新的 truth source。

Run lifecycle 也作为数据完整性算法被验证：check 必须拥有固定事件序列；repair 中每个 group 必须先 `group_started`，再恰好出现一个 terminal event，不能悬空或重复。扫描复杂度为 `O(E)`。SQLite 使用短 `BEGIN IMMEDIATE` 事务；人工 decision/alias 变更递增 manual revision，run finish 以启动时捕获的 revision 做 CAS，防止运行过程中人工状态变化后仍发布旧结果。

## 13. 运行状态聚合

[domain/status.py](../../src/drift_agent/domain/status.py) 的优先级为：

1. `failed=True` → `failed`；
2. `stale=True` → `stale`；
3. 无 disposition → `clean`；
4. check 且有 finding → `drift_found`；
5. repair 全部 fixed → `fixed`；
6. fixed 与其他 disposition 混合 → `partial`；
7. 含 unresolved → `unresolved`；
8. 含 needs approval → `needs_approval`；
9. 其他未修复情况 → `unresolved`。

统一映射为退出码 `0/1/2`，避免 CLI 和 CI 各自解释状态。

## 14. Stage 4 对照评测算法

评测代码位于 [evaluation](../../src/drift_agent/evaluation/)，与普通 Agent runtime 隔离。

### 14.1 Plan → Run → Score → Report

1. **Plan**：冻结 dataset/case、CLI/model、reasoning effort、trial、runtime 和 contract digests；
2. **Run**：为每个 subject/case/trial 创建 fresh repo 与隔离环境，执行一次，不自动 retry，封存 stdout/stderr/JSONL/snapshot receipt；
3. **Score**：hidden trusted scorer 验证 raw evidence，投影为 subject-neutral finding key，计算实际 changed bytes 和 derived abstention；
4. **Report**：只聚合严格 paired observation，同时保留 incomparable 和 completeness 状态。

### 14.2 严格配对键

Codex 与 Drift Agent 只有以下七元组完全相同才配对：

```text
(dataset_id, case_id, case_manifest_sha256, trial_id,
 snapshot_digest, task_digest, scope_digest)
```

同 subject/case/trial 的重复或冲突 observation fail closed，不会选择一个“更好结果”。

### 14.3 Neutral finding 与 changed bytes

[benchmark_scoring.py](../../src/drift_agent/evaluation/benchmark_scoring.py) 将 V3 finding 映射为不泄漏内部 detector schema 的 neutral key：code/doc path、symbol FQN、finding family、component 和 typed old/new value。

Mutation 由 before/after canonical repository snapshot 计算，不相信 subject 声称“没有改文件”。Repair abstention 也由 operation、最终 status 和 mutation 是否为空共同推导。

### 14.4 指标与缺失值

[stage4_comparison.py](../../src/drift_agent/evaluation/stage4_comparison.py) 对 paired observation 汇总：

- `precision = TP / (TP + FP)`；
- `recall = TP / (TP + FN)`；
- `F1 = 2TP / (2TP + FP + FN)`；
- repair@1/@2、correct abstention、validation、regression/safety；
- model/tool calls、tokens、cost、duration 和 completeness。

零分母为 `not_measured`，不是 0。部分 telemetry 缺失为 `accounting_incomplete`，已知 subtotal 与未知计数分别保留。3 次 trial 是同一 case 的重复测量，不增加独立问题数。

### 14.5 Portable 与 control

18 个冻结 case 中，12 个能用 subject-neutral task 和 repo-observable oracle 做 paired aggregate；5 个依赖 Drift Agent 私有 failure injection、1 个依赖 frozen golden model answer，只进入 control regression。这样避免给 Codex 暴露内部 fault/oracle，也避免把不可比案例加入分母。

## 15. 算法边界

当前算法有意不解决：

- 模糊 symbol/entity resolution；
- 通用 prose entailment；
- dynamic execution、复杂 control-flow semantic summary；
- 多语言 signature/docstring 统一；
- equivalent-but-not-byte-identical repair 的自动语义裁决；
- 针对恶意目标代码的完整 OS sandbox。

这些能力若加入，必须先给出新的 evidence contract、唯一性/置信度政策、失效规则、验证 oracle 和冻结评测，不能只增加一个模型 prompt。
