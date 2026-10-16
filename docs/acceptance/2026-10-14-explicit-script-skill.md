# 显式口播 Skill 修复验收（2026-10-14）

本轮完成本地显式选择、服务端快照绑定、云端请求组装、旧任务兼容及页面接入。已在无运行任务、无进行中付费请求、无运行节点的情况下，仅刷新后期 scheduler；生产接口读回验证通过。

## 已生效行为

- `edit-job/v2` 自动写稿必须提交 `script_input.skill = {id, version}`；服务器保存 `snapshot_sha256`。缺失/未知/损坏的绑定在入队前拒绝，后台另有推理前校验。
- `/v1/jobs` 提交 v2 也走同一约束。真正 v1 的隐式写稿被阻止，提示改用 v2；已有 v1 timeline 的字幕/画面修订复用路径保留。
- 通用“大鲸鱼”不再写死健康行业结构，账号支持 selling 不再自动加载保健品 Skill。结构审核的栏目判断也改为本次任务的实际栏目。
- 注册表 `config/script_skills.json` 只登记现存的 `health-supplement-selling@1.0.0`。日用品 Skill 没有空壳登记，不默认选择任何 Skill。
- 快照保存规则正文、源文件原始字节散列和正文散列；历史快照验证不依赖当前注册表或当前源文件。重复提交保留原绑定，不因源规则更新漂移。
- 每次生成保存 `script-generation.json`（实际 Skill、system、prompt、请求散列）与 `script-context.json`；相同输入复用真实、校验过的稿件。只改选镜方式、字幕或配乐不重写稿件。
- 更换 Skill 后旧确认不继承。显式再生成使用 `regeneration_id`，即使选择相同 Skill 也会独立生成并重新确认；同一次请求重试沿用该 ID。
- 粘贴稿不要求 Skill，不调用写稿模型。人工修改生成稿可保留 Skill 来源，仍走粘贴稿模式。
- 页面提供名称、版本、用途、账号和资料摘要；同 ID 的多个版本准确匹配。已有生成稿和缺 Skill 的旧 v2 任务可在页面明确选择后创建新修订。
- `SKILL.md` 不再复制另一份矛盾的五句结构，权威正文指向 `rules-zh.txt`；正文内容未在本轮擅自改写。

## 验证证据

Python 定向测试命令：

```powershell
envs/scheduler/Scripts/python.exe -m pytest tests/test_script_skill.py tests/test_script_skill_api.py tests/test_script_skill_pipeline.py tests/test_contracts_v2.py tests/test_script_plan.py tests/test_account_profile.py tests/test_ark_text_agent.py tests/test_selling_script_facts.py tests/test_single_shot_revision.py tests/test_clef_pipeline.py -q
```

结果：**92 passed, 2 skipped**。两项跳过测试需要创建 Windows 符号链接，当前权限不支持；普通路径越界、伪造快照、损坏内容及绑定身份校验均已执行。

页面测试 `tests/test_script_skill_ui.py`：**8 passed**。使用现有 Codex bundled Python 的 Playwright，进程内加入 scheduler 的 site-packages 以复用 pytest 依赖，关闭外部 pytest 插件自动加载。所有浏览器请求通过 `page.route` 接收替身响应，无生产写操作。覆盖空选择、空/失败列表、同 ID 多版本、账号切换、粘贴稿、同 Skill 重写及旧 v1 限制。

另运行 `node --check ui/app.js`、差异空白检查，均通过。未运行全项目测试或完整视频流水线。

独立审查使用 GPT-6-Astra，页面/注册表有界实现使用 GPT-6-Luna。审查发现并修复：

1. 真正 v1 payload 绕过 Skill：新增提交与后台双重限制，对应两条回归测试由失败转为通过。
2. 同 Skill “重新写稿”复用缓存：增加 `regeneration_id`，请求标识/缓存与确认均隔离，对应 Python 及浏览器用例通过。

额外核对真实本地链的请求结构：测试执行实际 `Pipeline.chat` 和 `Gateway.request`，仅把外部 HTTP/云端 transport 替换为隔离响应，确认最终 system/user 消息携带指定快照。此项不是云端真实模型质量验收。

完整日志位于 `state/deployment/explicit-script-skill/`：`final-python.txt`、`final-ui.txt`、`pre-reload.json`、`live-verification.json`、`progress.md`。基线副本在该目录的 `baseline/`，未提交或回退用户原有改动。

## 运行服务读回

- 刷新前：可运行/运行中任务 0、运行中付费请求 0、运行节点 0。
- 刷新方式：现有 `scripts/start-component.ps1 -Component scheduler -Restart`。
- `/health/live` 返回 `alive`，`pipeline_ready=true`。
- `/v2/script-skills` 返回现存保健品条目，未暴露规则文件路径或正文。
- 老X新任务模板没有预填 Skill。
- 刷新前后历史任务各状态数量一致；8761、8762、9235 的监听进程未变化。未操作机器人。
- 本轮生产任务提交 0，真实云端推理 0，配音/生成/渲染 0。

## 仍需独立处理的内容

- 日用品 Skill 的正文尚未设计、登记。本次修好的是显式选择与执行链，不把测试用非健康规则当作正式日用品 Skill。
- 保健品现行正文有药品示例、症状/效果表达待复核；保留原文并在目录与说明中标明，不能把加载成功视为内容审核通过。本轮未做真实模型质量验收。
- 老X当前账号附加写稿规则为空；其他既有账号（如老周）仍含已确认的产品、剂型和健康场景口径，本次未擅自删除或跨账号改写。非健康提示词隔离测试证明通用角色和路由不串入行业结构，不代表所有历史账号规则已完成行业拆分。
- 两个旧网关测试的固定 token 数值与现行配置不符，已改为验证配置实际上限；一个旧规则文本 grep 改为验证资料事实边界。未更改模型或预算配置。

## 回退边界

当前工作分支 `codex/explicit-script-skill`。运行任务与新增快照均不批量迁移。若需回退，依据限定文件基线恢复本次代码，并先处理含新 Skill 字段的任务兼容；不能让旧版本直接重写这些任务，不删除历史资产、模型、声音或成片。
