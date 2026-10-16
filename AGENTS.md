<!-- REUSE-FIRST-CONTROLLED-PHRASES:BEGIN -->
## GitHub借鉴与本地话术偏好（2026-10-15）

全局工作偏好唯一来源：`C:/Users/anxu/.codex/agents/references/reuse_first_policy.json`。方案设计、资料整理与实现先查本地已有能力和GitHub适用方案；能复用的方法、结构和组件优先复用，缺口自行补齐，保留来源和实际采用状态，不把研究称为已安装或已接入。

涉及本目录的保健品口播时，先读本机的 `config/controlled_phrase_policy.json` 及其 current_record。具体句段、词库和产品口径只留在本机，不写入公开仓库。事实审核独立，未通过不得确认。旧 API 仍保留历史兼容。

本条不改变其他项目的内容、身份、执行者、模型或费用规则；新旧冲突仅在上述范围按最新用户选择处理。
<!-- REUSE-FIRST-CONTROLLED-PHRASES:END -->

<!-- MODEL-LIFECYCLE-POLICY:BEGIN -->
## 本地模型生命周期

- 本地模型只在实际推理时按需加载；检查、问答和健康检查不加载。
- 推理成功、失败、超时或取消后立即退出/真正卸载并释放显存；`empty_cache` 不算卸载。
- 控制器可常驻但不得加载或重启闲置模型；按阶段共享 GPU，不预热、不闲置常驻。
- 保留模型资产与声音，不改认证、默认设备或添加 CPU 推理回退；不得打断其他活跃推理。
- 此规则跨会话和 Agent 生效，只有用户后续明确指示才能例外。唯一事实源：`C:/Users/anxu/.codex/agents/references/model_lifecycle_policy.json`。
<!-- MODEL-LIFECYCLE-POLICY:END -->
