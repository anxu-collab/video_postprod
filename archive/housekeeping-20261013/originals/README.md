# 自动后期制作

本目录是本机自动剪辑引擎：接收素材与整稿，编排配音、字幕、选镜和渲染，保留任务版本、费用记录与人工确认环节。视频生成仍属于上层项目的 H3 流程。

## 从这里开始

- 日常使用：双击 [自动剪辑.cmd](自动剪辑.cmd)，按[操作手册](docs/runbooks/auto-postprod-operator-v2.md)在网页中操作。
- 查资料：[文档导航](docs/README.md)。
- 维护与排查：[脚本索引](scripts/README.md)。
- 本次整理：[变更和回退记录](docs/maintenance/project-organization-20261013.md)。

启动入口会启动本地组件；待确认整稿、配音、字幕和成片应按界面流程审阅。技术检查通过不代表内容已验收。

## 目录用途

| 目录 | 内容 |
|---|---|
| `scheduler/` | 任务编排、数据库、版本与确认环节 |
| `workers/` | MPT、Narrato 组件与声音等适配 |
| `providers/` | 模型提供商接口与调用账本 |
| `renderer/` | 时间线编译、FFmpeg 渲染与校验 |
| `ui/` | 本地操作网页 |
| `contracts/` | 任务、时间线等结构定义与示例 |
| `config/` | 版本、账号登记及业务配置 |
| `migrations/` | 数据库迁移 |
| `locks/` | 依赖锁定文件，属于需保留的项目资料 |
| `scripts/`、`tests/` | 运维脚本与测试 |
| `.agents/`、`docs/` | 技能与项目文档 |
| `envs/`、`upstream/`、`tools/` | 本机环境、上游工作副本与工具 |
| `artifacts/` | 导入素材、内容寻址资产和相关记录 |
| `jobs/` | 任务版本、中间结果和交付原件 |
| `output/` | 按账号、产品、审阅状态归类的输出 |
| `state/`、`logs/` | 数据库、进程状态、本机凭据及日志 |
| `archive/` | 有来源记录的整理归档 |

## 账号与历史

[账号登记表](config/account_profiles.json)目前包含 `laoX` 和 `laozhou`。老X 档案位于[上层项目配置](../config/postprod_profile.json)；老周接入依据与制作记录见[账号记录](docs/accounts/laozhou-tianyiqing.md)。不同账号的声音、素材和任务保持各自归属。

上层的 [v1 模块说明](../docs/modules/auto-postprod-v1/MODULE.md)保留历史设计内容；其中早期“尚未部署”的描述不能直接当作当前运行状态。操作流程优先查本目录手册，具体任务状态以任务记录和实际审阅为准。

## 本机文件与版本管理

[忽略规则](.gitignore)排除虚拟环境、上游工作副本、运行数据、缓存和 FFmpeg 可执行文件，保留源码、配置、合同、依赖锁及工具清单。忽略仅影响 Git 展示，不删除文件，也不代表这些文件无需备份。

任务数据库、素材、中间结果和输出之间可能互相引用，不能只按目录名称、年龄或数量批量清理。记录中的运行状态和验收结果也不等于本次已重新验证。
