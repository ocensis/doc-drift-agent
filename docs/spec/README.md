# Spec 索引

各 Stage 的功能规范与配套 spec-test。状态以本索引为准。

| Stage | 主题 | 状态 | 备注 |
|-------|------|------|------|
| 2 | [结构化检测硬化](stage-2-structural-hardening-spec.md) | Complete | 配套 [spec-test](stage-2-structural-hardening-spec-test.md) |
| 3 | [可执行/语义检测与修复](stage-3-executable-semantic-spec.md) | Complete | 配套 [spec-test](stage-3-executable-semantic-spec-test.md) |
| 4 | [入口与对照评测](stage-4-adapters-evaluation-spec.md) | Complete | 配套 [spec-test](stage-4-adapters-evaluation-spec-test.md)；另见 [Codex 对照运行设计](stage-4-codex-benchmark-run-design.md) |
| — | TypeScript check-only 支持（FR-008） | Complete（2026-07-20） | 实战驱动交付，未走独立 stage spec。已支持 `.ts`/`.tsx` 的只读检测；TSDoc claim、fence stub、TS 修复与 interface 成员寻址仍不在范围内 |

语义 Section 漂移检测（Stage 5）的规范与迭代记录基于非公开基准，不随本仓库发布。
