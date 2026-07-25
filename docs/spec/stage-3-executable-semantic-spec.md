# Feature Specification: Stage 3 可执行与语义路径

**Created**: 2026-07-15
**Status**: Complete — executable validation/detection、deterministic constant-return semantic detection、provider-neutral/OpenRouter model boundary、model-assisted semantic repair 与 `stage3-v1` evaluation 均已实现
**Implementation Target**: `main`
**Verification**: Latest full pytest、Ruff 与 strict mypy gate passed；`structural-v1` 8/8 与 `stage3-v1` 10/10 passed；最终显式 live probe 连接 `deepseek/deepseek-v4-flash`，使用 24 prompt + 13 completion tokens、cost `$0.0000056`；两次实现探针合计 78 tokens / `$0.000010244`，均无自动 retry

## Goal

在不削弱 Stage 1/2 确定性、安全写入和兼容性合同的前提下，加入可执行示例验证，以及只处理已确定性对齐局部声明的语义检测与有界修复。

Stage 3 分五个可独立验收的切片：

1. **Executable validation/detection**：接入受控 doctest/pytest，建立运行预算、check finding 与验证回滚闭环。
2. **Deterministic semantic detection**：只比较唯一 exact-FQN 对齐、可由语法证明的局部常量返回值。
3. **Model boundary**：加入 provider-neutral `ModelClient`、OpenRouter strict structured-output adapter、真实 usage 账本与显式连接探针。
4. **Semantic repair**：把唯一对齐输入接入结构化 proposal、fast→strong routing 和最多两次局部 patch attempt。
5. **Evaluation hardening**：增加 executable/semantic 评测集、回归与成本报告。

## Compatibility Contract

- 默认 V1 和显式 V2 bundle 的 Stage 1/2 legacy 字段、状态、退出码与含义保持不变。
- executable slice 不引入 V3 wire schema。PASS/UNAVAILABLE 只通过 `ValidationResult` 和 `Usage` 表达；runner 确认的真实测试失败使用现有 `DriftFinding` 作为 legacy envelope。默认 V1 保留冻结的 `type=signature_drift` 八字段投影并在 `reason` 明示 executable failure，显式 V2 以 `kind=broken_example`、`reason_code=validation_failed` 精确区分。纯 Stage 1/2 输入的 V1/V2 字节语义不变；人工 decision MAY 抑制该 legacy finding 的展示，但 required FAILED receipt MUST 继续使运行保持 `drift_found`，不得降为 `clean`。
- semantic finding 使用显式 V3 `type=semantic_drift`，不得伪装成 legacy `signature_drift`。纯 legacy bundle 的 V3 除 `schema_version=3` 外与 V2 相同；默认 V1 与显式 V2 输出继续逐字段兼容。
- semantic detection 与 repair 是两个 mode-specific 显式 capability：`semantic_analysis` 只允许 `check`，`semantic_repair` 只允许 `repair`；CLI 均以 `--semantic` 启用。JSON CLI 必须同时使用 `--semantic --output-version 3`，单独请求 V3 不得启用语义能力；任何 opt-in semantic run（即使 clean 或 finding 已被 suppression）投影到 V1/V2 都必须 fail closed，避免能力静默降级。
- `RunRequest` 新增带安全默认值的 `budgets`、`semantic_analysis` 与 `semantic_repair`；旧调用方不传这些字段时行为不变，未知字段继续拒绝。
- 结构、docstring 和 executable 路径不得调用模型；未配置 `ModelClient` 时结构路径仍可完整运行。

## Run Budgets

默认预算固定为：

```json
{
  "max_patch_attempts_per_finding": 2,
  "max_model_calls_per_run": 4,
  "max_input_tokens_per_run": 20000,
  "max_validation_commands_per_run": 8,
  "timeout_seconds": 120
}
```

- patch attempt 上限按 finding 计算，其余预算按 run 计算。
- 任一预算必须有限且非负；patch attempt 上限固定在 1～2。
- 预算耗尽后不启动新的外部动作；已验证成功的独立 group 可以保留，其余 finding 使用 `unresolved/budget_exhausted`。
- `Usage` 必须记录真实模型调用、profile、token、验证命令与时长。
- 模型调用前的 input token 只能作为容量 reservation；公开 `Usage.input_tokens` 必须由 provider 响应回填。响应 schema 非法或本地校验失败时，也必须先记录已发生调用的真实 usage。

## Model Boundary Requirements

**Implementation status**: Complete；显式 probe 与 application semantic repair 共用同一个 provider-neutral、budgeted boundary。

- 公共 `ModelClient` 是 provider-neutral 的预算 facade，底层 provider 只实现非流式 `ModelTransport` protocol；request 明确 profile、schema、system/user prompt 和 output token 上限，response 明确 requested/actual model、request id、finish reason 与 usage，application 不直接暴露 raw transport。
- OpenRouter adapter 只允许官方 `https://openrouter.ai/api/v1/chat/completions`，使用 Bearer auth、strict JSON Schema 和 `provider.require_parameters=true`；禁用 redirect、环境代理、自动 retry、tools、plugins 与 response healing。
- provider HTTP 错误、HTTP 200 内 error、非唯一 choice、非 `stop`、非法 JSON object、缺失或不自洽 usage 全部 fail closed；若失败响应含 usage，必须由异常携带并在抛出前回填已知 token。OpenRouter 未返回官方 optional cost 时使用明确 `accounting_incomplete`，公开 cost 只累计已知费用且不得把未知费用宣称为真实零费用。错误对象和 CLI 不保留 key、prompt、provider message 或原始 model output。
- response body 在单一 absolute wall-clock timeout 下异步流式读取，解压后最多 1 MiB。base URL、model slug、timeout 和 API key header 字符必须本地校验；同步 facade 若处于已有 event loop 中必须在 reserve 前拒绝，避免嵌套 loop 或假 timeout。
- `.env` 不被应用隐式加载；用户必须显式使用 `uv run --env-file .env drift-agent model probe` 或 `uv run --env-file .env drift-agent repair --semantic --output-version 3`。probe 恰好发送一个固定、与仓库无关的小型请求，只输出安全状态与 usage。
- validation copy 排除所有名称以 `.env` 开头的文件/目录（`.env*`），验证子进程无论 `[validation].network` 值为何都不继承 provider credential/config。普通 check 与未启用 semantic capability 的 repair 保持零模型、零隐式网络；`structural-v1` 保持零模型，离线 `stage3-v1` 的 semantic cases 只使用 deterministic fake transport，绝不连接真实 provider。

## Executable Validation Requirements

- 命令源只允许 `drift-agent.toml` 的 `[validation].commands`；内置 allowlist 仅验证 launcher、module、flag 与显式目标，绝不从 Markdown、docstring 或模型输出生成命令。
- 首个切片接受 `python -m doctest`、`python -m pytest` 和 `pytest`；后两者规范化为当前解释器的 pytest runner，所有命令解析为 argv 并以 `shell=False` 执行。
- 禁止 shell 控制符、绝对目标路径和 `..` 路径逃逸；Python launcher 统一替换为当前解释器。
- validator 在排除 `.git`、所有名称以 `.env` 开头的文件/目录（`.env*`）、已知缓存和 symlink 的 disposable repository copy 中运行；安全 bootstrap 必须先加载 allowlisted runner，再加入副本 import path，且只构造最小 allowlist 环境，不得继承宿主 token、provider credential、proxy、指回源仓库的 `PWD`/`PYTHONPATH` 或 virtualenv 路径提示。该边界不宣称能够把恶意目标代码隔离为 OS/container sandbox。
- `[validation].network = false` 时，Python validator 必须通过隔离的 `sitecustomize` 拒绝 socket 连接；不得只依赖代理环境变量。
- 每个显式验证目标必须进入 run snapshot；此外，所有会暴露给 disposable validator 的普通文件必须形成完整 validation-input manifest。copy 必须逐项精确匹配该 manifest，依赖文件的新增、删除或字节变化都不得被忽略；group/final copy 前和发布前发现非 Agent-owned 输入变化时，不得报告 fixed。
- 每个 repair group 先通过 Stage 2 内部重检，再运行 required executable commands；任一失败只回滚该 group。
- 全部 group 完成后必须在最终快照上再运行一次 required commands；最终命令失败时保守回滚全部 retained groups，不报告伪成功。
- allowlisted doctest/pytest runner 的 exit 1 为 `validation_failed`；命令缺失、其他退出码、超时或环境不可用为 `validation_unavailable`；预算耗尽为 `budget_exhausted`。
- stdout/stderr 只保留有界诊断摘要，不进入 command shell，也不改变策略。
- 只要存在 configured commands，`check` 就运行全部去重后的命令，把它们视为全局 required oracle；结构 changed scope 为空、只有测试 target 变化，或代码变化没有修改显式 target 时都不得跳过，因为当前配置没有 dependency mapping。
- check-mode detector 首版要求每条命令恰好一个显式 target；多 target 命令仍可作为 repair gate，但 check 不从聚合输出猜测失败 anchor，而是返回 `validation_unavailable`。
- PASS 只记录 receipt；runner 确认的真实测试失败产生一个稳定 command-level `broken_example` finding 并回链 `ValidationResult.finding_ids`。compile、missing target、pytest exit 2～5、timeout 或环境不可用不产生 broken finding，而使 run 为 `unresolved`；预算耗尽使用既有 `budget_exhausted` 终态。
- doctest 与 pytest 都只有 exit 1 表示 runner 确认的测试/示例失败；pytest exit 2～5、doctest exit 2、负数或其他退出码属于中断、内部/用法错误或未收集测试，必须归为 `validation_unavailable`。finding evidence 只定位配置文件和唯一 target 文件，不声称定位具体配置行、doctest block 或 test function。
- check 在 snapshot 捕获后、finding 持久化前执行，不进入 repair transaction、不获取写锁、不修改源仓库。disposable copy 内的完整 validation-input manifest 必须与 captured snapshot 精确一致，执行后仍需复核完整 snapshot、Git scope 和 writer generation。

## Semantic Detection and Repair Requirements

**Implementation status**: Complete；检测与修复共享同一份冻结、唯一对齐证据，模型不能重新搜索或选择 symbol。

### Deterministic Constant-return Detection

- 入口只允许 `RunRequest(mode=check, semantic_analysis=true)`；CLI JSON 必须显式选择 V3。该路径只读、零模型调用、零 token，并复用 check snapshot、truth policy、Memory 与最终发布检查。
- Markdown provider 只识别 exact-FQN 标题、已被结构 provider 证明完整的 Python signature fence，以及紧随其后的一行 `Returns \`<literal>\`.` 或 `Always returns \`<literal>\`.`；普通散文、模糊标题、跨段文本和近似措辞一律不推断。
- semantic literal 必须无包装、无注释、无隐式拼接：`None`、`bool`、非负 `int` 与 `str` 必须各自是一个 Python token；负整数只额外允许“无空白的单个 `-` + 一个整数 token”的精确形态。整数整体必须处于 signed-64 范围，`str` 只含 Unicode scalar value 且可 UTF-8 canonicalize，并使用显式类型标签，因此 `True` 与 `1` 不相等。已匹配 grammar 但不支持的 literal 形成稳定 unavailable，而不是降级猜测。
- code fact 只接受同步 function/method，移除可选首行 docstring 后必须恰好只有一条 `return`；返回值只允许受支持的 `ast.Constant`，或操作符恰为 `USub`、operand 恰为整数 `ast.Constant` 的负整数形态。async、其他表达式、名称、分支、多语句或无返回值均不构造行为事实。
- claim、current `CodeFact` 与 `return.literal` fact 必须按 exact FQN 唯一对齐。缺失、claim/fact 歧义或不支持输入形成 required `semantic_alignment` unavailable，使 run 为 `unresolved`。
- direct assertion 不一致产生 `semantic_direct_mismatch`；`Always returns` 不一致产生 `semantic_over_promise`。finding 携带类型化 old/new value、精确 UTF-8 byte evidence、detector version 与独立 semantic fingerprint。
- code-derived finding 保持 detected；design/contract 进入人工审批；unknown 保守 unresolved。人工 decision 可抑制 finding，但 capability marker 仍阻止 V1/V2 序列化；任一 doc/code evidence 变化使旧 decision 失效。
- semantic opt-in 必须冻结所有实际扫描的 Markdown/Python 候选，即使某文件没有产出 claim/fact/issue；structural declaration 与 semantic sentence 的两次读取必须绑定同一 source hash。同路径证据 hash 冲突直接使 run stale，禁止 last-write-wins，并把非 fixed finding 转为 `unresolved/global_snapshot_changed`。当前 `drift-agent.toml` 相对 HEAD 的变化会重新分析新配置覆盖的全部 semantic claim，避免 truth/include/root 的 config-only 变化产生假 clean。

### Model-assisted Semantic Repair

- semantic repair 只接收已经由确定性 provider 唯一对齐并保存到 run state 的 `CodeFact + DocClaim`；不得搜索全库、猜测 symbol 或扩张 scope。ambiguity、alignment 不可用、非 code-derived truth 或被 suppression 的 finding 在模型调用前停止。
- 首版只处理 code-derived 局部散文的 direct mismatch/over-promise；design、contract 和 unknown 继续分别进入审批或保守拒绝。
- semantic repair 复用 provider-neutral `ModelClient` 预算 facade；extra-forbid 的 `SemanticRepairProposal` 只允许 `decision`、单个 literal `replacement_text`、`confidence` 与有界 `rationale`。path、span、command、diff 和 executable code 不属于输出 schema。
- 第一次调用固定使用 fast profile；fast 低置信时在写入前升级 strong，fast 第一次 patch 验证失败时先回滚该 attempt，再以 strong 进行第二次且最后一次尝试。不得直接 strong，也不得从 strong 返回 fast。
- 单个 finding 最多两次 patch attempt；invalid structured output 最多触发一次 schema-only retry，retry 可以发生在任一 profile、计模型调用与 token，但不计 patch attempt。第二次 schema 失败直接 unresolved。
- 模型不接收整个仓库，不产生命令，不直接写文件；patcher 只把经本地 parser 证明等于确定性 code fact 的单个 literal 写入 provider 冻结的 Markdown anchor，并继续执行 source hash、expected text、truth policy 和 workspace transaction guard。
- 任何语义 patch 都必须在同一 workspace lock/transaction 内通过 semantic finding 重检、无新增 finding、required executable validation、最终 closure、最终 required commands 和发布前 snapshot validation；失败 attempt 精确回滚，第二次验证失败后 unresolved/abstain，不进行第三次尝试。
- 自动写权限仍限 Markdown 和 AST 明确认定的 docstring 字符串；业务 Python AST 永远只读。

## Evaluation Requirements

- 冻结 versioned `stage3-v1` dataset 共 10 案：passing/failing doctest、passing/failing targeted pytest、timeout、unavailable、budget exhaustion、fast success、strong escalation、两次失败后 abstain。
- executable cases 的模型调用必须为 0；semantic cases 必须精确断言调用次数、profile、token、attempt 与最终字节。
- 每案使用全新仓库、state DB、runtime lock 和模型 fake；默认离线，禁止真实模型或网络服务成为测试依赖。
- Stage 1/2 的 `structural-v1` 8 个案例和全部 legacy serializer 测试必须继续通过。
- 冻结结果为 10/10：3 个 semantic opportunity 的 `repair_success@1=1/3`、`repair_success@2=2/3`、abstention correctness `1/1`，fast/strong route ratio 分别为 `3/5` 与 `2/5`；总计 5 model calls、35 input tokens、15 output tokens、5 validation commands、50,000 nano-USD known cost，且 executable zero-model、offline 与 model-script compliance 全部为 true。

## Not in Scope

- MCP、pre-push/CI adapter、SARIF、PR comment 和 Codex 对照实验属于 Stage 4。
- embedding、向量检索、全库语义搜索、多语言、daemon 和 Web UI 不进入当前产品边界。
- 模型不得自动修改业务代码；需要改代码或契约时继续输出 `ApprovalRequest`。
