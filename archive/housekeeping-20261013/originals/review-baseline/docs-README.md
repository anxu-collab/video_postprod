# 文档导航

| 需要做什么 | 文档 |
|---|---|
| 了解目录与日常入口 | [项目说明](../README.md) |
| 导入、配音、字幕校对、选镜、审片 | [自动后期 v2 操作手册](runbooks/auto-postprod-operator-v2.md) |
| 了解文本模型接入 | [Ark 文本 Agent 手册](runbooks/ark-text-agent-v1.md) |
| 查老周账号接入和历史制作记录 | [老周·天益清](accounts/laozhou-tianyiqing.md) |
| 找维护、验证或历史诊断脚本 | [脚本索引](../scripts/README.md) |
| 查本次整理与回退方式 | [2026-10-13 整理记录](maintenance/project-organization-20261013.md) |
| 查早期架构边界 | [上层 v1 模块历史说明](../../docs/modules/auto-postprod-v1/MODULE.md) |
| 查模块结构化记录 | [上层 module.yaml](../../docs/modules/auto-postprod-v1/module.yaml) |

历史设计、运行记录和实际状态需区分阅读。旧文档中的已通过、尚未部署或当前账号列表都带有当时的范围；本导航不把它们重新宣布为最新验收结论。账号登记以 [account_profiles.json](../config/account_profiles.json) 为准。

`state/deployment/` 保存本机部署证据，`jobs/` 保存任务记录；它们保留在磁盘，但被本目录 Git 忽略规则排除。
