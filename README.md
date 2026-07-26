# doc-drift-agent

代码改了、文档没跟上——把这件事变成一道能进 CI 的门禁。

只读检测是确定性的,不调模型、不联网。同一个 agent core 挂三个入口:命令行、stdio MCP server、GitHub Action。

支持 Python(`.py`)与 TypeScript(`.ts`/`.tsx`)源码,Markdown(`.md`)文档。

---

## 它报什么

给函数加一个参数,文档没改:

```python
# src/greeting/api.py
-def greet(name: str) -> str:
+def greet(name: str, *, loud: bool = False) -> str:
     """Return a greeting.

     Args:
         name (str): Who to greet.
     """
```

~~~markdown
<!-- docs/api.md -->
## greeting.api.greet

```python
def greet(name: str) -> str:
    ...
```
~~~

跑一次:

```
$ drift-agent ci check --repo demo --since HEAD~2 \
    --state-dir /tmp/s --artifacts-dir /tmp/a
status: drift_found
blocking: 1
```

两条 finding,**分量不一样**:

| kind | reason | reason_code | SARIF |
|---|---|---|---|
| `parameter_added` | parameter 'loud' exists in code but not documentation | `unknown_truth` | **error** |
| `docstring_parameter_changed` | Google Args field for 'loud' is missing | `unsupported.literal` | note |

第一条说"文档和代码对不上",第二条说"docstring 没有 `loud` 这一项,我没有锚点可校验"。**只有第一条能挡合并**——下面 [blocking 与 advisory](#blocking-与-advisory) 讲为什么。

## 快速开始

```bash
uv sync --dev
uv run drift-agent init --repo /path/to/repo      # 生成 drift-agent.toml
uv run drift-agent check --repo /path/to/repo     # 只读检测
```

`init` 会推断 `source_roots` 和 `docs_roots`,推断不出来时**保守拒绝**,而不是生成一份注定失败的配置。`[truth]` 分类留空等你确认。

默认 scope 是相对 `HEAD` 的工作区改动。要可复现的范围用 `--since`:

```bash
uv run drift-agent check --repo /path/to/repo --since origin/main --format json --output-version 3
```

人类输出很短:一行 `status:`,`failed` 时补上每条失败/不可用的 required validation,`repair` 写过文件则再列 `changed:`。**finding 的细节只在 `--format json` 里**。

---

## 三个入口

### CLI

| 命令 | 作用 |
|---|---|
| `check` | 只读检测 |
| `repair` | 有界修复,写文档并验证后交付 patch |
| `ci check` | 只读 + 产出 CI 产物,见下 |
| `init` | 生成配置 |
| `model probe` | 验证 key/模型/structured-output 通路 |
| `decision` / `alias` | 人工裁决与符号别名 |

`repair` 是显式命令,**不会**被 `check` 或任何 adapter 隐式触发。

### stdio MCP

绑定单一仓库,只走 stdio。tool 输入改不了 repo/state 路径、预算和验证命令。

```bash
uv run drift-agent-mcp --repo /path/to/repo --state-dir /tmp/drift-mcp-state
```

暴露两个 typed tool:`check_drift`、`repair_drift`。仓库没有 `drift-agent.toml` 时两个 tool 依然可用,返回一条 `check="config"` 的结构化指引而不是裸异常。

### GitHub Actions

[`action.yml`](action.yml) 是 composite action,把调用、SARIF 上传、job summary 和产物归档包在一起:

```yaml
permissions:
  contents: read
  security-events: write
  actions: read              # codeql-action 要读自己这次 run

steps:
  - uses: actions/checkout@v6
    with:
      fetch-depth: 0         # 门禁作用于 committed range,需要 base commit 在历史里
  - uses: ocensis/doc-drift-agent@v0
```

主要输入:

| 输入 | 默认 | 说明 |
|---|---|---|
| `base` | `""` | 范围起点;留空时回落到 PR base SHA |
| `semantic` | `false` | 语义检测,要 API key,按次收费 |
| `fail-on-drift` | `false` | 只对 blocking finding 生效 |
| `upload-sarif` | `true` | 需要 `security-events: write` |
| `version` | `@v0` | uvx 装哪个包版本 |

输出:`status`、`exit-code`、`blocking-count`、`artifacts-dir`。

本仓库自己吃自己的狗粮,见 [`.github/workflows/drift.yml`](.github/workflows/drift.yml):结构化 job 跑所有 PR(不要 key),语义 job 只跑同仓库 PR(fork 拿不到 secrets)。

---

## 四个设计决定

这几条是这个项目真正想说的东西。

### 退出码就是答案

```
0  clean / fixed
1  有 finding
2  门禁没能给出答案(stale / failed)
```

**2 永远让 job 失败**,与 `fail-on-drift` 无关。"我不知道"绝不能被降级成"我没发现问题"——那是静态检查工具最容易骗人的地方。

### blocking 与 advisory

一条 finding 的 `reason_code` 说的是两件不同的事之一:

- **文档错了** —— `unknown_truth`、`precondition_changed`、`omission.config_key`……
- **检测器判断不了** —— 任何含 `unsupported` / `ambiguity` / `ambiguous` 词元的 code

只有前者算 blocking。后者是**本工具覆盖面的边界,不是被检仓库的缺陷**。

具体到有多要命:`unsupported.symbol_kind` 会对每一个带装饰器的公开函数报一条。本仓库 `src/` 里有 153 个这样的方法,[`drift_agent.cli`](src/drift_agent/cli.py) 的 15 个 Typer 命令 100% 中招——**写多少文档都消不掉**。拿它挡合并,这个 action 没有任何人能接入。

于是:

- SARIF 里 advisory 是 `note` 不是 `error`,不在没有已知问题的行上刷红
- `ci check` 在 `status:` 之外多打一行 `blocking: N`
- `fail-on-drift: "true"` 只在 `N > 0` 时阻断

**advisory 是"不阻断",不是"不报告"**——它照样进 SARIF、job summary 和 `bundle.json`。

### adapter 只报告,不代你行动

`ci check` 往 worktree **外面**写四个文件:

```
bundle.json      固定 V3 schema
results.sarif    SARIF 2.1.0
summary.md       有界 Markdown
pr-comment.md
```

然后就结束了。**它不上传产物、不发评论、不调 forge API、不做任何 Git 写操作。** 所有面向 GitHub 的动作都由外层 workflow 显式决定。

这样做的直接好处:adapter 在你本地和在 CI 里行为完全一致,本地能复现的问题不会到了 CI 变成另一回事。

顺带一条纪律:门禁报告,workflow 决策。policy 步骤读不到 `blocking` 计数时**宁可失败也不猜 0**——跑挂了的 run 不能冒充"什么都没发现"的 run。

### scope 是 committed range,不是工作区

`--since REV` 先冻结当前 `HEAD`,再以 `merge-base(REV, HEAD)` 作为 before side。这让同一次检查可复现;默认的 `changed` scope 跟着工作区走,方便但不可复现。

CLI 不提供 `--file` / `--symbol`——scope 由 git 决定,不由手输的路径决定。

---

## 配置

仓库根放一份 `drift-agent.toml`:

```toml
[project]
source_roots = ["src"]
docs_roots = ["docs"]
include = ["src/**/*.py", "docs/**/*.md"]
exclude = ["**/generated/**", "**/.venv/**"]

[truth]                                # 谁是权威:代码还是文档
code_derived = ["docs/api.md", "docs/api/**"]
design       = ["docs/design/**"]
contract     = ["docs/contracts/**"]

[validation]                           # 只允许 doctest / pytest
commands = ["python -m pytest tests/test_api.py -q"]
network  = false
```

配置缺失或无效时,`check` / `repair` 返回 `status: failed` 加一条 `check="config"` 的 validation receipt,summary 以稳定 reason code(`config.missing` / `config.invalid` / `config.unreadable`)开头并带修复指引。

`[validation].commands` 在一次性工作区里跑:不含 `.git`、不含任何 `.env*`、不继承宿主 token 与代理变量、`shell=False`。**这是用来约束正常项目测试的,不等价于防恶意代码的容器沙箱。**

## 模型(可选)

结构检测、docstring 检测和确定性语义检测**全程零模型调用**。`.env` 存在不会隐式启用网络。

只有显式 `check --semantic` / `repair --semantic` 才进模型路径。**用 `--format json` 时必须同时指定 `--output-version 3`**(默认的人类输出不受此限);反过来单独选 V3 不会隐式打开语义能力。

```dotenv
OPENROUTER_API_KEY=...
OPENROUTER_MODEL=provider/model
# 可选
OPENROUTER_FAST_MODEL=provider/fast-model
OPENROUTER_STRONG_MODEL=provider/strong-model
OPENROUTER_PROVIDER=streamlake      # 固定 provider 并关闭 fallback
```

验证通路(会产生极小费用):

```bash
uv run --env-file .env drift-agent model probe --profile fast --format json
```

探针不读仓库内容,只输出连接状态、实际模型、request id、token 和 cost。

语义边界是**刻意窄**的:Markdown 必须是 exact-FQN 标题 + 完整 Python signature fence + 紧随其后的一行 ``Returns `<literal>`.``;代码必须是同步函数且只有一条常量 `return`。散文不作推断。模型只被允许回一个 literal 替换值——path、span、diff 和实际写入都不由模型决定。

## License

[Apache-2.0](LICENSE)。选它而不是 MIT 是因为多一条显式专利授权——这个仓库同时是一个会在别人 runner 里执行的 GitHub Action,接入方需要的授权范围比"读代码"更宽。
