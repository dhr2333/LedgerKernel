## 改了什么

<!-- 一句话说明本次改动 -->

## 为什么

<!-- 关联 issue / ADR / 用户场景 -->

## 验证

- [ ] `PYTHONPATH=src python3 -m unittest discover -s tests -t .`
- [ ] `PYTHONPATH=src python3 scripts/e2e_ledger.py`（合成 fixture 闭环）
- [ ] 涉及写入/恢复/外部变化时跑过 `scripts/e2e_ledger.py --source <TestAssets>`
- [ ] 公共 API / CLI / 配置 / schema 变化已更新 README
- [ ] 安全边界或不可逆决策变化已新增 ADR

## 兼容性

- [ ] 草稿 schema / 契约 schema / `.ledger-kernel` 布局无变化
- [ ] 旧 validation / approval / journal 仍可读取；如有变化已写明迁移

## 安全

- [ ] 没有绕过 `Kernel` 的账本写入
- [ ] 没有自动批准/跳过审批路径
- [ ] 没有提交真实账本、`.ledger-kernel/`、token、私钥或个人信息
