# 提交记录

## 1. 标识

- 日期：2026-10-16
- 提交人：仓库维护
- 短名：submission-record

## 2. 要解决的问题

公开仓库需要一块固定位置，说明哪些内容可以修改并提交。提交前先留下记录，审阅时能看到改动范围、验证和回退方法。

## 3. 允许修改的文件

| 路径 | 改动 |
|---|---|
| `submissions/README.md` | 新增可提交范围和提交前记录步骤 |
| `submissions/record-template.md` | 新增记录模板 |
| `submissions/records/2026-10-16-submission-record.md` | 本份记录 |
| `README.md` | 目录表和提交说明指向新目录 |
| `docs/README.md` | 文档导航增加入口 |

## 4. 明确不改的内容

未改调度、渲染、模型网关和网页。未加入素材、密钥、运行数据、行业话术库或产品文案。

## 5. 验证计划

核对新页面中的相对链接指向本次新增文件，并确认记录模板要求先写记录再改程序。

## 6. 验证结果

已核对 `README.md`、`docs/README.md` 与 `submissions/README.md` 的相对链接指向现存文件。未运行测试，未加载模型，未做付费调用。

## 7. 回退

删除 `submissions/`，并撤回 `README.md` 与 `docs/README.md` 中指向该目录的两处说明。
