# LedgerKernel 设计文档

本目录是 LedgerKernel v0.1 与 v0.2 账户层的设计基线，也是规格、实施计划与 ADR 的**唯一权威**。库外笔记（Obsidian 设计库）不保留规范正文副本，只放项目状态与未冻结讨论稿。内容与代码在同一提交中演进；引用时请给出本目录下的路径。

## 文档

- [顶层设计摘要](design-summary.md)：定位、写入权、账户语义、撤销与审计边界。
- [v0.1 设计规格](v0.1-spec.md)：数据模型、草稿生命周期、Dry-run、提交事务、CLI/MCP。
- [v0.1 工程决策冻结与原型实施计划](v0.1-implementation-plan.md)：ADR-001～ADR-013、P0-A～P0-E、验收矩阵。
- [v0.1 实施完成记录](v0.1-implementation-report.md)：实现范围、验证结果、冻结选择与已知限制。
- [v0.2 范围建议](v0.2-scope.md)：账户语义优先、独立发展；4 周 dogfood 的写入指标与账户层候选（2026-11-16 冻结，方向校准修订 1；P0/P1 已落地）。
- [v0.2 账户层设计](v0.2-account-layer.md)：契约编辑、账户状态/对账、账户级审核（已实现）。
- [v0.2 账户层实施计划](v0.2-account-layer-plan.md)：工作包、文件映射、测试矩阵、实施顺序与实施结果（第 9 节）。
- [v0.2 账户层 dogfood 轮次报告](v0.2-dogfood-round2.md)：基线轮（2026-11-16）指标、摩擦点与 v0.2.x 性能修复。

## ADR

- [ADR-012：Dry-run 增量必须与提案逐项一致（防文本注入）](adr/ADR-012-delta-assertion.md)
- [ADR-013：区分“已知转移”与“外部变化”；批准可在严格条件下沿用](adr/ADR-013-known-transition.md)
- [ADR-014：engine fingerprint 不绑定 Python 运行时](adr/ADR-014-engine-fingerprint.md)
- [ADR-015：review digest 只覆盖被审核的交易内容](adr/ADR-015-review-digest.md)
- [ADR-016：草稿模板只做字段预填，不绕过审核](adr/ADR-016-draft-templates.md)
- [ADR-017：重复流水只做校验期告警，不阻断提交](adr/ADR-017-duplicate-warning.md)
- [ADR-018：对账是工作流事实，由审计事件记录并按日期推导状态](adr/ADR-018-reconciliation-fact.md)
- [ADR-019：契约编辑走“验证 + CAS + 原子替换 + 审计”](adr/ADR-019-contract-apply.md)
- [ADR-020：账户级审核是分组视图，不改变批准绑定](adr/ADR-020-account-level-review.md)

## 阅读顺序

1. 先读顶层设计摘要，确认“内核是自动化写入的唯一受控入口，用户保留最高编辑权”。
2. 再读实施计划，确认 v0.1 的冻结边界与验收项。
3. 需要实现细节时读设计规格；涉及增量断言/批准沿用再看 ADR。
4. 账户层（契约编辑、对账、账户级审核）读 v0.2 账户层设计、实施计划与 ADR-018/019/020。
5. 当前代码行为与文档冲突时，以仓库 `README.md`、代码测试和最新 ADR 为准；发现不一致请提交 issue/PR。

## 文档状态

这些文档描述 **v0.1 原型基线** 与 **v0.2 账户层（v0.2.0，2026-11-16）**，不是长期产品承诺。标记为 `[建议冻结]` 的事项已经在该原型中实现，但后续版本仍可能通过新 ADR 修订。正式发布前允许推翻或重写旧设计；修订须在仓库留下理由（ADR 或范围文档修订）。安全边界与已知限制以仓库 README 为准。
