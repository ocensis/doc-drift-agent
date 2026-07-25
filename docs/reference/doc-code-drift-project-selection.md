# Doc-Code Drift Agent：靶子项目选型与竞品调研

> **历史调研（保留参考）**：靶子项目与工具事实仍可参考，路线图和架构结论不代表当前方案。请以 [2026-07-12 主设计](../design/2026-07-12-doc-code-drift-agent-design.md) 为准。

> 调研时间：2026-07-10
> 目的：为 Doc-Code Drift Agent 选择首个开源靶子项目、设计真实历史评测集，并明确与现有工具的差异。

---

## 1. 执行摘要

本次调研得到四个核心结论：

1. **开发主靶子选择 Click**：规模适中、代码与文档映射清楚、手写 Markdown 与自动 API 文档并存，最适合跑通第一个确定性检测闭环。
2. **真实历史评测集采用3仓组合**：HTTPX 提供 API 重命名 drift，Pydantic 提供签名与 docstring drift，Rich 提供代码删除后文档残留，Click/Typer 提供 CLI 行为和示例 drift。
3. **Python MVP 优先复用 Griffe，而不是直接从 tree-sitter 开始**：Griffe 已处理 Python public API、别名、导出、参数、注解和 docstring；tree-sitter 留给多语言阶段。
4. **差异化不应该被定义为“业界完全没人做”**：Swimm、Doc Detective、pydoclint、Specmatic 等已覆盖局部问题。本项目真正的差异在于统一完成“既有文档声明抽取 → 自动符号对齐 → 双向完备性检查 → 结构与语义 drift 判断”。

推荐组合：

```text
开发主靶子：Click
历史评测集：HTTPX + Pydantic + Rich + Click/Typer
Python 事实底座：Griffe
确定性基线：pydoclint + interrogate + doctest
后续压力测试：FastAPI
```

---

## 2. 选型标准

首个靶子不是越大、越知名越好，而是需要同时满足以下条件：

### 2.1 必要条件

- 源码和文档都在同一个公开 Git 仓库中。
- 有足够多的手写文档，而不是全部由源码自动生成。
- 文档能与具体函数、类、配置项或示例建立对应关系。
- 项目历史中存在代码变化后补文档、修复旧示例等真实提交。
- 项目规模允许个人在 MVP 阶段理解和扫描。
- 可以设计确定性 oracle，而不是所有结论都依赖 LLM。

### 2.2 优先条件

- Python 项目，便于先用成熟静态分析工具完成闭环。
- Markdown 文档较多，适合后续扩展文档 AST 和段落锚点。
- 有清晰的 public API 和稳定的模块边界。
- 文档中包含函数调用、参数说明、CLI 命令或预期输出。
- 有完整 Git 历史，可构造 drift 出现和消失的时间区间。
- 有测试可作为运行时证据。

### 2.3 不适合作为第一靶子的特征

- 多语言、多仓库和跨运行时依赖过多。
- 文档主要由 autodoc/mkdocstrings 自动生成，独立 drift 面太少。
- 大量动态元编程导致静态签名与用户 API 差距很大。
- 文档存在多语言翻译副本，容易制造大量非目标噪声。
- 示例依赖外部网络、凭证或不稳定服务。

---

## 3. 候选项目横向比较

| 项目 | 文档特点 | 源码-文档映射 | 真实样本潜力 | MVP 难度 | 推荐用途 |
|---|---|---|---|---|---|
| **Click** | MyST Markdown + Sphinx，教程与 API 并存 | 很清楚 | 高 | 低 | **首个开发靶子** |
| **Typer** | 大量 Markdown 教程和可执行 CLI 示例 | 清楚但有动态转换 | 很高 | 中 | 第二阶段示例检测 |
| **HTTPX** | 手工 API 清单和使用指南丰富 | 很清楚 | 很高 | 低~中 | 历史 drift 主评测集 |
| **Pydantic** | API、docstring、概念文档和示例丰富 | 清楚但类型系统复杂 | 很高 | 中~高 | 签名/docstring 评测集 |
| **Rich** | API 与输出文档丰富，真实修复较干净 | 清楚 | 中高 | 中 | 高精度小型评测集 |
| **Tenacity** | 小型 Sphinx/RST 项目，doctest 较多 | 很清楚 | 中 | 很低 | Smoke test |
| **Requests** | 历史长，API 稳定，RST 为主 | 很清楚 | 中高 | 低~中 | 长期潜伏 drift |
| **FastAPI** | 文档和可执行示例极多，多语言 | 丰富但复杂 | 极高 | 高 | 后期压力测试 |

---

## 4. 主靶子：Click

仓库：<https://github.com/pallets/click>

### 4.1 为什么选择 Click

Click 在四个维度上最平衡：

- **代码规模可控**：核心源码集中在 `src/click/` 的十余个模块。
- **文档结构适合解析**：使用 Sphinx + MyST Markdown，既有自动 API，也有大量手写说明。
- **对应关系清楚**：decorator、参数类型、命令、上下文、测试工具都有明显对应页面。
- **真实变更活跃**：参数语义、终端行为、CLI 输出和兼容性会持续变化。

建议第一期只选择以下范围：

| 代码范围 | 文档范围 | 首期检测项 |
|---|---|---|
| `src/click/core.py` | `docs/api.md`、`docs/commands-and-groups.md` | 类/方法存在性、参数与默认值 |
| `src/click/decorators.py` | `docs/options.md`、`docs/option-decorators.md` | decorator 调用、关键字参数 |
| `src/click/types.py` | `docs/parameter-types.md` | 参数类型名称、公开符号覆盖 |
| `src/click/testing.py` | `docs/testing.md` | 测试 API 调用示例 |

### 4.2 适合 Click 的 drift 类型

1. 文档调用了已删除或重命名的函数。
2. decorator 新增、删除或重命名关键字参数。
3. 参数默认值发生变化，教程仍描述旧默认值。
4. public API 已新增，但 API 索引和教程完全未提及。
5. CLI 示例命令可以执行，但实际输出与文档不一致。
6. 平台或依赖行为已变化，文档仍保留旧实现说明。

### 4.3 已确认的事实案例

#### 案例 A: 移除 Colorama 后文档仍描述旧行为

- Commit: <https://github.com/pallets/click/commit/a44e9f05edbfcea04b5ec91d4403976a50381c59>
- 涉及 `docs/utils.md`、`src/click/termui.py`、`src/click/utils.py`。
- 类型: 依赖/平台实现变化后文档未同步。

#### 案例 B: `multiple=True` 的语义说明错误

- Commit: <https://github.com/pallets/click/commit/8450d66f6e70dd2454b466af6f8008c5145eeb2e>
- 旧文档错误声称会多次调用底层函数，实际是一次传入 `tuple`。
- 类型: 参数行为描述与运行时事实不一致。
- Oracle: 使用 `CliRunner` 验证调用次数和参数形态。

#### 案例 C: context manager 示例缺少 `return self`

- Commit: <https://github.com/pallets/click/commit/e660d446d30d5714b6fd5e5b5cebac19db875a46>
- 类型: 可执行示例失效。
- Oracle: 执行 `with Repo(...) as repo`，修复前得到 `None`，修复后得到实例。

### 4.4 Click 的主要风险

- decorator 包装后的 CLI 参数不一定直接等于 Python 函数签名。
- autodoc 自动生成部分不是独立信源，不应重复报告 drift。
- 部分行为依赖终端、操作系统或 shell 环境。
- 文档有意使用简化签名时，不能把省略全部判成错误。

因此 MVP 应先做“明确声明冲突”，而不是要求文档完整复制代码签名。

---

## 5. 第二阶段靶子: Typer

仓库: <https://github.com/fastapi/typer>

Typer 适合验证系统能否从“符号和签名检查”升级到“可执行教程和 CLI 行为检查”。

### 5.1 主要价值

- Markdown 教程数量多。
- 包含完整 Python 源码和命令行输出。
- 可以检测 `typer.Option`、`typer.Argument` 和 `Annotated`。
- 文档示例与测试之间常有明确映射。
- 项目活跃，容易持续产生候选样本。

### 5.2 已确认案例

#### 参数名字错

- Commit: <https://github.com/fastapi/typer/commit/e531a859c4eba4c3f9ec53b637f4a21bc702fa3e>
- 文档将真实参数 `count` 写成不存在的 `counter`。
- Oracle: 签名/符号精确匹配。

#### 示例命令与展示输出不一致

- Commit: <https://github.com/fastapi/typer/commit/0440d9a7aeb2da997f91ffe5ddeb82f0336d51b2>
- 文档展示帮助输出，但命令没有传 `--help`。
- 修复同时增加了测试。
- Oracle: `CliRunner` 输出。

### 5.3 为什么不作为第一靶子

Typer 的最终用户 API 是由 Python 类型注解、Typer 和 Click 多层转换得到的。要覆盖最有价值的 drift，需要处理：

- `Annotated` metadata;
- Python 类型到 CLI 类型的映射;
- Typer 与 Click 包装层;
- 最终 CLI help 和运行输出。

这些能力适合第二阶段，不适合阻塞最小闭环。

---

## 6. 真实历史评测集设计

单纯人工修改文档制造 drift 容易极度“玩具化”。评测集应同时包含：

1. 人工可控 mutation；
2. 真实提交中的文档修复；
3. 能定位代码变化点与文档修复点的自然 drift 区间。

### 6.1 HTTPX: API 重命名的标准 drift 区间

仓库: <https://github.com/encode/httpx>

#### `ResponseClosed` → `StreamClosed`

- 代码改名: <https://github.com/encode/httpx/commit/9b8f5af7596ab2208375a4d26b5b585d51b82b01>
- 文档修复: <https://github.com/encode/httpx/commit/7d3a5347a9717169c00c73b71ba7c560e9a04443>

可以构造三阶段 ground truth:

```text
改名前：代码与文档一致
改名后、文档修复前：存在 drift
文档修复后：恢复一致
```

这是最标准的“代码先变、文档后补”真实样本。

其他 HTTPX 案例:

- 代理 key `"http"` 应为 `"http://"`: <https://github.com/encode/httpx/commit/782f507b634d58394c1e9043231f891644637638>
- `proxies` 向 `proxy` 迁移: <https://github.com/encode/httpx/commit/f8981f3d124f9b8db9073fd5c8afa11acb55a738>

### 6.2 Pydantic: 签名与 docstring drift

仓库: <https://github.com/pydantic/pydantic>

#### 已删除参数仍残留在 docstring

- Commit: <https://github.com/pydantic/pydantic/commit/080c741ecf4e113b9c7487de16ffbba5182f03bf>
- `apply_validators()` 签名已经没有 `field_name`，但 docstring 仍列出该参数。

这是非常适合确定性评测的样本:

```text
negative revision = fix commit 的父提交
positive revision = fix commit
oracle = 函数签名参数集合与 docstring Args 集合的差异
```

其他 Pydantic 案例:

- 示例返回类型应为 `Optional[str]`: <https://github.com/pydantic/pydantic/commit/f42bf86c01201ae959441022496f1146e5059083>
- `model_serializer` 示例缺少函数体: <https://github.com/pydantic/pydantic/commit/6671be7305e65dfafa6e6d58cc0efbb00aed3bc7>

### 6.3 Rich: 代码删除后文档残留

仓库: <https://github.com/Textualize/rich>

#### 已回滚功能仍留在文档中

- Commit: <https://github.com/Textualize/rich/commit/669b5006b3bbfe6fb023d76cda62c59773141cf5>
- `IterationSpeedColumn` 已在代码中回滚，但仍留在 progress 文档。
- 类型: 文档有、代码无。
- Oracle: 导入或 `hasattr` 检查。

其他 Rich 案例:

- docstring 引用不存在的方法: <https://github.com/Textualize/rich/commit/01d01ed5ee322f65ef5c333955570552c68a40d8>
- 示例遗漏 `Panel` import: <https://github.com/Textualize/rich/commit/97a7addc7fe33bc1b274cbee659da950789ae331>

### 6.4 Requests: 运行时语义与文档不一致

仓库: <https://github.com/psf/requests>

- Commit: <https://github.com/psf/requests/commit/e361622fae03f08b16c48a0a1414a718d9d45d25>
- 文档曾列出实际不会被抛出的 `URLRequired` 异常。
- 类型: 行为/异常语义 drift。
- Oracle: 针对无 scheme、非法 scheme 和非法 URL 运行异常测试。

这类样本适合作为困难集，因为纯签名比较无法发现它。

### 6.5 推荐的数据字段

每条历史样本至少记录：

```json
{
  "repository": "encode/httpx",
  "code_change_commit": "...",
  "drift_start_commit": "...",
  "doc_fix_commit": "...",
  "drift_end_commit": "...",
  "doc_path": "docs/exceptions.md",
  "doc_anchor": "Network Exceptions",
  "code_symbol": "httpx.StreamClosed",
  "drift_type": "symbol.rename",
  "oracle_type": "static_symbol_lookup",
  "expected_before_fix": "drift",
  "expected_after_fix": "consistent"
}
```

如果找不到独立的代码变化点，至少记录修复提交的父版本和修复版本，但必须人工确认父版本中的代码事实已经成立。

---

## 7. 现有工具与竞争格局

更准确的行业判断不是“没有人做 doc-code drift”，而是现有项目分别解决了不同子问题。

| 工具 | 已覆盖能力 | 未覆盖的关键部分 | 本项目中的定位 |
|---|---|---|---|
| Swimm | 显式代码绑定、Git-aware 同步、CI 告警 | 既有散文抽取、自动对齐、盲区与一般语义冲突 | 产品竞品 |
| Doc Detective | 执行 CLI/API/UI 文档步骤 | 签名、覆盖率、散文声明、符号对齐 | 可执行 detector/竞品 |
| pydoclint | docstring 参数、返回、异常与签名一致性 | 外部 Markdown、跨文件对齐、语义判断 | 强基线 |
| interrogate | docstring 是否存在及覆盖率 | 内容是否正确 | 覆盖率弱基线 |
| doctest | Python 示例输入输出 | 普通散文、签名、覆盖盲区 | 确定性执行基线 |
| Griffe | Python public API、参数、注解、别名、docstring | 外部文档声明和 drift 判定 | **核心依赖** |
| mkdocstrings | 从源码生成 API 文档 | 手写说明与示例的 drift | 生态适配对象 |
| Sphinx | autodoc、doctest、引用与构建告警 | 统一声明模型与语义 drift | 适配器/基线集合 |
| oasdiff | 两份 OpenAPI 的结构变化 | OpenAPI 与代码实现的一致性 | 契约 diff oracle |
| Specmatic | 结构化契约与 provider 实现一致性 | 任意 Markdown 和代码符号 | 契约子域竞品 |
| Optic | OpenAPI 与运行流量一致性 | 普通函数、配置、散文、盲区 | 运行时证据参照 |

### 7.1 Swimm

官网: <https://swimm.io/>

Swimm 最接近“持续文档”产品：文档创建时绑定代码片段或符号，代码移动和重命名后利用 Git 历史重新定位，无法同步时在 CI 中告警。

其主要边界是“显式绑定优先”。对于已有仓库中的普通 Markdown，它并不等价于：

- 自动提取任意文档声明;
- 自动建立文档段落到代码符号的关系;
- 检查新增 public API 是否没有文档;
- 判断行为散文是否与实现冲突。

本项目可借鉴：显式绑定、Git-aware relocation、PR 增量检查和人工确认工作流。

### 7.2 Doc Detective

仓库: <https://github.com/doc-detective/doc-detective>

Doc Detective 回答的是：“用户照着文档执行，步骤还能不能成功？”它可以执行 CLI、HTTP API 和浏览器操作。

本项目不应重写这类执行框架。更合理的做法是把它接成外部 detector，并区分：

- `execution_failed`: 步骤执行失败;
- `semantic_drift`: 文档声明和实现事实冲突;
- `environment_error`: 依赖、网络或凭证问题;
- `unknown`: 证据不足。

### 7.3 pydoclint

仓库: <https://github.com/jsh9/pydoclint>

它是 Python docstring 子域内最接近本项目确定性定位层的工具，支持参数、返回、yield、异常和类属性检查。

最适合用作：

- Python docstring detector 的 baseline;
- mutation ground truth oracle;
- 证明本项目不只是在重写 docstring lint。

### 7.4 Griffe

仓库: <https://github.com/mkdocstrings/griffe>

Griffe 建议作为 Python MVP 的代码事实底座。它已支持：

- 模块、类、函数、属性和别名;
- 参数 kind、默认值和注解;
- decorator、继承和导出;
- public API 判定;
- Google/NumPy/Sphinx docstring;
- JSON 输出;
- Git ref 间 public API breaking change 检查。

因此第一版没有必要用 tree-sitter 重做 Python 语义。推荐架构是让 Griffe 输出项目自己的通用 `CodeFact`，以后 tree-sitter provider 也输出同一 schema。

### 7.5 契约工具

`oasdiff`、Specmatic 和 Optic 分别覆盖：

- OpenAPI 版本之间的结构差异;
- 契约与 provider 实现是否一致;
- OpenAPI 与真实运行流量是否一致。

这些工具说明“契约 drift”已经是成熟子领域。本项目应将结构化契约交给现有确定性工具，把主要创新放在：

- 外部 Markdown 与源码符号的自动对齐;
- public API 文档覆盖盲区;
- 普通散文行为声明;
- 多种证据的统一融合。

---

## 8. 调整后的技术路线

### 8.1 Python MVP

```text
Repository discovery
        ↓
Griffe PythonFactProvider
        ↓
统一 CodeFact schema
        ↓
Markdown/docstring Claim extractor
        ↓
显式引用 + 精确名称 + FQN 对齐
        ↓
确定性 Structural Detectors
        ↓
DriftFinding + 证据锚点
        ↓
CLI 报告
```

第一版只做：

- public 函数/类存在性;
- 文档中的 Python 调用表达式;
- 参数名、数量和默认值;
- docstring 参数集合;
- public API 的文档覆盖;
- 明确无法对齐的返回 `unknown`。

暂时不做：

- embedding;
- GraphRAG;
- Multi-Agent;
- Memory;
- 多语言;
- 任意散文行为判断;
- 自动修改文档。

### 8.2 Detector 分层

#### Structural

- 符号存在性;
- 参数名和默认值;
- 返回和异常结构;
- public API coverage。

参考 Griffe 和 pydoclint。

#### Executable

- doctest;
- fenced Python code;
- CLI 命令与输出;
- HTTP/API/UI 步骤。

参考 doctest、pytest-doctestplus 和 Doc Detective。

#### Contractual

- OpenAPI;
- GraphQL;
- gRPC;
- MCP schema。

参考 oasdiff、Specmatic 和 Optic。

#### Semantic

- 前置条件;
- 副作用;
- 幂等性;
- 缓存行为;
- 异常条件;
- 状态转换;
- 文档间矛盾。

这一层才使用 LLM，而且必须在已对齐的声明-事实对上判断。

### 8.3 建议的数据契约

#### CodeFact

```json
{
  "symbol_id": "python:click.decorators:option",
  "fqn": "click.decorators.option",
  "kind": "function",
  "public": true,
  "parameters": [],
  "returns": null,
  "source_location": {},
  "content_hash": "..."
}
```

#### DocClaim

```json
{
  "claim_id": "docs/options.md#basic-value-options:code-2",
  "claim_type": "call_signature",
  "target_text": "click.option",
  "assertion": {},
  "doc_location": {},
  "extraction_source": "markdown_ast"
}
```

#### Alignment

```json
{
  "claim_id": "...",
  "symbol_id": "...",
  "method": "fqn_exact",
  "confidence": 1.0,
  "status": "aligned"
}
```

#### DriftFinding

```json
{
  "finding_id": "...",
  "category": "signature.parameter.unknown",
  "status": "drift",
  "confidence": 1.0,
  "claim_id": "...",
  "symbol_id": "...",
  "doc_location": {},
  "code_location": {},
  "evidence": {},
  "detector": "structural.signature"
}
```

---

## 9. 评测方案

### 9.1 数据集组成

建议第一版准备约 40~60 条样本:

| 来源 | 数量建议 | 用途 |
|---|---|---|
| Click 人工 mutation | 15~20 | 快速覆盖参数、符号、默认值和盲区 |
| HTTPX 历史样本 | 5~10 | API rename、配置和异常清单 |
| Pydantic 历史样本 | 5~10 | docstring 和类型签名 |
| Rich 历史样本 | 5~8 | 删除符号、错误引用和缺失 import |
| Click/Typer 可执行示例 | 5~10 | CLI 行为和输出 |

### 9.2 分层指标

不要只报告一个总准确率。至少分别报告:

- Claim extraction precision/recall;
- Alignment precision/recall;
- Structural drift precision/recall;
- Public API coverage recall;
- Executable example pass/fail agreement;
- Evidence location accuracy;
- Unknown/reject rate;
- 历史 drift 检出率;
- 人工 mutation sensitivity。

### 9.3 基线对比

| 基线 | 对比目的 |
|---|---|
| interrogate | “有 docstring”不等于“文档正确” |
| pydoclint | 外部 Markdown 和跨文件对齐是额外能力 |
| doctest | 示例通过不等于全部文档一致 |
| Griffe API diff | code-code 版本差异不等于 doc-code drift |
| Doc Detective | 可执行步骤只是文档声明的一部分 |

最有说服力的实验是:

1. `interrogate` 显示覆盖率很高;
2. `pydoclint` 没有 docstring 结构错误;
3. doctest 全部通过;
4. 本项目仍发现外部教程引用已删除 API、新 public API 没有任何文档、散文默认行为与实现冲突。

---

## 10. 推荐实施顺序

### 阶段 1: 最小闭环

- 使用 Griffe 提取 Click public API。
- 解析 Click Markdown 中的 inline code 和 Python fenced code。
- 建立精确名称/FQN 对齐。
- 检测不存在的符号和错误关键字参数。
- 输出带代码与文档位置的 CLI 报告。

### 阶段 2: 真实历史评测

- 导入 HTTPX rename 样本。
- 导入 Pydantic stale docstring 样本。
- 导入 Rich deleted-symbol 样本。
- 接入 pydoclint、interrogate 和 doctest 基线。
- 输出分层 precision/recall。

### 阶段 3: 可执行教程

- 加入 Typer。
- 解析并执行 CLI 示例。
- 比较命令、退出码和规范化输出。
- 区分 drift、环境失败和 unknown。

### 阶段 4: 语义层

- 从散文抽取行为声明。
- LLM 只处理已对齐的声明-事实对。
- 支持限制条件、副作用、异常条件和状态变化。
- 加入人工复核和 bad case 回流。

### 阶段 5: 工程化入口

- MCP Server;
- PR/CI 增量门禁;
- Git-aware rename;
- Memory 与 drift 历史;
- tree-sitter 多语言 provider;
- FastAPI 压力测试。

---

## 11. 最终定位建议

不建议继续使用:

> 业界没有人做 doc-code consistency。

建议改为:

> 业界已有工具分别解决代码片段绑定、可执行示例、docstring 结构校验和 API 契约一致性，但缺少一个面向既有代码仓库、统一完成文档声明抽取、自动符号对齐、双向完备性检查和语义 drift 判断的通用引擎。

更简短的项目介绍可以是:

> Swimm 保持显式绑定的代码文档同步，Doc Detective 验证文档步骤能否执行，pydoclint 检查 docstring 结构，Specmatic 验证 API 契约；Doc-Code Drift Agent 则扫描既有仓库中的结构化和非结构化文档，将声明自动对齐到代码事实，并检测冲突、过时和知识盲区。

---

## 12. 最终决策

| 维度 | 决策 |
|---|---|
| 首个开发靶子 | **Click** |
| 第二阶段靶子 | **Typer** |
| 历史 API drift 主样本 | **HTTPX** |
| 签名/docstring 样本 | **Pydantic** |
| 删除符号/失效示例样本 | **Rich** |
| Python 解析底座 | **Griffe** |
| 多语言解析底座 | tree-sitter，后置 |
| 确定性基线 | pydoclint + interrogate + doctest |
| 后期压力测试 | FastAPI |
| 核心差异化 | 统一声明模型、自动对齐、双向四态、证据融合、语义层 |

下一步最值得做的不是继续扩大调研，而是固定 Click 的一个 commit，完成:

```text
Markdown 函数调用声明
        ↓
Griffe Python 函数事实
        ↓
精确符号对齐
        ↓
错误关键字参数检测
        ↓
带双侧位置的 DriftFinding
```

这条链跑通后，再导入 HTTPX 的 `ResponseClosed → StreamClosed` 历史案例，证明检测器不仅能抓人工 mutation，也能发现真实开源项目中的文档漂移。
