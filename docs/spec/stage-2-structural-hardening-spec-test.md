# Stage 2 结构路径强化 – 测试 Spec

**对应技术 Spec**: `docs/spec/stage-2-structural-hardening-spec.md`
**Created**: 2026-07-14
**Completed**: 2026-07-15
**Status**: Implemented and locally verified
**Verification**: 232 pytest tests passed; Ruff passed; strict mypy passed for 49 source files; all 8 `structural-v1` offline cases passed with zero model and network calls

该状态记录当前自动化测试与评测套件的通过情况；SC-001～SC-094 继续作为 Stage 2 的验收合同。

## User Scenarios

### 主成功路径

**SC-001**: 无漂移的只读检查
- **Given** 一个包含唯一可对齐 Python 符号和同步文档声明的已配置仓库
- **When** 用户运行检查模式
- **Then** 运行状态等于 `clean`，finding 数量等于 0，模型调用数等于 0，版本控制工作树的路径集合、字节、mode 与 Git status 均不变；只允许 Git administrative state 中追加 run record

**SC-002**: 参数新增被单独分类
- **Given** 当前代码比文档声明多一个参数，其他结构事实一致且对齐唯一
- **When** 用户运行检查模式
- **Then** 恰好存在一个“参数新增”finding，该 finding 同时包含代码与文档证据，且不存在不透明的重复整签名 finding

**SC-003**: 参数删除被单独分类
- **Given** 当前代码比文档声明少一个参数，其他结构事实一致且对齐唯一
- **When** 用户运行检查模式
- **Then** 恰好存在一个“参数删除”finding，finding 中的参数身份等于被删除参数，模型调用数等于 0

**SC-004**: 参数顺序与种类变化可区分
- **Given** 一个符号同时存在参数顺序变化和参数种类变化，文档声明可唯一定位
- **When** 用户运行检查模式
- **Then** 结果包含每 symbol 恰好一个携带共同参数 before/after 完整序列的 `parameter_order_changed`，并为实际 kind 变化的参数各含一个 kind finding；两类均引用对应证据且排序固定

**SC-005**: 标注、默认值和必填性变化可区分
- **Given** 一个符号的三个不同参数分别具有仅标注变化、两侧默认值均存在但值不同、以及 `MISSING_DEFAULT` 与具体默认值之间的 presence 变化
- **When** 用户运行检查模式
- **Then** 结果恰好各包含一个 annotation、`parameter_default_changed` 和 `parameter_requiredness_changed` finding，每类 finding 的预期值和当前值均等于输入事实，且同一参数没有 default/requiredness 重复 finding

**SC-006**: 返回标注变化被检测
- **Given** 代码返回标注与文档声明不同，其他结构事实一致
- **When** 用户运行检查模式
- **Then** 恰好存在一个“返回标注变化”finding，finding 的 raw 与 normalized 代码侧/文档侧值均可直接与输入断言

**SC-007**: Markdown 结构声明得到确定性修复
- **Given** 一个传统 package public function/method 对应 exact-FQN heading 加单一 top-level Python ellipsis stub，且 code-derived、唯一对齐、精确锚定、来源版本未变化
- **When** 用户运行修复模式
- **Then** finding 终态等于 `fixed`，结果中的 applied 等于 `true`，文档声明等于当前代码结构，模型调用数等于 0

**SC-008**: Google docstring 参数与返回值漂移被分类
- **Given** 一个 AST 可证明、可精确定位的单一 plain literal Google-style docstring，其 `Args` 与 `Returns` 声明均与当前代码不一致
- **When** 用户运行检查模式
- **Then** 结果分别包含 docstring 参数与返回值 finding，且每项都包含 normalized value 和精确代码/docstring 证据；不存在任何 exception/`Raises` finding

**SC-009**: docstring 安全修复不改变业务结构
- **Given** 一个 code-derived、可唯一修复的 Google `Args` 或 `Returns` docstring finding
- **When** 用户运行修复模式
- **Then** finding 终态等于 `fixed`，docstring 内容等于预期内容，去除 docstring 后的抽象语法结构与修复前完全相同，其他 Python 字节均不变

**SC-010**: 删除符号产生残留文档 finding
- **Given** `HEAD` 中存在一个公开符号且当前工作树已删除该符号，未改动的 eligible 文档仍保留其声明
- **When** 用户运行检查模式
- **Then** 恰好存在一个“已删除符号仍被文档引用”finding，finding 同时引用变更前符号证据与当前文档证据

**SC-011**: 有效人工 alias 支持确定性重命名
- **Given** 用户通过 `alias add` 从已完成 run 建立旧符号到新符号的人工 alias，当前 HEAD 仍继承确认点、old object 可查询，repository identity、两侧 evidence 与 aligner version 均未变化，文档仍引用旧符号
- **When** 用户运行修复模式
- **Then** 旧文档声明唯一对齐到新符号，文档引用被更新为新符号，finding 终态等于 `fixed`

**SC-012**: 同文件多个不重叠 finding 全部成功
- **Given** 同一文档文件中有两个互不重叠、各自可唯一验证的 code-derived finding
- **When** 用户运行修复模式
- **Then** 两个 finding 的终态均等于 `fixed`，两个预期范围均被更新，文件中其余字节保持不变，运行状态等于 `fixed`

**SC-013**: 跨文件多个 finding 全部成功
- **Given** 两个不同文档文件各有一个可独立修复的 code-derived finding
- **When** 用户运行修复模式
- **Then** 两个 finding 的终态均等于 `fixed`，两个文件均只包含各自预期变化，运行状态等于 `fixed`

**SC-014**: 一个修复失败时保留另一个独立成功修复
- **Given** 两个独立 finding，其中一个候选修复验证通过，另一个候选修复验证失败
- **When** 用户运行修复模式
- **Then** 通过项终态等于 `fixed` 且其修改保留，失败项终态等于 `unresolved`、reason code 等于 `validation_failed` 且其原文恢复，运行状态等于 `partial`

**SC-015**: code-derived 修复与 design-derived 审批并存
- **Given** 一个可安全修复的 code-derived finding 和一个 design-derived finding
- **When** 用户运行修复模式
- **Then** code-derived finding 终态等于 `fixed`，design-derived finding 终态等于 `needs_approval`，审批请求数量等于 1，运行状态等于 `partial`

**SC-016**: 持久化运行记录与事件
- **Given** 一个可访问的 Git common state SQLite 和一个已识别仓库
- **When** 用户完成一次检查运行
- **Then** 恰好新增一条关联 repository/workspace 与 run id 的记录，事件 seq 从 1 连续且顺序恰为 `run_started,snapshot_captured,facts_collected,findings_detected,decisions_applied,run_finished`，最终状态、finding 数量和模型调用数与返回结果一致

**SC-017**: Git state 默认位置与显式 override
- **Given** 默认 `<git-common-dir>/drift-agent/state-v1.sqlite3` 可写，并另有一个不位于 worktree 的显式 `state_dir` 目录
- **When** 用户运行检查和修复
- **Then** 未 override 的 run 只写默认 DB，override run 只写 `<state_dir>/state-v1.sqlite3`；已有非目录路径返回 `failed/state_store.path`；两者都不改变 worktree，repair lock 始终使用同一 workspace key 且不随 `state_dir` 改变

**SC-018**: 人工 ignore decision 抑制完全匹配的告警
- **Given** 用户用 `decision add` 从已完成 run 创建 human-confirmed ignore，repository、symbol、normalized old/new、代码/文档 evidence 及 detector version 均匹配
- **When** 用户再次运行检查
- **Then** active findings 不包含该 finding，suppression audit 含 decision id/action/reason/actor/evidence key，且若无其他 active finding则运行状态等于 `clean`

**SC-019**: 人工 false-positive decision 抑制完全匹配的告警
- **Given** 用户对某 finding 作出 false-positive decision，所有绑定证据仍有效
- **When** 用户再次运行检查
- **Then** active findings 不包含该 finding，suppression audit 指向对应 false-positive decision，且若无其他 active finding则运行状态等于 `clean`

**SC-020**: Agent 历史不会自行抑制告警
- **Given** 某 finding 曾由 Agent 修复失败或被排序到较后位置，但不存在人工 ignore 或 false-positive decision
- **When** 相同当前证据再次产生该 finding
- **Then** 该 finding 仍出现在活跃 finding 列表中，且抑制 decision 数量等于 0

**SC-021**: 四类项目评测案例均可本地重放
- **Given** `structural-v1` manifest catalog 与规范冻结的 8-row case matrix 在 case ID、provenance/license、operation、coverage 和 expected oracle 上逐项一致
- **When** 用户在无网络和无模型服务条件下运行完整评测
- **Then** 四类项目的案例数均大于 0，每个案例都有明确通过或失败结果，外部网络请求数等于 0，模型调用数等于 0

**SC-022**: 评测结果具有确定性
- **Given** 相同评测版本、相同输入和相同配置
- **When** 用户连续运行两次完整评测
- **Then** 排除 run/finding/repository ids、时间、duration、PID、绝对路径与 lock generation 后，两次 normalized finding multiset、逐案 disposition/reason/status、changed bytes 和全部指标完全相同

**SC-023**: 评测报告可定位失败
- **Given** 评测集中其他案例已证明通过，且只有一个案例的预期 finding 与实际结果不一致
- **When** 用户运行完整评测
- **Then** 失败数等于 1，报告包含该案例身份、预期 finding、实际 finding，且汇总中的通过数加失败数等于案例总数

### 异常路径

**SC-024**: 重复代码事实阻止自动修复
- **Given** 同一符号身份对应两个代码事实且无法唯一选择
- **When** 用户运行修复模式
- **Then** 对应 finding 终态等于 `unresolved`、reason code 等于 `ambiguity.fact`，文档内容不变，applied 等于 `false`

**SC-025**: 重复文档声明阻止自动修复
- **Given** 一个代码事实对应两个同等候选文档声明且没有唯一对齐证据
- **When** 用户运行修复模式
- **Then** 两个候选均不被修改，对应 finding 终态等于 `unresolved`、reason code 等于 `ambiguity.claim`，无 patch attempt

**SC-026**: 相似名称不会被猜测为重命名
- **Given** `HEAD` 中的旧符号在工作树中表现为 delete+add，新增符号名称相似，但没有人工 alias
- **When** 用户运行修复模式
- **Then** 系统不建立旧到新的 alignment，旧文档引用不变，对应 finding 终态等于 `unresolved/ambiguity.rename`；只有 Git 明确 rename 的唯一结构对应或 active alias 可通过

**SC-027**: 来源版本变化使 alias 失效
- **Given** 一个通过 typed operation 建立且曾有效的 alias，但 history 不再继承确认点、old object 不可查询或 new/doc/aligner 任一绑定 evidence 已变化
- **When** 用户再次运行检查
- **Then** alias 不参与 alignment，结果标记 alias 已失效，相关残留文档 finding 正常出现

**SC-028**: 不完整删除范围保守拒绝自动删除
- **Given** 已删除符号仍被文档引用，但系统只能定位引用片段而不能证明完整声明范围
- **When** 用户运行修复模式
- **Then** 文档字节完全不变，finding 终态等于 `unresolved`、reason code 等于 `unsupported.incomplete_declaration`，applied 等于 `false`

**SC-029**: 不受支持的 docstring 风格不被改写
- **Given** docstring 使用 NumPy/Sphinx/mixed style，或 raw/f-string/拼接 literal，无法按单一 Google `Args/Returns` grammar 精确锚定
- **When** 用户运行修复模式
- **Then** Python 文件内容不变，finding 终态等于 `unresolved`，reason code 等于对应 `unsupported.docstring_style|literal`

**SC-030**: docstring 抽象语法保护失败
- **Given** 一个候选 docstring patch 会导致去除 docstring 后的抽象语法结构与原结构不同
- **When** 用户运行修复模式
- **Then** 候选 patch 被回退，Python 文件与运行前完全一致，finding 终态等于 `unresolved/validation_failed`

**SC-031**: 来源 hash 不匹配时不覆盖文档
- **Given** finding 生成后待改文档内容已由外部进程改变，使来源版本或预期原文不匹配
- **When** 系统尝试应用修复
- **Then** 外部内容保持不变，finding 终态等于 `unresolved`、reason code 等于 `precondition_changed`、applied 等于 `false`；没有 independent fixed group 时 run 为 `unresolved`，有时为 `partial`

**SC-032**: 最终快照变化使运行 stale
- **Given** 一个候选修复已通过逐项验证，但最终整体验证前代码或相关文档证据发生外部变化
- **When** 系统执行最终一致性验证
- **Then** 运行状态等于 `stale` 并覆盖普通 aggregate，相关非 fixed finding reason code 等于 `global_snapshot_changed`，所有可唯一回退的 Agent 修改不保留，外部修改保持不变

**SC-033**: repair group 引入任意新 finding 时回退该组
- **Given** 一个候选修复消除了原 finding 但在受影响 validation closure 内引入任意一个新的活跃 finding
- **When** 系统验证该 repair group
- **Then** 该组修改被回退，原 finding 终态等于 `unresolved/validation_new_finding`，不相关且已验证的 repair group 不受影响；若后者存在 run 为 `partial`

**SC-034**: 最终整体验证失败时不报告伪成功
- **Given** 各 repair group 的局部验证通过，但最终整体重检发现任一已保留修复不再满足一致性约束
- **When** 系统完成最终验证
- **Then** 可归因 failing groups 一次回退并将 finding 置为 `unresolved/final_validation_failed`，剩余组重验到 fixed point；稳定 snapshot 下无法唯一归因时所有 implicated groups 为 `unresolved/final_validation_failed`，运行结果按是否有 retained fixed group 为 `partial` 或 `unresolved`

**SC-035**: 重叠 patch 被识别为冲突
- **Given** 两个候选修复指向同一原始文件的重叠字节范围
- **When** 用户运行修复模式
- **Then** 两个候选均跳过并为 `unresolved/conflict.overlap`，不会同时应用，冲突范围外内容不变；没有其他 fixed group 时 run 为 `unresolved`

**SC-036**: design/contract finding 绝不自动写入
- **Given** 一个 truth 分类为 design 或 contract 的结构 finding
- **When** 用户运行修复模式
- **Then** applied 等于 `false`，目标文件不变，finding 终态等于 `needs_approval`，审批请求数量等于 1

**SC-037**: unknown truth finding 保持未解决
- **Given** 一个无法确定 truth 来源的结构 finding
- **When** 用户运行修复模式
- **Then** applied 等于 `false`，目标文件不变，finding 终态等于 `unresolved`，审批请求数量等于 0

**SC-038**: 路径逃逸与符号链接目标被拒绝
- **Given** 候选 patch 指向仓库外路径或通过符号链接间接指向非允许目标
- **When** 用户运行修复模式
- **Then** 非允许目标内容不变，applied 等于 `false`，运行状态等于 `failed`，reason code 等于 `unsafe_path`

**SC-039**: 锁获取超时不产生修改
- **Given** 同一 workspace 的 OS repair lock 已被另一运行持有超过默认 monotonic 5.0 秒
- **When** 第二个修复运行尝试获取锁
- **Then** 第二个运行状态等于 `failed/lock_timeout`，等待遵守 5.0 秒边界，且在超时前没有 target transaction write

**SC-040**: 异常后释放修复锁
- **Given** 一个修复运行已获得锁并在写事务中发生异常
- **When** 该运行结束后另一个修复运行立即请求同一仓库的锁
- **Then** 第一个运行的未提交修改被回退，第二个运行能在等待上限内获得锁

**SC-041**: 中断后释放修复锁
- **Given** 一个修复运行已获得锁并在验证期间收到可捕获的 `KeyboardInterrupt` 或 `SystemExit`
- **When** 随后启动另一个修复运行
- **Then** 发布点前 owned edits 被回退，OS lock 在 finally 释放，后续运行能在 5.0 秒内获得同一 workspace lock；本场景不声称 SIGKILL 自动回退

**SC-042**: 持久化状态不可访问时安全失败
- **Given** 默认或显式 SQLite 不可读写、integrity check 失败、schema 版本过新或 migration 失败
- **When** 用户运行修复模式
- **Then** 运行状态等于 `failed/state_store.*`，DB 不被自动删除/重建，版本控制工作树与运行前完全一致且无 target write

**SC-043**: 代码版本变化使人工 decision 失效
- **Given** 某 finding 有 active human ignore，但绑定的代码 evidence 或 normalized old/new value 已变化
- **When** 用户再次运行检查
- **Then** decision 不再抑制告警，该 finding 出现在活跃 finding 列表中，结果标记 decision 已失效

**SC-044**: 文档版本变化使人工 decision 失效
- **Given** 某 finding 有人工 false-positive decision，但绑定的文档来源版本已变化
- **When** 用户再次运行检查
- **Then** decision fingerprint 不再匹配、告警重新进入 active findings，结果标记 doc evidence mismatch

**SC-045**: detector 版本变化使人工 decision 失效
- **Given** 某 finding 有人工 decision，但当前 detector 版本不同于 decision 绑定版本
- **When** 用户再次运行检查
- **Then** decision 不再抑制告警，该 finding 按当前 detector 结果重新报告并获得不同 fingerprint

**SC-046**: 当前证据冲突时历史结论不覆盖事实
- **Given** 历史运行或 alias 建议某一对齐，但当前代码与文档证据证明该对齐不再唯一或不再成立
- **When** 用户运行修复模式
- **Then** 历史结论不参与自动修复，目标内容不变，相关 finding 终态等于 `unresolved`

**SC-047**: 不同仓库的持久状态严格隔离
- **Given** 两个 independent clones 具有相同符号名称和相同表面文本，但只有 clone A 存在人工 decision 或 alias
- **When** 用户分别检查仓库 A 和仓库 B
- **Then** 两者 repository/workspace identity 均不同，人工状态只影响 A，B 的 finding/alignment 与 fresh state 相同

### 边界、空值与并发

**SC-048**: 无变更输入
- **Given** 目标仓库相对 `HEAD` 的 index 与当前工作树均没有 eligible 代码或文档变更，也没有 eligible untracked 文件
- **When** 用户运行检查模式
- **Then** 运行状态等于 `clean`，finding 数量等于 0，validation 数量等于 0，目标仓库不变

**SC-049**: 无参数与无返回标注符号
- **Given** 一个无参数且无返回标注的公开符号，其文档也明确表示无参数和无返回声明
- **When** 用户运行检查模式
- **Then** 不产生参数、默认值、必填性或返回标注 finding，empty sequence 与 `MISSING_RETURN` 不等于 literal `None`

**SC-050**: 空值默认与缺少默认值严格区分
- **Given** 代码参数默认值为空值，而文档把该参数声明为无默认值
- **When** 用户运行检查模式
- **Then** 恰好产生一个 `parameter_requiredness_changed` finding，代码侧值等于字面量 `None`，文档侧值等于 `MISSING_DEFAULT`，且 `parameter_default_changed` 数量等于 0

**SC-051**: 各类特殊参数保持身份和顺序
- **Given** 一个符号同时含仅限位置、普通位置、可变位置、仅限关键字和可变关键字参数，文档中有一处种类或顺序漂移
- **When** 用户运行检查模式
- **Then** 参数按 exact name 匹配；实际 kind 变化每参数一个 finding，reorder 每 symbol 恰好一个携带共同参数 before/after 序列的 finding，其余参数不产生 finding

**SC-052**: 空 docstring 的保守处理
- **Given** 一个公开 function/method 存在空 docstring，代码结构要求的 `Args` 或 `Returns` 声明无法从中取得精确现有范围
- **When** 用户运行修复模式
- **Then** 系统报告 missing claim finding，Python 文件不变且 finding 终态等于 `unresolved/unsupported.literal`；系统不凭空生成整份 docstring

**SC-053**: 重复修复保持幂等
- **Given** 第一次修复已成功并且代码、文档、配置和人工状态均未变化
- **When** 用户再次运行修复模式
- **Then** 第二次运行 applied 等于 `false`，worktree diff 为空，active finding 为 0；相同 normalized input 的 fingerprint 与稳定排序不变

**SC-054**: 大于一个 finding 时不再受单项限制
- **Given** 一个运行包含三个互不冲突且均可安全修复的 finding
- **When** 用户运行修复模式
- **Then** 三个 finding 的终态均等于 `fixed`，不存在 `stage1_limit`；若多个 findings 共享同一 anchor/replacement，则只形成一个 group 和一个 patch

**SC-055**: 同仓库并发修复串行化
- **Given** 两个修复运行几乎同时针对同一 writable workspace 启动，且 5.0 秒上限允许后启动者等待
- **When** 两个运行都执行完成
- **Then** 任一时刻进入写事务的运行数不超过 1，最终文件等于某一完整串行执行顺序的结果，文件中不存在半应用内容

**SC-056**: 不同仓库并发修复互不阻塞
- **Given** 两个修复运行分别针对不同 workspace identity（包括同一 repository 的 linked worktrees）同时启动
- **When** 两个运行请求各自的独占锁
- **Then** 两个运行均能在等待上限内获得各自锁，且每个仓库只包含属于自身运行的修改

**SC-057**: 检查与修复并发时验证读取快照
- **Given** 修复运行正在同一仓库处理多个 patch group，同时启动一个检查运行
- **When** 检查读取仓库证据
- **Then** 前后 active writer 均为空、generation 未变且 evidence hashes 一致时才返回单一快照结果；任一条件不满足时 run 等于 `stale`，绝不报告混合 facts

**SC-058**: 回退不覆盖并发外部编辑
- **Given** Agent 已修改一个范围，用户随后修改同文件的另一范围，而 Agent 的验证最终失败
- **When** Agent 执行回退
- **Then** exact Agent replacement 可唯一定位时被恢复，不相交用户 edit 被映射并保留，最终文件同时满足这两个字节级断言

**SC-059**: 默认状态位于 Git common state
- **Given** 用户未显式指定持久化状态位置
- **When** 用户完成一次运行
- **Then** 运行记录只存在于 `<git-common-dir>/drift-agent/state-v1.sqlite3`，lock 只存在于 runtime root，版本控制工作树的路径集合与 Git status 不变

**SC-060**: 既有阶段 1 契约保持兼容
- **Given** 一个既有 exact-FQN 签名漂移案例和既有机器可读结果消费者
- **When** 用户分别运行检查和修复模式
- **Then** 默认 V1 JSON 由冻结的递归 `extra="forbid"` Stage 1 consumer 直接解析且 key/type/default/含义不变；显式 V2 保留 legacy 字段并提供 additive 细粒度字段；状态/退出码映射恰为 `clean|fixed -> 0`、`drift_found|partial|needs_approval|unresolved -> 1`、`stale|failed -> 2`，从不出现 `repaired`

**SC-061**: 结构运行始终为零模型调用
- **Given** 任一参数、默认值、符号、Google `Args/Returns`、删除、重命名或多 finding 结构案例
- **When** 用户运行检查、修复或评测
- **Then** 返回结果中的模型调用数等于 0，模型请求记录数等于 0，网络模型凭据未被读取或使用

**SC-062**: 评测汇总守恒
- **Given** 一个包含通过、检测失败、修复成功和保守拒绝案例的评测版本
- **When** 用户运行完整评测
- **Then** `TP=Σmin(expected[k],actual[k])`、`FP=|actual|-TP`、`FN=|expected|-TP`，passed+failed=total，repair_successes、conservative_rejections 与 zero-model/offline compliance 均等于逐案 exact fold

**SC-063**: 完整声明范围可安全删除
- **Given** `HEAD` 中的公开符号已从工作树删除，且未改动文档中存在唯一、完整、可验证的对应声明范围
- **When** 用户运行修复模式
- **Then** 该完整声明范围被删除，finding 终态等于 `fixed`，文档中范围外的所有字节保持不变

**SC-064**: 非重叠但前置条件不兼容的候选发生冲突
- **Given** 两个候选 patch 的字节范围不重叠，但它们对同一来源要求互不兼容的 base 或 expected-text 前置条件
- **When** 用户运行修复模式
- **Then** 两个候选均为 `unresolved/conflict.base|expected_text`，不会同时应用，且目标文件不包含混合前置条件下的结果

**SC-065**: 非重叠但验证相互影响的候选发生冲突
- **Given** 两个候选 patch 的字节范围不重叠，但应用任意一个都会改变另一个 finding 的 alignment 或 validation 结果
- **When** 用户运行修复模式
- **Then** 两个候选为 `unresolved/conflict.validation_dependency` 而不会同时应用，独立的其他 repair group 仍可继续并保留

**SC-066**: 普通验证失败后释放修复锁
- **Given** 一个修复运行已获得锁，某 repair group 的普通验证返回失败并完成该组回退
- **When** 该运行结束后另一个修复运行立即请求同一仓库的锁
- **Then** 后续运行能在默认 5.0 秒内获得 workspace lock，且前一失败 group 的修改不保留

**SC-067**: staged、unstaged、untracked、delete 与 path rename 进入同一基线闭包
- **Given** `HEAD` 后分别存在 eligible staged-only、unstaged、untracked、deleted 和 Git path-renamed 输入，当前工作树字节代表 after 状态
- **When** 用户运行检查模式
- **Then** 每种状态均以 `HEAD` before 与当前工作树 after 参与一次 closure，index 不形成第三套 truth；Git rename 仅在 old/new symbol 唯一结构对应时形成 rename alignment，其他 path rename 不猜测 symbol rename

**SC-068**: 代码变化扫描未改动文档
- **Given** 一个公开符号在 `HEAD` 与工作树之间变化，而引用它的 eligible 文档文件本身未变化
- **When** 用户运行检查模式
- **Then** old/new symbol identity 的并集驱动扫描全部 eligible current claims，并报告未改动文档中的对应 drift

**SC-069**: 文档变化解析未改动代码事实
- **Given** 一个 eligible 文档 claim 在 `HEAD` 与工作树之间变化，而其 exact-FQN 对应的 Python 文件未变化
- **When** 用户运行检查模式
- **Then** changed claim 与当前 eligible code fact 比较，结果不因代码路径未出现在 Git diff 中而漏检

**SC-070**: 检查运行的状态写入失败不改变目标
- **Given** 检查已完成证据收集，但 required state write 失败
- **When** 系统结束该检查运行
- **Then** 运行状态等于 `failed/state_store.write`，版本控制工作树的路径集合、字节、mode 与 Git status 均不变

**SC-071**: 修复运行的最终状态写入失败先回退
- **Given** 一个修复已写入并验证候选，但最终 required state write 在 rollback ownership 放弃前失败
- **When** 系统处理该失败
- **Then** 本次运行可证明拥有的修改在 journal/lock release 前被回退，运行状态等于 `failed/state_store.write`，且没有 finding 被伪报为 `fixed`

**SC-072**: Group-local 问题使用 unresolved 并允许部分成功
- **Given** 两个独立 repair groups，其中 A 验证 fixed，B 在 apply 前 source/expected-text 变化
- **When** 用户运行修复模式
- **Then** A 的修改保留且 finding 为 `fixed`，B 不覆盖外部内容且为 `unresolved/precondition_changed`，run status 等于 `partial`；若没有 A 则 run 等于 `unresolved`

**SC-073**: Run status precedence 固定
- **Given** normal aggregate、global final-snapshot change 与 infrastructure/rollback failure 的参数化组合
- **When** 系统聚合最终结果
- **Then** precedence 恰为 `failed > stale > normal aggregate`，group-local unresolved 永不单独产生 run-level stale/failed

**SC-074**: 默认 V1 与显式 V2 wire
- **Given** 同一个 Stage 1 exact-signature case 和一个产生细粒度字段的 Stage 2 case
- **When** 用户分别请求默认 JSON 与 `--output-version 2`
- **Then** 默认 JSON 无条件省略 `schema_version` 与全部 additive 字段，所有 finding 的 legacy `type` 均等于 `signature_drift`，并逐层通过冻结 V1 `extra="forbid"`/literal consumer；V2 含 `schema_version=2` 和全部 additive 字段/默认值，且两者 legacy 字段、status 与 exit 含义相同

**SC-075**: Python bounded support matrix
- **Given** UTF-8 传统 package 中参数化的 public sync/async module function 与 public class direct method（无 decorator、`@staticmethod` 或 `@classmethod`），以及 flat/namespace/re-export/overload/nested/dynamic/custom-decorator/private/kind-transition 反例
- **When** 用户运行检查
- **Then** 只为受支持直接声明按 `python-symbol-v1` module/owner/name/category identity 提取 facts；可隔离反例为 `unresolved/unsupported.package_layout|symbol_kind`，不可读编码/语法使 run `failed/provider.encoding|syntax`

**SC-076**: Markdown 与 Google docstring grammar
- **Given** top-level ATX exact-FQN heading+下一非空 block 单一 ellipsis stub、完整 FQN token、AST-proven 且缩进/field/continuation 唯一的 Google `Args/Returns` 正例，以及 intervening block、参数表、模糊文本、NumPy/Sphinx/mixed/raw/f-string/拼接/空 literal 反例
- **When** 用户检查并尝试修复
- **Then** 正例按精确 anchor 检测/修复且 description bytes 保持不变；无 annotation 的 Args field 只断言 presence，instance/class method receiver 被排除而 staticmethod 参数保留；反例不写入并给出稳定 `unsupported.*`；`Raises` 内容不产生 Stage 2 finding或 unsupported

**SC-077**: Canonical expression normalization
- **Given** 仅 whitespace、冗余括号、quote spelling 或 numeric underscore 不同的 expression 对，以及 identifier qualification、operator 或 constant 真正不同的对
- **When** detector 比较 annotation/default/return
- **Then** 前者 normalized AST 相同且不产生 finding，后者每 component 恰好一个 finding；整个过程不 import、不执行、不解析 alias 语义

**SC-078**: Fingerprint、粒度与排序稳定
- **Given** 相同 repository/symbol/evidence/detector 输入重复两次，并分别改变 normalized value、evidence hash、kind 或 detector version
- **When** detector 生成 findings
- **Then** 相同 material 产生相同 fingerprint 和顺序；任一绑定 material 变化产生不同 fingerprint；old 固定为 current claim、new 固定为 current code并使用 typed missing sentinel；参数按 exact name、order 每 symbol 一个、其他每 component 一个

**SC-079**: Decision add/list/revoke 与幂等
- **Given** 一个 persisted finding 与 human-confirmed actor/reason
- **When** 用户依次执行 decision add、重复 add、list、冲突 add、revoke 与重复 revoke
- **Then** 相同 add 返回同一 id，list 展示 active/audit，冲突 record 在 revoke 前被拒绝，revoke append-only 且重复为 no-op；所有 evidence 由 run record 推导而非 caller hash

**SC-080**: Symbol identity 变化使 decision 失效
- **Given** 一个 active decision，随后 symbol identity 改变但表面文本或文件 hash 可被构造成相似
- **When** 用户再次检查
- **Then** fingerprint 不匹配，decision 不抑制，finding 重新进入 active list，并记录 symbol mismatch

**SC-081**: Alias lifecycle 与 old evidence 保留
- **Given** 用户经 typed operation add/list 一个 alias，随后提交 rename 使 HEAD 前进但仍继承确认点
- **When** 用户修复旧文档引用，随后分别 revoke、rewrite history 或改变 new/doc/aligner evidence
- **Then** ordinary HEAD advance 下 stored old commit/blob 可查询且 alias 生效；revoke 或任一 lineage/evidence mismatch 后 alias 失效；重复 add/revoke 幂等，冲突 active alias 被拒绝

**SC-082**: 仅 suppression 的运行是 clean
- **Given** 唯一 finding 有完全匹配的 active ignore 或 false-positive decision
- **When** 用户运行检查
- **Then** active findings 为空、status 等于 `clean`、suppression audit 完整；revoke 后 finding 恢复并为 `drift_found`

**SC-083**: Repository/workspace topology
- **Given** independent clone、linked worktree、move、filesystem copy 与 re-init 的参数化仓库拓扑
- **When** 计算 identities 并查询 decision/alias/lock
- **Then** linked worktrees 共享 repository identity/SQLite 但 workspace identity/lock 不同；clone、move、copy、re-init 均不共享 memory 或 lock

**SC-084**: Identity collision fail closed
- **Given** 持久化 digest 与 full identity material 不匹配
- **When** 用户启动检查或修复
- **Then** run 等于 `failed/repository_identity_collision`，不复用 memory、不进入 target transaction，也不把冲突 lock 当作自身 lock

**SC-085**: Git-state DB、override、migration 与 corruption
- **Given** 默认 Git common state、显式 external override、受支持旧 schema、newer schema、corrupt DB 与 failed migration 的参数化输入
- **When** 用户启动运行
- **Then** 默认/override precedence 精确，旧 schema 原子迁移且保留 rows，newer/corrupt/migration failure 不 reset DB并返回 `failed/state_store.*`，worktree 不变

**SC-086**: Concurrent state writes 与 event 顺序
- **Given** 两个并发运行共享 repository SQLite，但使用短 transaction
- **When** 两个运行完成
- **Then** 两条完整 run records 均存在，每条 seq 从 1 连续、event taxonomy/order 与模式匹配且恰好一个 terminal event；SQLite 不把整个 repair 生命周期串行化

**SC-087**: Mid-run required state write failure
- **Given** repair 已保留一个 group edit，但后续 required event write 在 final record 前失败
- **When** 系统处理 failure
- **Then** 在 ownership journal 和 OS lock release 前回退所有可证明 owned edits，run 等于 `failed/state_store.write`，无 finding 伪报 fixed，不相交 external edit 保留

**SC-088**: 五秒 lock、owner 与 stale metadata
- **Given** workspace lock contention、显式 timeout override 及没有 OS holder 的 stale owner sidecar
- **When** 用户请求 repair lock
- **Then** 默认 timeout 精确为 monotonic 5.0 秒、override 有限且非负；owner 字段完整；stale sidecar 单独不阻塞，下一 acquisition 覆盖 owner 并递增 generation

**SC-089**: SIGKILL/crash 释放 OS lock
- **Given** repair process 在原子文件替换后被 SIGKILL，留下 owner/journal 和可能的 complete Agent replacement
- **When** 后续 run 请求同一 workspace lock并检查 recovery evidence
- **Then** OS lock 可取得；中断 run 没有 terminal fixed record；后续 run 把 current bytes 作为新 evidence 重新检测并只通过正常 CAS/validation 流程修改，外部重叠内容不被自动覆盖；不声称 SIGKILL 已自动 rollback 或 fixed

**SC-090**: Group keys、savepoint 与最终归因
- **Given** coalesced same-anchor findings、各 conflict kind、多个独立 savepoints，以及最终重检中可归因和不可归因 failing sets
- **When** planner/applicator/final validator 执行
- **Then** group id/order/conflict key 稳定；可归因 set 一次回退后其余组重验到 fixed point；不可归因 implicated groups 全部 unresolved，独立 retained groups 保留

**SC-091**: 重叠 external edit 阻止 inverse
- **Given** Agent replacement 后 external edit 触碰 owned replacement，使 inverse 无法唯一证明
- **When** group 必须回退
- **Then** current/external bytes 不被覆盖，finding 为 `unresolved/rollback_unavailable`、run 为 `failed`、`changes.applied` 排除 residual，V2 residual evidence 含 path/range/hash

**SC-092**: Evaluation catalog、provenance、license 与 cap audit
- **Given** `structural-v1` catalog 和一个分别篡改 case id、source revision、SPDX license、copied_bytes 或 file/byte cap 的副本
- **When** evaluator 预检 manifest
- **Then** 正常 catalog 与规范 8-row matrix 的 operation、coverage、status/disposition/reason 和 mutation oracle 逐项一致；HTTPX/Rich historical class cases 保守拒绝，Click multi case 覆盖受支持 function rename/delete；任一 provenance/license/copy/cap 篡改在执行前失败；每 case 不超过 16 files/64 KiB、全 dataset 不超过 64 files/256 KiB

**SC-093**: Evaluation oracle、isolation 与 projection
- **Given** 含 duplicate normalized keys、repair success、conservative rejection 及 deterministic replay 的 cases
- **When** 每案在 fresh repo/Git-state DB/runtime root 运行两次
- **Then** TP/FP/FN multiset 公式、case pass、repair/rejection counts 精确，排除非确定字段后的 projection byte-equal，case 间无 state 泄漏

**SC-094**: Evaluation coverage 与零模型报告
- **Given** 完整 `structural-v1` catalog
- **When** 用户离线运行全部 cases
- **Then** coverage union 含 parameter/default、rename、delete、Google Args/Returns、same/cross-file multi、partial、conflict、conservative rejection；每案和 aggregate 的 model_calls 均为 0、network calls 为 0、compliance 为 true

## Test Type Convention

- **单元测试**: 必须覆盖结构差异分类与空值边界、唯一对齐与 alias 有效性、人工 decision 失效、truth policy、finding 状态聚合、repair group 冲突与隔离、docstring 安全断言、锁身份与超时语义、评测预期值比较。理由：这些规则具有确定性输入输出，可在不依赖真实仓库进程的情况下穷举边界；现有项目已采用 `tests/unit` 分层并要求 strict 类型检查。（依据：`codebase-context.md`“可复用接口与测试基础设施”及“依赖与质量”）
- **集成测试**: 必须。使用临时真实版本控制仓库、真实文件系统、跨进程并发、持久化状态和用户入口覆盖同文件/跨文件事务、部分成功、stale、异常/中断回退、锁竞争、decision/alias 跨运行生效及四类评测重放；测试放入现有 `tests/integration`，用户级全链路兼容性放入现有 `tests/e2e`。理由：写入原子性、Git 前后快照、进程锁、持久状态和 CLI 契约无法仅靠单元测试证明。（依据：`codebase-context.md`“可复用接口与测试基础设施”）

## Not In Scope

- 异常/`Raises` drift、re-export、overload、flat/namespace package、class signature 与 class symbol rename/delete 的自动修复；`Raises` 在其他 Google 字段有效时被忽略，HTTPX/Rich historical class cases 只验收保守拒绝。
- doctest/pytest 可执行示例路径：属于阶段 3，不作为本阶段验收场景。
- 语义 detector、fast/strong 模型路由、预算和 repair attempt 上限：属于阶段 3，且结构路径必须保持零模型调用。
- MCP、pre-push/CI adapter 与 Codex 对照实验：属于阶段 4。
- 多语言、GraphRAG、embedding、向量库、daemon 和 Web UI：超出已确认的个人 Python 仓库产品边界。
- 独立的 repair-attempt、blind-spot 或 bad-case 持久实体：本阶段只要求运行、人工 decision 和必要 alias 的持久化；bad case 进入版本化评测集。
- 未经许可与体积审计的完整上游仓库复制：评测只验收最小、可审计、可重放素材。
