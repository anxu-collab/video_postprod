# 本地显式指定口播 Skill 修复计划

执行状态（2026-10-14）：本地选择、快照、接口/后台门禁、页面及定向验收已完成，且已加载到空闲的后期调度服务。100 项定向测试通过、2 项因 Windows 符号链接权限跳过。真实云端模型质量验证未执行，日用品 Skill 内容仍为独立工作。详见 `docs/acceptance/2026-10-14-explicit-script-skill.md`。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 本计划不授权启动实现或真实付费调用；执行者遵守项目既有 ZCode 分工。

**Goal:** 本地在调用云端写稿前明确声明本次 Skill，移除按账号 selling 标签推断行业的行为。

**Architecture:** 本地调用方提交 Skill ID 与版本，服务端从受控注册表解析并保存规则快照，写稿只消费该快照。通用编剧角色、行业规则、账号口吻和任务资料分别维护；接口、网页和后台统一验证。

**Tech Stack:** Python、现有 JSON Schema、现有调度服务、原生 JavaScript UI、pytest。

**Spec:** 本文“确定的行为合同”，依据用户 2026-10-14 指示“调用哪个 skill 本地应该提前说明了”及本轮源码核查。

## Global Constraints

- 本地明确指定，不能按账号支持 selling、栏目、产品关键词或云端模型猜测 Skill。
- 新的自动写稿请求缺少 Skill、Skill 不存在或快照校验失败，均在云端调用前返回可解释错误。
- 粘贴现成稿不强制选写稿 Skill，不调用写稿模型；既有音频和成片不自动重做。
- 保留账号声音、人物、配乐隔离；不改模型、认证、预算、机器人或模型服务状态。
- 按用户要求只验证改动覆盖的路径，不跑完整视频生产，不配音、不渲染。
- 日用品 Skill 的内容创作是独立工作。本次不发明日用品模板、不将不存在的 Skill 标记为可用。
- 当前仓库存在大量既有变更；实施前记录限定文件基线，不回退他人改动，不自动提交整个工作区。

## Review Focus

- 从 API、网页、修订接口、后台旧任务恢复分别进入，都不能绕过显式选择：Task 2/3/4。
- Skill 文件更新后旧任务不得漂移，切换 Skill 不得复用旧稿的缓存或确认：Task 1/3。
- 旧任务无 Skill 仍可查看和复用已有工件，未生成任务不得自动套用历史错误规则：Task 2/3。
- 非健康 Skill 的最终 system/user 提示词均不得夹带健康角色、保健品句式或账号标签推导逻辑：Task 3。
- 用户从生成稿切换为粘贴稿，或仅修改配镜时，不应触发多余写稿和配音：Task 2/4/5。

## 确定的行为合同

1. 在 `script_input` 新增 `skill`，请求格式为 `{id, version}`。生成模式的新提交和重新生成必须填写；`generation_note` 不能替代结构化选择。
2. 服务端解析后增加 `snapshot_sha256`，快照保存完整规则正文、ID、版本、源文件散列。快照目录沿用账号快照的存储与原子写入方式，执行时确定具体位置并由模块集中管理。
3. 新增受控 `config/script_skills.json`，条目为 ID、名称、版本、用途说明、项目内规则文件；拒绝重复 ID/version 和越界路径。只登记实际存在且规则一致的 Skill。缺失的日用品 Skill 显示不可用，不建立空壳条目。
4. `GET /v2/script-skills` 只列可用条目及用途说明；UI 默认“请选择口播 Skill”。调用本地 API 的 Agent 可直接明确传入，用户无需逐次手工点击；前提是调用方已明确选择，不来自后台猜测。
5. 写稿前保存实际采用的 Skill 和最终提示词摘要；将完整提示词留在任务工程记录，不在视频画面显示。缓存键至少覆盖最终提示词和 Skill 快照散列。
6. Skill 变更通过新修订表达。写稿及依赖它的审核/确认需重新计算；已分析素材可复用。若稿件改变，配音及后续确认按现有依赖机制失效；不得沿用旧授权确认来自动放行新稿。
7. 历史结构继续可读。历史已生成稿和人工确认内容不自动改写；历史任务如尚需实际生成而没有绑定 Skill，返回 `SCRIPT_SKILL_REQUIRED`，由本地补齐后创建新修订。不得给旧任务偷偷补保健品 ID。
8. 大鲸鱼保留通用口播编剧身份与口语化、事实依据要求。身体感受、成分分组、特定 CTA、五句健康结构只归对应 Skill。用户提示词中现有“4到14字”等结构指令也须检查，避免与选定 Skill 冲突。
9. 保健品 `SKILL.md` 与运行规则只保留一个权威正文。先列出既有差异；涉及内容取舍的冲突不能借路由修复擅自重写，未解决的版本不可宣称审核就绪。

## Task 1: Skill 注册与不可变快照

**Files:** Create `scheduler/script_skill.py`, `config/script_skills.json`, `tests/test_script_skill.py`.

**Interfaces:** `list_skills() -> list[dict]`; `pin_skill(selection: dict) -> dict`; `load_skill(binding: dict) -> dict`。binding 包含 id/version/snapshot_sha256；load 返回已校验的正文和元数据。

- [ ] 写失败测试：已注册条目可解析，未知 ID/版本拒绝；路径越界拒绝；源文件更新不改变旧快照；篡改快照拒绝；重复登记拒绝。
- [ ] 运行 `envs/scheduler/Scripts/python.exe -m pytest tests/test_script_skill.py -q`，确认失败针对缺失功能。
- [ ] 实现注册与快照模块，复用项目现有原子文件与散列方式；正文只从受控路径读取。
- [ ] 重跑同一文件，预期全部通过；检查测试只写临时目录。

## Task 2: 任务合同、提交与历史兼容

**Files:** Modify `contracts/edit-job-v2.schema.json`, `scheduler/contracts.py`, `scheduler/service.py`; Create `tests/test_script_skill_api.py`; Extend `tests/test_contracts_v2.py`.

**Interfaces:** `validate_script_skill_requirement(job: dict) -> None` 放入 `script_skill.py`；在新提交和需要生成的修订进入队列前调用。历史静态读取不调用新提交约束。注册接口返回不含磁盘绝对路径的可用 Skill 列表。

- [ ] 写失败测试：生成模式缺 Skill 在入队前失败且模型调用计数为零；合法选择被绑定；未知/伪造快照失败；粘贴稿不受影响；两个版本提交接口及修订接口约束一致。
- [ ] 运行上述两个测试文件，核实新增断言失败。
- [ ] 接入 Schema、服务端验证、快照绑定、列表接口和可读错误信息。调用方不能用任意正文冒充已注册 Skill。
- [ ] 重跑定向测试；检查历史 payload 仍可解析和查看，不批量改数据库。

## Task 3: 提示词与后台消费显式绑定

**Files:** Modify `scheduler/script_plan.py`, `scheduler/pipeline.py`, `scheduler/revision_plan.py`; Extend `tests/test_script_plan.py`, `tests/test_account_profile.py`, `tests/test_ark_text_agent.py`; Create `tests/test_script_skill_pipeline.py`.

**Interfaces:** `script_system_prompt(profile: dict, skill_snapshot: dict) -> str`；后台在真正生成前调用 `load_skill`，将相同快照用于提示词及运行记录。`_generation_prompt` 通过显式 Skill 内容/设置处理结构，不再搜索账号规则中的行业词决定结构。

- [ ] 写失败测试：同一支持 selling 的账号选择两个测试 Skill，输出提示词随显式选择变化；非健康样例不含健康强制句式；含 selling 与不含 selling 的账号选择相同 Skill 时行业规则相同。
- [ ] 增加后台防绕过测试：旧任务无绑定但需要模型生成时零调用；已有稿件可继续；快照缺失零调用；变更 Skill 导致写稿缓存及相关确认失效，素材分析保留。
- [ ] 运行新测试，确认复现当前隐式加载缺陷。
- [ ] 移除 `skill_rules_text(profile)` 的 selling 推断，将角色健康专属内容迁出通用身份；删除后台无绑定情况下静默走旧生成器的漏洞。依据 revision_plan 实际依赖规则精确增加失效范围。
- [ ] 修改相关旧断言以符合显式接口，重跑上述五个定向文件；核对真实 system 与 user 请求捕获记录，不仅检查拼接函数。

## Task 4: 本地页面与调用说明

**Files:** Modify `ui/index.html`, `ui/app.js`, `.agents/skills/auto-postprod/SKILL.md`, `docs/runbooks/auto-postprod-operator-v2.md`; Extend `tests/test_script_skill_api.py`.

**Interfaces:** 网页读取 `/v2/script-skills`，将选择写入 `script_input.skill`；确认页展示服务器实际绑定的名称与版本。非浏览器调用方采用相同合同。

- [ ] 增加页面/API测试：生成模式缺选择不能提交；账号切换不偷偷改变 Skill；粘贴模式不要求选择；修改已有稿件保留来源记录但不重新调用模型。
- [ ] 接入选择与用途说明，提交前显示 Skill、账号和资料摘要；历史缺绑定任务显示如何补齐，不默认选第一项。
- [ ] 在隔离测试服务验证生成、粘贴、历史补齐三条路径，模型请求使用记录型替身。更新调用文档，明确本地 Agent 提交时必须声明 Skill。

## Task 5: 局部验收与交接

**Files:** Create `docs/acceptance/2026-10-14-explicit-script-skill.md`; 只在实现完成后写真实结果。

- [ ] 定向运行 Task 1–4 涉及测试，加 `tests/test_incremental_v2.py` 中与稿件修订/确认有关的用例；不执行整套生产或真实配音。
- [ ] 人工审查两份捕获请求：明确选择健康 Skill 与非健康测试 Skill，确认无行业串入；非健康测试 Skill 不登记为正式日用品能力。
- [ ] 核对界面绑定与模型请求使用相同快照，失败发生在网络调用前；记录测试命令、结果、改动文件与未完成项。
- [ ] 真实模型验证作为离线验收之后的独立步骤：只写稿、保存请求与结果，不推进配音/渲染；执行前核对已有费用授权及所选 Skill 是否内容就绪。本次计划不声称已完成该验证。

## 回退与变更控制

- 实现按 Task 分批审查；提交只包含本次明确文件，不自动将用户已有未提交改动纳入。
- 不迁移、不删除历史工件；新快照为增量文件。
- 回退只恢复本次代码/配置改动并保留已产生的任务与快照；旧版本不应直接接管含新字段的任务，恢复前检查兼容性。
- 本次通过标准是“本地声明什么，云端就接收什么；缺失就不调用”，不是“模型能返回一段文字”。
