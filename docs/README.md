# 文档导航

| 需要做什么 | 文档 |
|---|---|
| 了解目录与日常入口 | [项目说明](../README.md) |
| 导入、配音、字幕校对、选镜、审片 | [自动后期 v2 操作手册](runbooks/auto-postprod-operator-v2.md) |
| 了解当前口播链、模型与 Skill 来源 | [口播文本链说明](runbooks/ark-text-agent-v1.md) |
| 找维护、验证或历史诊断脚本 | [脚本索引](../scripts/README.md) |
| 查项目整理与回退 | [2026-10-13 项目整理记录](maintenance/project-organization-20261013.md) |
| 查脚本目录整理与回退 | [2026-10-13 脚本目录整理记录](maintenance/directory-restructure-20261013.md) |

历史设计、运行记录和实际状态需区分阅读。旧文档中的已通过、尚未部署或当前账号列表都带有当时的范围；本导航不把它们重新宣布为最新验收结论。账号登记以 [account_profiles.json](../config/account_profiles.json) 为准。

`state/deployment/` 保存本机部署证据，`jobs/` 保存任务记录；它们保留在磁盘，但被本目录 Git 忽略规则排除。
