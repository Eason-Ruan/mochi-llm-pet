# C 与 D 的联调入口

本分支以 C 的 `7600627` 和 D 的 `e22c3fd` 为起点，保留双方提交历史。C 原 PR 的实现分支与上游 main 不因本地联调而被合并或改写。任务服务仍使用 M0 内存实现；本轮验证界面、工作段、事件接收和证据关联。

## 运行

完成 `uv sync --python 3.12 --extra dev --locked` 后，在仓库根目录执行：

```sh
uv run python scripts/run_cd_integration.py --offscreen --output output/m0/cd-first
```

每轮使用新的输出目录。去掉 `--offscreen` 可运行可见的原生 Qt 窗口，要求桌面解锁且可操作；脚本仍为自动操作和虚拟时钟，不是人工实验。

入口依次运行 C/D 回归测试、样例和 Schema 校验、D 原有的带证据清单回放、组合回放、提醒、工作段、状态纠正、多提醒与桌宠、真实时间提醒以及宿主原生回归。再用 D 的交付计数器、C/D 回顾证据检查器和 D 的窗口、交付违规、工作成果报告工具检查证据。

`summary.json` 记录每步退出码、耗时、命令、源码与产物散列、测试数量和各 GUI 流程的原生窗口验证状态；每步另存日志。任何一步失败会停止并以非零状态退出。真实时间流程实际等待十秒提醒和延后；其他场景使用虚拟时钟。所有操作仍为 automation，不能当作真人实验。

去掉 `--offscreen` 后，所有 GUI 报告必须通过 native_gui_verified 检查才会汇总为通过。Windows、真人体验、A 上游动态 UiSpec 和 B 正式持久化服务在报告中单列，不随 Mac 自动检查通过而被标为完成。

D 的 `d_replay.py` 当前只接受 metrics 单项配置，因此保留独立 D 基线回放；组合配置走 M0 标准 CLI。没有放宽 D 的证据要求，也不将组合回放冒充其正式报告。

人工操作 C/D 工作面板：

```sh
uv run python -m deskpet.m0 gui --profile scenario --adapters examples/m0/cd-product.adapters.json --output output/m0/cd-manual
```

桌宠模式将配置换成 `examples/m0/cd-desktop.adapters.json`。通过诊断页快进场景时间；输出目录的 `d-sink-info.json` 应说明已装配 `d_metrics.DMetricsSink`。

## 回顾的接受证据

C 回顾界面新增「我确认本轮没有完成项」。填写了成果时不能同时选中该项。保留空白回顾的兼容行为，但空白不视为确认没有完成项；勾选状态随该工作段草稿保留。

UI 在 session.end 的 `_review` 中携带 review_id、session_id、offered_at 和显式无成果标志。宿主移除该 UI 元数据后，将原有成果与下一步交给 WorkSessionService；公共 WorkSession/SessionCommand 模型不变。只有后端返回 applied 的结束命令才追加 `session.end_accepted`，拒绝或 duplicate 不重复生成接受记录。

该事件包含 command_id、UI 动作和服务接受事件的 seq、后端接受的完整 SessionResult.view、原始回顾元数据、提交时间、来源及状态。不能用客户端填写的成果替代后端接受视图。服务仍是内存实现；事件日志是本轮证据，不代表重启恢复已完成。

| review_status | 含义 |
| --- | --- |
| confirmed | 有合法回顾记录，接受视图中有成果 |
| confirmed_none | 用户显式选择没有完成项，接受视图成果为空 |
| unconfirmed | 提交了空白回顾，没有显式确认无成果 |
| not_offered | 该 UI 动作未提供回顾元数据 |
| invalid_evidence | 元数据不合法、关联不匹配或成果与无成果标志矛盾 |

宿主依据渲染通道与 auto_smoke 标记 origin，不接受 UI 自报 user。offscreen 和自动脚本标记为 automation；虚拟时钟下即使人工点击仍标记 simulated，不能算真实实验样本。

## 交给 D 的文件

- `reminders/trace.jsonl`：原始交付事件；`delivery-metrics.json` 来自 D 原有计数器，按 delivery_id、版本和事件顺序校验。
- `work/trace.jsonl`：工作段和接受回顾事件；`review-evidence.json` 检查动作、服务接受、终态版本及来源的关联。缺少接受记录会报告 evidence_gap，不从 ended 事件推断成果。
- `work/c-work-sessions-report.json`：本轮最终工作段视图与界面检查。
- `summary.json`、步骤日志、截图和源码散列：用于复现，运行输出不随源码提交。

可单独检查另一次 GUI 运行：

```sh
uv run python scripts/d_count_deliveries.py output/m0/cd-manual/trace.jsonl
uv run python scripts/check_cd_reviews.py output/m0/cd-manual/trace.jsonl
```

检查器报告的是证据完整性与诊断计数，`formal_outcome_coverage` 始终为 not_measured。D 当前的正式工作成果计算器也保留原行为。参与者、工作项、入组前的分母与资格、回顾截止规则和实际人工确认记录仍需与 D 确认后采集；没有这些数据不能发布成果完成率或正式回顾覆盖率。A 的真实 UiSpec 推送、B 的 SQLite 服务、Windows 和人工体验验收不包含在本轮通过结论中。
