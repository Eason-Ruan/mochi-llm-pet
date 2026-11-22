# C · P4 体验完善与桌宠入口

本阶段完成分区布局、中文时间、焦点保留、多提醒队列，以及同一 Qt 进程中的桌宠入口。任务、工作段和观察仍使用同一套共享服务；P0–P3 的文档保留各阶段历史记录。

## 打开与退出

先退出旧版本窗口，双击 [start_c_workspace.command](../scripts/start_c_workspace.command) 或 [start_c_desktop.command](../scripts/start_c_desktop.command)。两个入口都会打开同一套新版工作伙伴。

- **单击桌面小猫**：打开已有工作面板并进入提醒页。
- **拖动小猫**：移动位置，保持在所在屏幕的可用区域内。
- **右键小猫**：选择提醒、专注或状态入口，或退出工作伙伴。
- **点击窗口关闭按钮或「收起到桌宠」**：仅隐藏面板，当前服务和数据继续保留。
- **真正退出**：右键小猫选择「退出工作伙伴」，停止共享服务线程和小猫动画，再退出程序。

当前数据仍为内存数据，真正退出后不会恢复。启动前应退出旧版本，避免同时运行两个独立进程。

如需保留原来的纯联调窗口、不显示桌面小猫，从仓库根目录运行：

```sh
uv run --offline python -m deskpet.m0 gui --profile scenario --adapters examples/m0/c-product.adapters.json --output output/m0/c-workspace
```

桌宠模式的等价命令：

```sh
uv run --offline python -m deskpet.m0 gui --profile scenario --adapters examples/m0/c-desktop.adapters.json --output output/m0/c-desktop
```

## 布局和时间

C 页分为「提醒事项」「专注与回顾」「状态与纠正」三个区域，各自滚动，避免长表单堆在同一页面。右侧到期提醒栏始终保留，不必切走正在编辑的页面。桌宠模式默认收起联调状态条和事件日志，可从「显示联调日志」重新展开；诊断页和快进功能仍可使用。

提醒安排显示北京时间 UTC+08:00，根据同一共享时钟显示「今天」「明天」「昨天」，更远日期显示中文年月日。跨日换算依据时区，不改变领域中的 UTC 时间戳。状态来源、采样有效性和常见信号名增加中文说明。

当前仍是 **scenario 虚拟时间模式**：通过「任务与工作段」页快进，不随真实等待倒数。「今天」指场景时钟的日期，页面会明确标注演示模式。正式真实时钟和恢复能力不由本阶段隐式开启。

## 焦点和多个提醒

到期消息在右侧队列中按收到顺序加入，每条保留独立事项和操作；相同 delivery_id 不重复创建。已有卡片不会因为新提醒被覆盖或自动切换，输入、选区、滚动和当前功能页也不因普通刷新而重置。

多条提醒时，通过右侧下拉框选择。只有当前选中、实际可见并完成绘制的卡片才报告展示回执；队列里的隐藏卡片不计为已展示。完成或取消只移除对应事项，其他提醒保留。长卡片可在侧栏滚动。

如果面板收起，到期请求仍由同一服务处理，小猫显示待处理数量。需要打开面板并实际显示卡片才产生展示回执；小猫数字、后台构建控件和截图不等于展示。请求在隐藏期间过期时，按原有服务与有效期规则处理，本阶段不承诺锁屏交付、系统通知或后台补发恢复。

后台刷新不会主动激活主窗口。只有用户点击小猫、选择入口或主动打开面板时才请求前台焦点。用户在另一个窗口输入时，普通服务刷新不夺取其焦点。

## 桌宠整合边界

[desktop.py](../deskpet/c_ui/desktop.py)在现有宿主外增加轻量桌面入口，复用仓库的 [AnimationPlayer](../deskpet/sprite/player.py)、[素材注册器](../deskpet/sprite/registry.py)和既有小猫素材。小猫入口只负责动画、移动、待处理数量及打开面板，不创建第二套领域服务。

`c-desktop.adapters.json` 使用既有 gui 适配器入口；进入后移除该入口配置、挂载 C 的 ui_surface，并保留同一工作段适配器。QApplication、服务线程、Clock、任务／工作段状态都只有一套。隐藏和再次打开面板不重新装配这些对象。

这次复用了桌宠外观与动画，没有启动旧 Application.main、Brain、Sensors、记忆数据库或真实模型。因此不宣称已经合并旧桌宠完整自主行为、抚摸反馈、抛掷物理与新工作助手；这类进一步整合仍需 A 协调。旧桌宠独立启动方式保持原样。

## 验证

```sh
uv run --offline python -m pytest -q tests/m0
uv run --offline python scripts/smoke_c_experience.py --output output/m0/c-p4/native
uv run --offline python scripts/smoke_c_work_sessions.py --output output/m0/c-p4/work-native
uv run --offline python scripts/smoke_c_reminders.py --output output/m0/c-p4/reminders-native
uv run --offline python scripts/smoke_c_state.py --output output/m0/c-p4/state-native
uv run --offline python -m deskpet.m0 gui --profile scenario --adapters examples/m0/c-product.adapters.json --auto-smoke --output output/m0/c-p4/host-native
```

原生检查要求桌面已解锁，窗口可见。`smoke_c_experience.py --offscreen` 用于集成检查，不算原生桌面证据。

## 2026-11-19 验收记录

| 检查 | 结果 | 本机证据（仓库根目录下） |
| --- | --- | --- |
| 全部 M0 测试 | 193 passed | `output/m0/c-p4/pytest-final.log` |
| 公共样例与 Schema | 24 个样例、20 个场景通过 | `output/m0/c-p4/examples.json` |
| 桌宠配置离线回放 | 20/20 通过，无 GUI 导入 | `output/m0/c-p4/replay/report.json`及对应测试 |
| 桌面体验原生流程 | 17 项检查通过 | `output/m0/c-p4/native/c-experience-report.json` |
| 工作段原生回归 | 18 项检查通过 | `output/m0/c-p4/work-native/c-work-sessions-report.json` |
| 提醒原生回归 | 26 项检查通过 | `output/m0/c-p4/reminders-native/c-reminders-report.json` |
| 状态原生回归 | 13 项检查通过 | `output/m0/c-p4/state-native/c-state-report.json` |
| 宿主原生回归 | 29 项检查通过 | `output/m0/c-p4/host-native/native-gui-report.json` |
| 截图 | 已查看工作草稿与三条提醒、小猫入口、重新打开后的面板，可读 | `output/m0/c-p4/native/` 下 PNG |

源码指纹为 `bd0278ba013d73181e2f018a478b4bd8418e7dd5cfc9573370d65105409f6d69`，回放与五轮原生检查一致。焦点检查在字段含选择区时连续投递三条提醒，核对文字、选区、焦点和工作页不变；仅显示第一条时只产生一条回执，选择第二条后再产生第二条，第三条在隐藏时取消且无成功展示。另核对外部输入框在后台刷新后仍保持活跃、收起后工作线程不停止、小猫单击打开同一工作台，以及退出后线程和动画定时器停止。

多提醒测试还发现并修复了投递消息早于时钟快照的回执竞态：C 等待至少达到 requested_at 的快照后才报告可见绘制，避免生成早于请求时间的回执。

Windows、多屏切换、DPI 组合、完整人工体验、长期真实运行与重启恢复仍未验收。自动检查和截图不代替完整用户评审。所有改动仍在本地，未提交或推送 GitHub；output 产物不作为源码提交。

[C 实现计划](adr/0008-c-implementation-plan.md) · [动态界面与状态](c-p3.md)
