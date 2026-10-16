# 脚本目录整理：2026-10-13

> 历史记录：其中口播试验脚本已于 2026-10-14 退出维护入口；删除是否完成以[本轮整理记录](voiceover-consolidation-20261014.md)为准，不按本文直接恢复或运行旧批次脚本。

本次将 35 个原有脚本按用途归类，保留固定入口和既有数据。没有改动业务模块、配置、合同、测试或任务数据。

## 结果

- `scripts/validation/`：移动 11 个验证脚本。`acceptance-orchestration-v2.py` 因受保护测试按旧路径导入而留在 `scripts/`；`verify-scenedetect.py` 因 `install-scenedetect.ps1` 固定入口调用及现有文档引用而留在原位。
- `scripts/jobs/`：移动 2 个任务处理脚本。
- `scripts/diagnostics/`：移动 13 个下划线历史诊断脚本。
- 7 个 PowerShell 运维/安装入口、根目录 `自动剪辑.cmd` 保持路径不变。
- `scripts/README.md` 已按真实目录重写；操作手册和账号文档中的活跃脚本路径已更新。旧路径只在历史记录中保留，没有建立转发入口。

所有移动文件的移动前后 SHA256 位于 `../../archive/housekeeping-20261013/directory-moves.json`。路径修改的原始副本与理由位于同目录 `path-edits.json` 和 `originals/`；每项 `content_modified` 按 `sha256_before` 与 `sha256_after` 的差异记录。脚本索引原件按整理前基线 SHA256 `926B745364BCE5AF7E83AB993C58F5AF406FF9ECEC90CC52F5FE24B1029EDE81` 恢复；换行规范化的中间备份保留在 `originals/review-baseline/scripts-README-newline-variant.md` 作为过程记录。相互调用只改了 `_run_all_ark.py` 对 `_clean_job.py` 的路径；被移入子目录的项目根目录解析从 `parents[1]` 调整为 `parents[2]`。

## 验证

- 35 个原始脚本逐个映射到新位置或明确保留位置；移动清单记录 26 项（11 个 validation、2 个 jobs、13 个 diagnostics）；逐项核对修改项的 `originals/` 回退副本均与 `sha256_before` 基线一致。
- 所有项目内 Python 脚本使用 `compile(source, filename, 'exec')` 做语法编译，没有执行脚本。
- 检查 moved Python 项目的根路径定位和 `_run_all_ark.py` 内部调用；外部真实依赖旧路径的 acceptance 测试与安装脚本目标均留在根目录。
- 检查文档相对链接存在，并搜索旧脚本路径。历史文档可保留当时路径，活跃操作文档使用新位置。
- 未运行业务测试、运维入口、模型、服务或真实任务；因此这不是业务验收结论。

## 回退

1. 先检查 `directory-moves.json` 中目的文件仍存在且 SHA256 等于 `sha256_after`，并确认原路径不存在；再逐项移回，不覆盖新文件。
2. 对修改过的源文件，需要回退时从 `originals/` 核验对应副本后恢复。脚本索引与本记录可按本次版本对照恢复；已修改的活跃手册路径可根据 `path-edits.json` 中记录的目标及回退副本恢复。
3. 不清理归档目录，不改动任何任务、素材、数据库、环境、日志或输出。
