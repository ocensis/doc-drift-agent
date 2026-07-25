# FR-009 评测框架

散文/图表级语义漂移检测(FR-009)的评测工具集。所有工具共享一个 harness 库,用**同一个评分函数**打分,保证工具、基线、探针三方的数字可直接比较。

> **开跑前必读**:[评测有效性检查清单](../../docs/evals/eval-validity-checklist.md)——八条,每条对应一次真实的"跑了半天发现方向错了"。先校准尺子,再读数。
> **背景与逐次迭代**:[docs/evals](../../docs/evals/README.md)。

## 结构

```
_harness.py                 共享库:物化 fixture、加载 GT、评分、分项、union@K
fr009_section_drift.py      被测工具:drift-agent 的召回/精度(需模型)
fr009_agent_baseline.py     竞争基线:通用 agent 做同一件事(需 claude CLI)
fr009_evidence_coverage.py  模型无关探针:证据覆盖率(召回天花板,秒级)
section_evidence_health.py  模型无关探针:证据集中度(过拟合体检,秒级)
```

四个工具都不硬编码仓库路径或阈值——换基准只改 `--fixture`/`--ground-truth`。

## harness 提供什么(`_harness.py`)

| 函数 | 作用 |
|---|---|
| `materialize_fixture` | 克隆 bundle 到临时目录、checkout head_sha,返回 `(repo, baseline)`。扫描的永远是一次性副本,不碰源仓库。 |
| `resolve_target` / `add_target_arguments` | 统一的 `--fixture` 或 `--repo`+`--baseline` 入口。 |
| `load_ground_truth` | 返回 `(items, window, {label: class})`。 |
| `score` | **一对一 + section 包含匹配**。一条 finding 至多核销一条 GT;GT 密集文档下用 section 边界而非行窗口,避免误归属。 |
| `class_recall` | 按 prose/diagram 分项,分母取 GT class 计数。 |
| `union_over_repeats` | union@K 及其在各子集间的 min/max/mean——防止把一次幸运子集读成提升。 |

## 三种工具怎么用

### 1. 被测工具的召回/精度

```bash
set -a && source .env && set +a          # OPENROUTER_API_KEY
python evals/field/fr009_section_drift.py \
  --fixture evals/datasets/field/react-refactor-v1 \
  --ground-truth docs/field-reports/2026-07-20-customer-agent-react-refactor/eval-ground-truth.json \
  --repeats 5 --max-model-calls 56 --timeout-seconds 5400 \
  --dump-findings /tmp/findings.json
```

主指标 `union_recall` 与 `union_at_3`(含 min/max/mean),精度看 `mean_extras`。`--dump-findings` 落盘每轮 findings,供 extras 人工分类(FR-006)与离线重打分。**每轮落盘**,中途崩了不丢数据。

### 2. 竞争基线(回答"值不值得存在 / 是模型还是架构的限制")

```bash
# 无约束天花板:放开工具、去掉工具特有约束 → 任务本身能做到多少
python evals/field/fr009_agent_baseline.py --fixture ... --ground-truth ... \
  --repeats 3 --mode unconstrained --dump /tmp/baseline_unc.json

# 同约束对照:与工具同等镣铐 → 同规则下工具 vs 通用 agent
python evals/field/fr009_agent_baseline.py --fixture ... --ground-truth ... \
  --repeats 3 --mode constrained --dump /tmp/baseline_con.json
```

两个模式**通常都要跑**。⚠️ 别把 `constrained` 下的"打平"读成"工具追平了强 agent"——那是给对手戴上工具自己的镣铐比出来的(IT-0013 的真实教训)。无约束天花板才是"任务可解性"的诚实参照。

### 3. 模型无关探针(秒级,不花模型预算)

```bash
# 证据覆盖率:能证明漂移的代码文件有没有进证据集 = 召回天花板
python evals/field/fr009_evidence_coverage.py --fixture ... --ground-truth ... \
  --expectations docs/field-reports/.../eval-evidence-expectations.json

# 证据集中度:某个大文件是否成了大多数段落的证据 = 过拟合信号
python evals/field/section_evidence_health.py --fixture ...
```

改证据选择前后跑这两个,能在花 20 分钟真跑之前就发现"覆盖率没涨"或"集中度失控"。**注意**:证据覆盖率是必要条件、诊断量,**不能当优化目标**——"多塞文件"能同时刷高它和噪声(检查清单 §5)。

## 已知仪表待办

- `code_quote`(反证代码)目前不落盘,导致精度类护栏无法离线预筛(IT-0031 因此放跑了一次注定失败的实跑)。应把它提升到 finding 的 `new_value` 并进 dump——有 wire/指纹爆炸半径,需独立处理,未做。
