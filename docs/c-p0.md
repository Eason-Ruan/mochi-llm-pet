# C · P0 接入准备

本文记录 P0 的历史接入与验收。当前代码已具备 [P1 工作段闭环](c-p1.md)和 [P2 提醒闭环](c-p2.md)，`c-product.adapters.json` 同时接入 C 的工作段服务与界面；下方的 P0 按钮与 mock 边界描述对应当时版本。

本阶段提供 C 自己的页面入口和联调配置，复用 M0 的 Qt 宿主、内存任务服务、工作段服务及虚拟时钟。无需模型密钥，也不采集真实桌面信号。

## 启动

在仓库根目录安装锁定环境：

```sh
uv sync --python 3.12 --extra dev --locked
```

先运行仓库原有样例：

```sh
uv run --offline python -m deskpet.m0 gui --profile scenario --adapters examples/m0/c-ui.adapters.json --output output/m0/c-baseline
```

关闭样例窗口，再启动 C 的页面：

```sh
uv run --offline python -m deskpet.m0 gui --profile scenario --adapters examples/m0/c-product.adapters.json --output output/m0/c-workspace
```

本机已配置 `.venv` 时，也可直接运行 `.venv/bin/python` 替代 `uv run --offline python`。macOS 用户可以双击 [启动脚本](../scripts/start_c_workspace.command)。联调窗口打开后，选择「C 界面 · 实际展示」页，即可看到「Mochi · C 工作面板」。

## 页面接入与验证

入口为 [`deskpet.c_ui.surface:create_surface`](../deskpet/c_ui/surface.py)，配置为 [`c-product.adapters.json`](../examples/m0/c-product.adapters.json)。当前只替换 ui_surface，工作段继续使用现有 MemoryWorkSessionService，未新增后端或时钟。

1. 页面先显示「等待服务连接」，收到共享快照后显示「服务已连接」并启用按钮。目标为空时不能提交。
2. 填写目标，点击「开始 60 秒联调工作段」。按钮等待操作结果期间禁用，成功后显示已创建。
3. 切到「任务与工作段」页，核对同一目标；可在该页暂停、继续和结束。回到 C 页，工作段状态与剩余时间应同步更新，未提交的输入草稿应保留。
4. 在诊断页创建 hard 提醒，确认并快进到期。宿主自动切到 C 页展示提醒；点击「已完成」后，诊断页任务完成，C 页卡片移除。

scenario 使用虚拟时间，等待 60 秒不会自行推进业务计时；使用联调台的「快进」验证时间变化。界面普通重绘不修改工作段状态。

## P0 的实现边界

- `CWorkspace` 是独立 QWidget，提供共享连接、目标输入、工作段概览、任务数量和提醒区域。
- 通过 SurfaceContext 和 UiAction 调用现有 queued gateway，界面不直接访问领域服务。
- 提醒暂时复用 M0 的 ReminderView，沿用已存在的真实绘制回执和按钮行为；正式卡片在后续阶段实现。
- 当前页面上的开始按钮用于验证一次真实共享操作。完整工作段控件、回顾、UiSpec renderer、状态纠正和产品样式按后续阶段推进。
- 快照没有 UiSpec 推送字段。P0 已确认动态组件应先独立开发，正式推送方式需要与 A 对齐。
- 任务动作携带 task_id、expected_task_version 和宿主当前使用的任务版本作为 surface_revision；工作段动作在 payload 中带 session_id 与 expected_session_version，开始为 0。

## 自动检查

```sh
uv run --offline python -m deskpet.m0 examples --check
uv run --offline python -m deskpet.m0 replay --suite m0 --adapters examples/m0/c-product.adapters.json --output output/m0/c-p0/replay
uv run --offline python -m pytest -q tests/m0/test_ui_surface.py tests/m0/test_work_runtime.py tests/m0/test_c_workspace.py
uv run --offline python -m deskpet.m0 gui --profile scenario --adapters examples/m0/c-product.adapters.json --auto-smoke --output output/m0/c-p0/product-native
```

新增集成测试从实际配置加载 CWorkspace，经过宿主队列创建工作段，在诊断页暂停后核对 C 页同步，并处理一次提醒展示及完成。另检查使用同一配置的 headless replay 不导入 Qt 或 C 的界面模块。

原生自动检查需保持桌面已解锁且窗口可见，不能使用 offscreen/minimal 替代原生验收。macOS 沙箱若出现 `Incompatible processor ... neon`，按 [M0 环境说明](m0.md)在允许原生系统查询的环境中运行，沿用锁定依赖。

运行结果与截图保存在本机 `output/m0/c-p0/`，不提交生成产物。原生自动检查、截图可读性与人工体验评审分别记录，Windows 需单独验收。

## 2026-11-18 本机验收记录

| 检查 | 结果与证据位置（仓库根目录下） |
| --- | --- |
| 锁定环境 | Python 3.12.16，按现有 uv.lock 安装，未修改依赖版本 |
| 公共样例与 Schema | 24 个样例、20 个场景通过，Schema 与代码一致；`output/m0/c-p0/examples.json` |
| C 配置离线回放 | 20/20 通过；`output/m0/c-p0/replay/report.json` |
| 相关测试 | 25 passed；包含 2 项新增 C 页面集成检查；`output/m0/c-p0/pytest.log` |
| 原有嵌入样例 | 29 项原生自动检查通过、2 条 native 回执、无错误；`output/m0/c-p0/baseline-native/native-gui-report.json` |
| C 自有页面 | 29 项原生自动检查通过、2 条 native 回执、唯一展示出口为 C；`output/m0/c-p0/product-native/native-gui-report.json` |
| 截图检查 | 已查看 C 页截图，标题、中文、输入框及按钮可读；`output/m0/c-p0/product-native/c-ui-surface.png` |

上述回放和两次原生检查的源码指纹均为 `18a74f659c8102a43bb0dd72137d94648058ca5551db8948988ffb53ee61bba6`。基于现有脚本的原生自动检查验证了实际装配、展示路由和交付回执；C 自有按钮的工作段创建、双向同步及草稿保留由 Qt 集成测试验证。完整人工输入／焦点体验、Windows、真实模型与完整番茄钟仍未验收。

首次受限环境中的 Qt 检查出现 neon 特性查询失败，原生及集成测试在允许系统查询的环境中重跑通过，未更换 Qt 或修改锁文件。

[C 实现计划](adr/0008-c-implementation-plan.md) · [C 交接](handoff/c-gui-work-sessions.md)
