# 脚本索引

日常启动入口为根目录 [自动剪辑.cmd](../自动剪辑.cmd)。操作见[手册](../docs/runbooks/auto-postprod-operator-v2.md)，口播模型和规则来源见[口播文本链说明](../docs/runbooks/ark-text-agent-v1.md)。

## 固定运维入口

- [doctor.ps1](doctor.ps1)、[init-operator.ps1](init-operator.ps1)、[start-component.ps1](start-component.ps1)、[start.ps1](start.ps1)、[stop.ps1](stop.ps1)
- [install.ps1](install.ps1)、[install-scenedetect.ps1](install-scenedetect.ps1)

## 保留的通用验证入口

部分入口会调用真实模型或读写运行数据；不因出现在此列表就获得运行授权。

- [inspect-cosy-graph.py](validation/inspect-cosy-graph.py)
- [inspect-torch-bridge.py](validation/inspect-torch-bridge.py)
- [qualify-ark.py](validation/qualify-ark.py)
- [qualify-fixtures.py](validation/qualify-fixtures.py)
- [qualify-torch-aux.py](validation/qualify-torch-aux.py)
- [qualify-vision.py](validation/qualify-vision.py)
- [qualify-voice.py](validation/qualify-voice.py)
- [run-real-fixtures.py](validation/run-real-fixtures.py)
- [test-live-incremental.py](validation/test-live-incremental.py)
- [acceptance-orchestration-v2.py](acceptance-orchestration-v2.py)
- [verify-scenedetect.py](verify-scenedetect.py)

## 任务工具

- [library-job.py](jobs/library-job.py)
- [repair-live-shot.py](jobs/repair-live-shot.py)

## 已退役的口播试验入口

根目录临时 DeepSeek 比稿、固定五条 Ark 任务自动推进、旧 Skill 隐式加载探测，以及旧口播资格/质量探测脚本已退出维护入口。它们不会随本文继续被推荐运行，也不新增转发脚本。

用户再次明确授权后，清单中的 57 个旧脚本、比稿结果、日志及副本已删除；准确路径、散列和复核结果见[整理记录](../docs/maintenance/voiceover-consolidation-20261014.md)。历史目录整理清单只描述当时状态，不能用它恢复已退役流程。
