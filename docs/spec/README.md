# Spec 索引

各 Stage 的功能规范与配套 spec-test。状态以本索引为准。

| Stage | 主题 | 状态 | 备注 |
|-------|------|------|------|
| 2 | [结构化检测硬化](stage-2-structural-hardening-spec.md) | Complete | 配套 [spec-test](stage-2-structural-hardening-spec-test.md) |
| 3 | [可执行/语义检测与修复](stage-3-executable-semantic-spec.md) | Complete | 配套 [spec-test](stage-3-executable-semantic-spec-test.md) |
| 4 | [入口与对照评测](stage-4-adapters-evaluation-spec.md) | Complete | 配套 [spec-test](stage-4-adapters-evaluation-spec-test.md)；另见 [Codex 对照运行设计](stage-4-codex-benchmark-run-design.md) |
| — | TypeScript check-only 支持（FR-008） | **Complete**（2026-07-20，PR #2） | 实战驱动交付，未走独立 stage spec；范围与边界记录于[问题台账](../field-reports/ISSUES.md) FR-008 与 [field report](../field-reports/2026-07-20-customer-agent-react-refactor/report.md)。后续边界（TSDoc claims、fence stub、TS repair、interface 成员寻址）如立项归入新 stage |
| 5 | [语义 Section 漂移检测（基准驱动优化）](stage-5-semantic-section-drift-spec.md) | **Draft** | 配套 [spec-test](stage-5-semantic-section-drift-spec-test.md)；基准 `evals/datasets/field/react-refactor-v1`，验收线见 spec |

演进主线与跨 stage 问题追踪见 [field-reports/ISSUES.md](../field-reports/ISSUES.md)；基准资产、成绩历史与逐次迭代记录见 [docs/evals](../evals/README.md)。
