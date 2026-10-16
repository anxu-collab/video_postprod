# 脚本索引

日常使用入口是根目录 [自动剪辑.cmd](../自动剪辑.cmd)，操作步骤见[手册](../docs/runbooks/auto-postprod-operator-v2.md)。以下脚本保留原位置，索引不改变调用路径。

## 启动命令

从 `video_postprod` 根目录运行：

```powershell
.\scripts\start.ps1 -OpenUI
```

这会初始化操作者设置并请求启动本地组件。`stop.ps1` 请求停止项目 supervisor，`start-component.ps1` 用于指定组件维护；运行前应核对活跃任务。`doctor.ps1` 检查环境、依赖、工具与端口；其部分模型状态文本为历史诊断口径，不能替代实际验收。

## 启动与维护

- [doctor.ps1](doctor.ps1)
- [init-operator.ps1](init-operator.ps1)
- [start-component.ps1](start-component.ps1)
- [start.ps1](start.ps1)
- [stop.ps1](stop.ps1)

## 安装

会修改本机环境；已有安装不需因整理目录而重装。

- [install-scenedetect.ps1](install-scenedetect.ps1)
- [install.ps1](install.ps1)

## 验证与验收

验证脚本的副作用不同，可能创建测试任务、加载本地模型或调用付费服务。应先阅读对应脚本与授权范围，不能整批作为只读检查执行。

- [acceptance-orchestration-v2.py](acceptance-orchestration-v2.py)
- [check-ark-text-agent-script.py](check-ark-text-agent-script.py)
- [inspect-cosy-graph.py](inspect-cosy-graph.py)
- [inspect-torch-bridge.py](inspect-torch-bridge.py)
- [qualify-ark-text-agent.py](qualify-ark-text-agent.py)
- [qualify-ark.py](qualify-ark.py)
- [qualify-fixtures.py](qualify-fixtures.py)
- [qualify-torch-aux.py](qualify-torch-aux.py)
- [qualify-vision.py](qualify-vision.py)
- [qualify-voice.py](qualify-voice.py)
- [run-real-fixtures.py](run-real-fixtures.py)
- [test-live-incremental.py](test-live-incremental.py)
- [verify-scenedetect.py](verify-scenedetect.py)

## 素材与任务处理

可能构建、提交或修订任务；先核对账号、素材、目标任务和调用参数。

- [library-job.py](library-job.py)
- [repair-live-shot.py](repair-live-shot.py)

## 历史诊断与特定任务

下划线脚本可能包含固定账号、任务编号、真实接口调用、自动确认或清理动作，不是通用日常入口。保留它们用于历史排查；重用前逐份检查，不能直接批量执行。

- [_build_ark_jobs.py](_build_ark_jobs.py)
- [_cap.py](_cap.py)
- [_check_skill_load.py](_check_skill_load.py)
- [_clean_job.py](_clean_job.py)
- [_fail.py](_fail.py)
- [_ledger.py](_ledger.py)
- [_peek_01i.py](_peek_01i.py)
- [_probe_jobs.py](_probe_jobs.py)
- [_probe_provider.py](_probe_provider.py)
- [_repro.py](_repro.py)
- [_run_all_ark.py](_run_all_ark.py)
- [_run_ark_job.py](_run_ark_job.py)
- [_status.py](_status.py)

截至 2026-10-13，共索引 35 个现有脚本。返回[项目说明](../README.md)。

