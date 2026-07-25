# Stage 3 可执行与语义路径 – 测试 Spec

**对应技术 Spec**: `docs/spec/stage-3-executable-semantic-spec.md`
**Created**: 2026-07-15
**Status**: Complete — SC3-001～SC3-020、SC3-ED-001～SC3-ED-006、SC3-SD-001～SC3-SD-006 与 SC3-MC-001～SC3-MC-006 全部实现
**Verification**: Latest full pytest、Ruff 与 strict mypy gate passed；`structural-v1` 8/8 与冻结的 `stage3-v1` 10/10 passed；最终显式 live probe 连接 `deepseek/deepseek-v4-flash`，使用 24 prompt + 13 completion tokens、cost `$0.0000056`；两次实现探针合计 78 tokens / `$0.000010244`，均无自动 retry

## Executable Slice

**SC3-001：默认预算兼容**
- 旧 `RunRequest` 不传 budgets 时得到冻结默认值；非法负数、非有限 timeout、零次 patch attempt 和超过两次 attempt 被拒绝。

**SC3-002：安全命令解析**
- `python -m doctest`、`python -m pytest` 与 `pytest` 被规范化为当前解释器 argv；shell 控制符、非 allowlist module、绝对路径与 `..` 被拒绝，且始终 `shell=False`。

**SC3-003：运行环境隔离**
- validator 将 bytecode、pytest cache、HOME/cache 与 temp 写入临时目录；network=false 时 socket 连接被拒绝，目标 worktree 不出现 cache artifact。

**SC3-004：通过的 doctest 保留修复**
- repair group 内部重检和 doctest 都通过时，finding 为 `fixed/validated`，命令结果为 passed，usage.validation_commands 精确增加。

**SC3-005：失败的 doctest 回滚当前组**
- runner 确认 doctest 失败时当前 group 回滚为 `unresolved/validation_failed`，其他已验证 group 不受影响。

**SC3-006：pytest 验证与诊断**
- targeted pytest 通过/失败均返回有界、可定位的 ValidationResult，且不把输出当作命令或策略。

**SC3-007：不可用与超时**
- module、显式目标、隔离工作区不可用或 timeout 时为 `unresolved/validation_unavailable`，不保留未验证 patch，run 本身不因普通工具不可用而伪报 failed。

**SC3-008：验证命令预算**
- 达到 max_validation_commands_per_run 后不启动下一命令，未验证 group 为 `unresolved/budget_exhausted`，usage 不超过上限。

**SC3-009：最终组合验证**
- 最终 required command 失败时保守回滚全部 retained groups，不报告 fixed。

**SC3-010：Stage 2 回归**
- commands 为空时不运行外部验证，默认 bundle、事件顺序、V1/V2 serializer、8 个 structural-v1 case 与既有状态保持不变。

## Check-mode Executable Detection

**SC3-ED-001：配置驱动的唯一 target provider**
- provider 只接收 `[validation].commands`，对 allowlisted command 去重并要求恰好一个显式 target；Markdown、docstring、stdout 和模型输出不能生成命令或扩张 scope。命令是全局 required oracle，结构 scope 为空或只有测试 target 变化时仍运行。

**SC3-ED-002：PASS 与真实失败**
- PASS 只产生 `ValidationResult(PASSED)`；doctest/pytest exit 1 产生一个稳定 `broken_example` finding，failed receipt 精确回链该 finding，doctest 与 targeted pytest 都覆盖；其他退出码不产生 finding。

**SC3-ED-003：不可用与预算**
- compile error、missing/symlink/multi target、timeout、pytest exit 2～5 或环境不可用不伪造 drift，run 为 `unresolved`；命令预算为零时 subprocess 不启动且 usage 不增加。

**SC3-ED-004：只读与 snapshot 一致性**
- check 不获取写锁、不创建 repair transaction，cache/temp 只进入 disposable copy；copy 的完整 validation-input manifest、执行后 source snapshot、Git scope 和 writer generation 任一不一致均为 `stale`，包括 validator 依赖文件的新增、删除或字节变化。

**SC3-ED-005：V1/V2 与 Memory 兼容**
- 默认 V1 finding 仍是冻结八字段 `signature_drift` legacy envelope 且 reason 明示 executable failure；V2 以 `kind=broken_example` 区分。finding fingerprint、持久化和 check 六事件 grammar 保持稳定；target/config evidence 变化不改变 executable symbol identity，使旧 decision 按 evidence mismatch 失效。decision 即使抑制 legacy finding，required FAILED receipt 仍使 run 为 `drift_found`。

**SC3-ED-006：零模型调用**
- executable check 的 PASS、FAIL、UNAVAILABLE 和 budget cases 均精确断言 `model_calls=0`、`input_tokens=0`，相同命令、target bytes 和 detector version 得到相同 finding identity。

## Deterministic Semantic Detection

**SC3-SD-001：显式 opt-in 与 V3 能力门**
- 默认关闭且不改变 legacy 行为；仅 `check` 接受 semantic analysis。JSON CLI 要求 `--semantic --output-version 3`，V3 本身不隐式启用；semantic run 对 V1/V2 fail closed，包括 clean 与 finding 被 suppression 的情况。

**SC3-SD-002：严格 claim grammar 与精确 anchor**
- 只识别 exact-FQN 标题、完整 signature fence 后紧邻的一行 `Returns \`literal\`.` / `Always returns \`literal\`.`；非负 scalar 必须是单一 token，负整数只允许“无空白的单个 `-` + 一个整数 token”的精确形态；拒绝 `+1`、`- 1`、`-(1)`、`--1`、`-True`、其他 unary expression、注释、隐式字符串拼接、超出 signed-64 的整数和孤立 Unicode surrogate，并覆盖 signed-64 两端、UTF-8、CRLF、普通散文近似措辞、unsupported literal 与 byte anchor。

**SC3-SD-003：可证明的常量 code fact**
- 可选 docstring 后恰好一条可 canonicalize 的 scalar return 才形成内部行为事实；返回值只允许受支持的 `ast.Constant`，或操作符为 `USub`、operand 为整数 `ast.Constant` 的负数形态。覆盖 `None`、bool、signed-64 int 两端、UTF-8 str，以及 async、名称、其他 unary/算术表达式、分支、多语句、超范围整数和孤立 surrogate 拒绝；类型标签保证 `True != 1`。

**SC3-SD-004：唯一对齐与检测结果**
- exact FQN 的 claim、current symbol 和 `return.literal` fact 必须各自唯一；direct/always mismatch 分别形成稳定 typed finding，相等时 clean；缺失、歧义和已识别但不支持的输入形成 required unavailable 并使 run 为 `unresolved`。

**SC3-SD-005：truth、Memory 与 snapshot**
- code-derived/design/contract/unknown 分别走既有 detected/approval/unresolved policy；semantic finding 可被人工 decision 抑制，doc evidence 变化会稳定失效；code-only、doc-only 与 config-only truth change 都能进入正确 closure。所有扫描候选（含零 claim/fact 文件）的 provider 间改写或同路径 evidence hash 冲突必须 stale，相关 finding 必须转为 unresolved，最终 snapshot 变化不得发布旧结论。

**SC3-SD-006：只读与零模型调用**
- semantic detection 不进入 repair transaction，不修改工作区；PASS、finding、unavailable、truth routing 与 suppression cases 都精确断言 model/token/validation-command 为 0，并验证 V3 wire 与稳定 fingerprint。

## Model Client Connectivity

**SC3-MC-001：显式配置与 provider-neutral contract**
- 未显式提供 key/model 时安全失败；配置对象 mask key，fast/strong profile 与 structured request/response 不依赖 OpenRouter wire shape，`.env` 不被 application 自动加载，只有进程环境中的显式配置与 opt-in semantic repair 才能启用 provider。

**SC3-MC-002：严格 OpenRouter 请求**
- 恰好一次 POST 到官方 `/api/v1/chat/completions`，Bearer auth、非流式、temperature=0、reasoning disabled、strict JSON Schema、`require_parameters=true` 与 `data_collection=deny` 精确断言；无 redirect/proxy/retry/tool/plugin，且不加入 probe 非必需的 seed。

**SC3-MC-003：fail-closed 响应与错误脱敏**
- 覆盖 400/401/402/429/5xx、absolute wall-clock timeout、redirect、HTTP 200 error、choice error/数量、非 stop、非法 JSON object、缺失/不自洽 usage 和超大响应；CLI 不输出 provider detail、prompt 或 key。

**SC3-MC-004：真实 usage 账本**
- reservation 不进入公开 Usage；provider 返回后记录真实 prompt/completion token 与 cost。本地 schema 校验、非 stop 或结构化内容失败但 provider 已返回 usage 时，已知 usage 仍先持久计入；实际 input 超出 reservation 时同样先记账，单个 reservation 不能重复回填。缺失 optional cost 时仍记录已知 token 并明确 `accounting_incomplete`，费用字段只表示已知 subtotal，不伪造完整零费用。

**SC3-MC-005：credential 与 dotenv 隔离**
- 所有名称以 `.env` 开头的文件/目录（`.env*`）不进入 disposable validation copy 或 validation-input manifest；无论 validator network 开关为何，provider key/model/base/timeout 均不继承。离线 evaluation 清空 provider 环境。

**SC3-MC-006：显式最小真实探针**
- 默认测试只用 MockTransport/fake；单独执行的 probe 不读取仓库，返回 connected、实际 model、request id、token 与 cost，并且只产生一次可审计模型调用。

## Model-assisted Semantic Repair

**Implementation status**: Complete；application repair 已复用 provider-neutral model boundary，并在同一 workspace transaction 中完成有界 proposal、routing、重检与回滚。

**SC3-011：只接收唯一对齐输入**
- `semantic_repair` 只允许 repair mode；CLI 使用 `repair --semantic --output-version 3`。ambiguity、unknown truth、非 code-derived finding 或未对齐文本不调用模型，并以稳定 reason code 停止；V1/V2 对任何 opt-in semantic repair fail closed。

**SC3-012：fast 首次成功**
- extra-forbid proposal 只含 decision、单 literal replacement、confidence 与 bounded rationale；fast 返回合法高置信 replacement 且验证通过时只调用一次模型、只产生一次 patch attempt。

**SC3-013：受控 strong 升级**
- fast 低置信或第一次 patch 验证失败时才调用 strong；验证失败的 fast attempt 先回滚。直接 strong、strong 后回到 fast、第三次 attempt 或 scope 扩张均被拒绝。

**SC3-014：预算耗尽**
- model call、input token、wall-clock 任一预算耗尽后不再调用模型，已有独立 fixed group 保留，其他 finding 为 budget_exhausted。

**SC3-015：模型 schema 错误**
- 全 finding、跨 profile 最多允许一次 schema-only retry；仍非法则 unresolved，且两次响应都计 model call/token、但不计 patch attempt。

**SC3-016：模型不能产生命令或业务代码写入**
- 带 extra command/path/span、跨 anchor replacement、非目标 literal、Python executable span 或 repository escape 的响应在落盘前被拒绝；合法写入位置只能来自冻结的 Markdown literal anchor。

**SC3-017：两次验证失败后 abstain**
- 每次 patch 都经过 semantic 重检、无新增 finding、required commands、最终 closure/snapshot gate；第二次语义 patch 仍失败时回滚 Agent bytes，finding unresolved，不进行第三次尝试。

## Evaluation and Quality Gate

**SC3-018：离线确定性评测**
- 冻结 `stage3-v1` 共 10 案，其中 7 个 executable cases 为零模型调用，3 个 semantic cases 使用 deterministic fake transport；每案使用新 repo/state/lock，相同输入的 normalized projection byte-equal，10/10 通过。

**SC3-019：成本与路由指标**
- aggregate 精确报告 `repair_success@1=1/3`、`repair_success@2=2/3`、abstention correctness `1/1`、fast route `3/5`、strong route `2/5`；总计 5 model calls、35 input tokens、15 output tokens、5 validation commands 与 50,000 nano-USD known cost。executable zero-model、offline 和 model-script compliance 均为 true。

**SC3-020：全量门禁**
- 最新全量 pytest、Ruff、strict mypy、`structural-v1` 8/8 和 `stage3-v1` 10/10 全部通过，Git worktree 除预期文档/源码/评测改动外无运行产物。
