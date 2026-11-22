# C · P1 工作段闭环

本文保留 P1 的实现与验收记录。当前提醒功能已推进到 [P2 提醒闭环](c-p2.md)，C 已使用自己的提醒表单和卡片；下方关于复用 ReminderView 的描述对应 P1 当时版本。

本阶段把目标、时长、开始、暂停、继续、结束及确认回顾接入 C 页面，并通过适配配置替换为 C 的工作段服务。公共模型、宿主线程、共享时钟和任务服务沿用 M0；完整休息周期、自动模式和跨进程恢复仍为后续范围。

## 打开与操作

先关闭旧版联调窗口，再双击 [start_c_workspace.command](../scripts/start_c_workspace.command)。选择「C 界面 · 实际展示」，可以看到完整工作段操作区。也可从仓库根目录运行：

```sh
uv run --offline python -m deskpet.m0 gui --profile scenario --adapters examples/m0/c-product.adapters.json --output output/m0/c-workspace
```

1. 填写工作目标，设置时长。默认 1500 秒（25 分钟），验证时可改成 60 秒。可选择关联已有事项。
2. 点击「开始工作段」。列表和详情显示目标、阶段和剩余时间；切换列表可查看其他工作段。
3. 点击「暂停」后剩余时间保持不变，点击「继续」后从剩余时间计时。诊断页操作的同一工作段也会同步到 C 页。
4. 点击「结束并回顾」，填写真实成果和下一步，每行一项，可留空。点击「返回工作段」仅关闭编辑区并保留草稿，不结束工作段。
5. 点击「确认结束并保存回顾」后结束工作段，在页面下方查看确认内容。结束后的记录可继续从列表选择查看，关联事项不会被自动完成。
6. 工作时间耗尽时显示「本轮时间已到」，可进入回顾。此时没有自动休息、延长或开始下一轮。

**当前启动的是虚拟时间联调模式**：请在「任务与工作段」页点击「快进」推进计时，真实等待不会自动倒数。默认的真实采样模式不属于本阶段验收。工作段及回顾目前只保留在本次程序运行中，关闭重开会清空；不宣称具备重启恢复。

页面可滚动，回顾编辑区打开后会滚动到确认按钮。普通数据刷新保留正在输入的目标、回顾草稿及当前选择。

## 实现与协议

| 模块 | 职责 |
| --- | --- |
| [surface.py](../deskpet/c_ui/surface.py) | QWidget 入口、共享快照、动作提交、滚动区域与提醒展示 |
| [work_panel.py](../deskpet/c_ui/work_panel.py) | 工作段选择、阶段按钮、回顾确认、结果关联与草稿管理 |
| [service.py](../deskpet/work_sessions/service.py) | CWorkSessionService，提供 apply／get／list_sessions／pump |
| [c-product.adapters.json](../examples/m0/c-product.adapters.json) | 同时替换 ui_surface 和 work_sessions |

工作段动作经 SurfaceContext 提交 UiAction，宿主将其转换为 SessionCommand。命令携带 session_id、command_id 和 expected_version；开始为版本 0，后续使用当前快照中的工作段版本。操作等待时禁用相关按钮，返回结果只用于确认操作；显示的领域状态由最新共享快照更新。

服务用共享单调时钟维护截止点，墙上时间用于 phase_due_at。普通读取与计时不增加版本；开始、暂停、继续、到期、结束增加版本。暂停冻结 remaining_s，继续重新计算截止点；结束清除该工作段的截止安排并保存用户确认值。重复命令按规范化内容核对，命令 ID 冲突、过期版本、非法状态和输入均被拒绝。到期事件只产生一次。

回顾以 session_id 保存各自草稿，切换工作段、返回编辑或普通刷新不会混入其他记录。提交失败保留用户输入；版本冲突后用户查看最新状态，再明确重试。若其他入口已经结束当前工作段，界面显示对应提示并禁用再次提交。结束成功后展示服务保存的成果和下一步；现有协议暂不支持结束后重新编辑。

提醒仍复用已有 ReminderView，正式提醒卡和完整动态渲染属于后续阶段。

## 检查命令

```sh
uv run --offline python -m pytest -q tests/m0/test_c_sessions.py tests/m0/test_c_workspace.py tests/m0/test_ui_surface.py tests/m0/test_work_runtime.py
uv run --offline python -m deskpet.m0 replay --suite m0 --adapters examples/m0/c-product.adapters.json --output output/m0/c-p1/replay
uv run --offline python scripts/smoke_c_work_sessions.py --output output/m0/c-p1/native
uv run --offline python -m deskpet.m0 gui --profile scenario --adapters examples/m0/c-product.adapters.json --auto-smoke --output output/m0/c-p1/host-native
```

原生检查保持桌面已解锁、窗口可见。`scripts/smoke_c_work_sessions.py --offscreen` 可验证 Qt 逻辑，但不能计作原生界面证据。该脚本通过 C 实际按钮和宿主队列操作，不直接调用服务写入方法；所有时间快进均为虚拟时间。

## 2026-11-18 验收

| 检查 | 结果 | 本机证据（仓库根目录下） |
| --- | --- | --- |
| 公共样例与 Schema | 24 个样例、20 个场景通过 | `output/m0/c-p1/examples.json` |
| C 服务离线回放 | 20/20 通过 | `output/m0/c-p1/replay/report.json` |
| 相关测试 | 41 passed | `output/m0/c-p1/pytest.log` |
| C 工作段原生流程 | 18 项检查通过 | `output/m0/c-p1/native/c-work-sessions-report.json` |
| 宿主原生回归 | 29 项检查通过、native 展示回执 | `output/m0/c-p1/host-native/native-gui-report.json` |
| 截图 | 已查看回顾填写和保存后的截图，中文、输入区及操作按钮可读 | `output/m0/c-p1/native/review-before-confirm.png`、`saved-review.png` |

此次源码指纹为 `cd1cccd53894b91431283d36e979aeb07fc762c6527026509e0124498c7adfbf`，回放与两次原生检查一致。自动检查证明：开始后两页共享同一记录；专注推进 20 秒后剩余 40 秒；暂停再推进 120 秒仍剩余 40 秒；从诊断页继续后同步 C 页；再推进 10 秒剩余 30 秒；确认回顾后结束，关联任务保持 registered。另一条 5 秒工作段用于核对到期提示和空回顾；草稿隔离、拒绝反馈、无效输入、重复命令及墙上时间跳变由相关测试覆盖。

这不是全仓库测试结论。完整人工输入／焦点体验、Windows、真实时钟长期运行、真实模型及重启恢复尚未验收。生成报告和截图只保留在本地 output，不作为源码提交。

[C 实现计划](adr/0008-c-implementation-plan.md) · [P0 历史记录](c-p0.md) · [GUI 与工作段契约](contracts/gui-work.md)
