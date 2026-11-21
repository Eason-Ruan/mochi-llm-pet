# C · P2 提醒闭环

动态组件与状态查看／纠正现已接入，见 [P3 动态界面与状态](c-p3.md)。本文保留提醒阶段的操作与验收记录。

本阶段在 C 页面中实现「创建草稿 → 核对并确认 → 到期展示 → 完成或延后」，同时提供修改、取消、旧卡片清理和操作反馈。任务状态与调度继续由现有 TaskService 负责，C 不另建调度器。P1 的工作段和回顾保留。

## 打开与试用

关闭旧版窗口，双击 [start_c_workspace.command](../scripts/start_c_workspace.command)，进入「C 界面 · 实际展示」。页面可滚动，上方为到期卡片，下方为提醒表单、事项列表和工作段。

快速验证一条到点提醒：

1. 在「提醒事项」填写“提交报告”，提醒方式选择「到点提醒」，最早提醒设为 10 秒后。
2. 点击「创建草稿」。在事项列表选择该草稿，核对详情中的日期和时间，再点击「确认提醒」。草稿确认前不调度。
3. 切到「任务与工作段」页，点击「快进 10 秒」。到期后会切回 C 页，在「到期提醒」显示卡片。
4. 点击「已完成」，事项变为已完成，卡片移除。若选择延后，先设置秒数、核对卡片上预览的新时间，再点击「延后提醒」。
5. 延后后旧卡移除，事项仍为同一条；按新安排快进到期，会再次出现卡片，此时可以完成或继续处理。

当前仍是 **scenario 虚拟时间模式**，需要快进，不按真实等待倒数。页面显示明确的北京时间（UTC+08:00）；模拟日期来自场景，不代表当前真实日期。任务和工作段仅保留在本次进程中，重开后不会恢复。

## 其他操作

- **柔性提醒**：选择窗口内择机，填写最早提醒和窗口宽度。TaskService 根据有效状态判断是否提前展示；窗口最迟时刻仍会兜底。C 显示明确窗口，不自行改变调度规则。
- **修改安排**：从列表或卡片点击「修改安排」，载入对应事项；修改内容或时间后点击「保存修改」。保存前原安排有效，保存成功后旧投递失效；已确认任务仍保持已确认。草稿修改后仍需确认。
- **取消提醒**：列表或卡片均可取消，草稿也可以取消。取消后的旧请求或旧卡片操作不能恢复事项。
- **操作反馈**：等待结果时限制同一事项的重复提交。版本冲突或失败保留输入；发生修改冲突时，再点击「修改安排」载入最新版本后重试。
- **显示与完成分开**：展示成功后状态为“已提醒，待处理”，只有用户明确点击完成才变为已完成。

新提醒表单中的时间预览以当前服务时间计算，事项列表中的详情展示该事项已经保存的实际安排。点击创建草稿不会自动确认；在列表确认时应以详情为准。

## 实现说明

| 模块 | 职责 |
| --- | --- |
| [reminder.py](../deskpet/c_ui/reminder.py) · ReminderPanel | 新建及修改表单、任务列表、确认与任务动作、结果关联和失败反馈 |
| [reminder.py](../deskpet/c_ui/reminder.py) · ReminderCard | 绑定 delivery_id 的到期卡片、延后预览、可见绘制回执及失效处理 |
| [surface.py](../deskpet/c_ui/surface.py) | 卡片映射、唯一展示路由、快照刷新、卡片移除及滚动 |
| [smoke_c_reminders.py](../scripts/smoke_c_reminders.py) | 通过实际 C 控件与宿主队列验证完整流程 |

UiAction 沿用现有白名单。创建使用新 task_id 和 expected_version=0；后续动作带当前任务版本，surface_revision 遵循宿主的任务版本约定。修改编辑器保留载入时的版本，避免刷新把未确认的输入悄悄应用到新任务。领域结果由最新共享快照展示，迟到结果仅清理对应等待状态，不覆盖新快照。

延后动作传入 seconds，TaskService 从当前共享时钟计算新的安排，保留柔性窗口宽度、增加 occurrence 和任务版本。C 清理旧卡；下一次交付使用同一 task_id 和新的 delivery_id。相同 delivery_id 重送复用一张卡，不增加有效展示次数。

卡片在真实可见 paint 后安排 Qt 回调，并再次检查有效任务版本、组件生命周期、可见区域和宿主可报告状态，才报告成功展示。截图、隐藏卡片及失效回调不能形成成功回执；成功提交后不重复报告。任务完成、取消或版本更新时停止旧卡的定时回调并移除控件。未展示且请求已过期的卡片会清理；已成功展示的卡片仍可供用户后续处理。

构建控件或调用 show_reminder 不等于展示成功。宿主与 B 继续核验请求、回执和版本，C 的本地检查只是第一层防护。

## 自动验证

从仓库根目录运行：

```sh
uv run --offline python -m pytest -q tests/m0/test_c_reminders.py tests/m0/test_c_workspace.py tests/m0/test_c_sessions.py tests/m0/test_ui_surface.py tests/m0/test_work_runtime.py tests/m0/test_task_service.py
uv run --offline python -m deskpet.m0 replay --suite m0 --adapters examples/m0/c-product.adapters.json --output output/m0/c-p2/replay
uv run --offline python scripts/smoke_c_reminders.py --output output/m0/c-p2/native
uv run --offline python -m deskpet.m0 gui --profile scenario --adapters examples/m0/c-product.adapters.json --auto-smoke --output output/m0/c-p2/host-native
```

原生检查保持桌面解锁、窗口可见。`scripts/smoke_c_reminders.py --offscreen` 仅验证 Qt 逻辑，不能作为原生展示证据。脚本用虚拟时间推进业务，不产生真实等待时延指标。

## 2026-11-19 本机验收

| 检查 | 结果 | 证据（仓库根目录下） |
| --- | --- | --- |
| 相关测试 | 80 passed | `output/m0/c-p2/pytest-final.log` |
| 公共样例与 Schema | 24 个样例、20 个场景通过 | `output/m0/c-p2/examples.json` |
| C 配置离线回放 | 20/20 通过 | `output/m0/c-p2/replay-final/report.json` |
| C 提醒原生流程 | 26 项检查通过，4 次成功原生展示 | `output/m0/c-p2/native-final/c-reminders-report.json`、`trace.jsonl` |
| 宿主原生回归 | 29 项检查通过 | `output/m0/c-p2/host-native-final/native-gui-report.json` |
| 截图 | 已检查到期卡、时间、按钮和事项列表可读 | `output/m0/c-p2/native-final/due-reminder.png`、`reminder-panel.png` |

最终源码指纹为 `7c2c80fcc7594cc7416bb4e6f23d876fb91d57f1fa001be8e19d885b2708835a`。原生流程核对了未确认草稿不调度、确认后不提前展示、延后后同一任务再次展示、旧回调失效、完成后不重开、忙碌时柔性窗口兜底、展示后取消、修改后取消及取消任务不产生交付请求。宿主交付出口模拟渲染失败后，重试沿用原交付 ID，再产生成功回执。

相关 Qt 测试另覆盖隐藏／截图不误报、重复绘制不重复回执、旧版本编辑不覆盖新记录、失败保留输入，以及 P1 工作段闭环回归。最初有一个测试辅助事件循环会关闭测试窗口，已改为局部事件循环，最终检查通过。

这些结果只证明对应场景和本机原生运行。完整人工体验、Windows、真实模型、长期真实计时、多个屏幕和跨进程恢复仍未验收。源码未提交或推送 GitHub；生成报告和截图保留在本地 output。

[C 实现计划](adr/0008-c-implementation-plan.md) · [P1 工作段](c-p1.md) · [任务语义](contracts/task-semantics.md)
