# 口播文本链：当前维护说明

**范围：** 本文说明已部署的口播接口和历史调用。写稿技能由任务显式选择。旧接口仍保留。

更新于 2026-10-14。本文说明实际运行入口与规则来源，替代旧 Turbo 接入报告中的操作指引。历史试验输出不作为当前模型或口播质量通过的证据。

## 日常入口

通过本地网页或现有任务 API 提交任务，流程为：生成整稿或粘贴用户稿 → 按句编号 → 稿件审核/确认 → 配音或导入音频 → 音频确认 → 字幕核对 → 配镜与审片。操作见[操作手册](auto-postprod-operator-v2.md)。

本轮新增独立口播稿入口：先选账号和 Skill、填产品资料，再写稿与审核。人工确认后填入原有新建视频表单，继续选择素材和后期设置；保存草稿不收费，生成按钮才使用显式勾选的额度。

“大鲸鱼”是本地维护的编剧角色。角色、选定行业规则、账号口吻和本次商品资料组合后，经 `providers/gateway.py` 发给云端文本模型。这里不是素材实验室的五个托管 Agent，也不通过旧批次脚本推进。

## 模型与配置来源

唯一配置为 [config/ark-text-agent.json](../../config/ark-text-agent.json)，由 [providers/ark_text_agent.py](../../providers/ark_text_agent.py) 读取和校验。

| 职责 | 本次核对的配置 | 思考 |
|---|---|---|
| script：整稿/短稿 | deepseek-v4-1-flash-260910 | 开启 |
| review：事实核查/文字审片 | doubao-seed-2-1-lite-260915 | 关闭 |

模型、token 上限、估算费率、思考余量及预算上限以配置字段为准，本文不重复维护价格表。配置中的历史 verified_date 不证明当前模型已重新实测。思考 token 的预留按现有程序处理；本次没有改模型、费率、预算、认证或实际请求参数。

`route_model` 优先识别审核节点，再识别写稿节点；旧任务显式保存的路线继续用于其既有流程，不能因为清理临时文件就删除生产兼容分支。真实调用和用量仍由现有账本记录。

## 角色、Skill、账号和资料

| 内容 | 权威位置 |
|---|---|
| 编剧身份、正文清理和分句 | [scheduler/script_plan.py](../../scheduler/script_plan.py) |
| 可选择的 Skill 与版本 | [config/script_skills.json](../../config/script_skills.json) |
| 规则解析和不可变快照 | [scheduler/script_skill.py](../../scheduler/script_skill.py) |
| 保健品 1.1.0 规则正文 | [.agents/skills/health-supplement-selling/rules-zh-v1.1.txt](../../.agents/skills/health-supplement-selling/rules-zh-v1.1.txt) |
| 保健品 1.0.0 历史规则 | [.agents/skills/health-supplement-selling/rules-zh.txt](../../.agents/skills/health-supplement-selling/rules-zh.txt) |
| 已批准表达及适用判断 | [词库登记](../../config/script_expression_catalog.json)、[词库说明](../../assets/script_expressions/README.md)、scheduler/product_applicability.py |
| 账号口吻、声音等 | [config/account_profiles.json](../../config/account_profiles.json) 指向的既有档案 |
| 商品资料与声明 | 本次任务的 product_documents、claims 等输入与固定记录 |

生成模式显式提交 `script_input.skill`，缺失或损坏时先拒绝云调用；粘贴现成稿不强制选择写作 Skill。独立草稿的账号和 Skill 必须明确选择，保存时固定版本及输入快照；审核消费与写稿相同的资料包。

保健品 1.1.0 为五个分工不同的表达单元，90–130字是目标，不再要求每句35字、三个症状或重复归因反转。1.0.0及历史快照保留原语义。独立草稿最多修复两轮；人工修改另存新稿并只做审核。日用品 Skill 尚未登记。

## 审核与后期边界

旧链结构审核可受开关控制，账号事实审核也有条件分支；关闭或缺规则显示跳过。新独立草稿要求明确的逐句审核结果，服务异常、空返回、信息不足都不能当通过。只有 ready_for_user_review 可进入人工确认；确认绑定具体正文，交接时后端再次核对账号与来源。模型通过不替代用户内容验收或医学事实验证。

沿用稿件确认、音频确认、字幕核对和配镜链。任何稿件或规则版本变更须经现有修订流程处理，不用旧脚本自动点过确认，也不清空任务和付费账本以重复生成。声音、已确认正文和已交付媒体仍保留原归属。

## 维护与验证

- 正式回归测试继续在 tests/；测试代码不等于应删除的中间比稿输出。
- 验证只跑改动涉及部分；本轮逐项测试、独立审查、真实试稿及运行状态见[验收记录](../maintenance/health-script-chain-20261014.md)。不运行完整视频来替代文字链验收。
- 旧临时比稿、固定任务批量推进、scratch 资格探测脚本退出操作入口；不要运行它们。
- 旧口播试验清单中的 57 个文件已删除；清单和实际复核结果见[口播整理记录](../maintenance/voiceover-consolidation-20261014.md)。
- 本轮已获授权实施并做一轮真实产品文字验证；不自动确认稿件、不批量配音或制作视频，机器人保持运行。
