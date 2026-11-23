# C 的真实时间入口

双击 `scripts/start_c_workspace.command` 或 `scripts/start_c_desktop.command`，默认使用 realtime 模式。页面标明「真实时间模式」，顶部时钟随系统时间更新，快进按钮禁用。首次启动前退出旧的演示窗口；已有进程不会因修改启动脚本而切换时钟。

仓库根目录下也可以运行普通工作面板：

```sh
uv run python -m deskpet.m0 gui --profile realtime --adapters examples/m0/cd-product.adapters.json --output output/m0/c-realtime-manual
```

创建 hard 提醒、最早时间设为 10 秒后、确认后实际等待，右侧到期提醒自动显示。时间按创建草稿时计算；如果长时间后才确认，已到期的 hard 提醒会在确认后处理。延后以提交时的实际时间重新计算，工作段按实际经过时间倒数，暂停冻结剩余时长。

realtime 复用 RealClock 和原有服务线程的 100ms pump，不在界面创建第二套业务时钟。到期墙钟时间用于展示，工作段使用单调时钟计算剩余时长。它不启动 live-smoke 的 OS 采样、截图或模型调用；无观察样本时状态为 unknown，soft 提醒仍按其最迟期限处理，hard 提醒不依赖 available 状态。

虚拟时间测试保留在 `--profile scenario`；双击 `scripts/start_c_simulation.command` 或运行 C/D 联调脚本可进入演示模式。M0 通用 CLI 为兼容旧联调用法仍默认 scenario，日常双击入口明确传 realtime。live-smoke 仍是独立的原生采样诊断模式。

真实时间验证脚本：

```sh
uv run python scripts/smoke_c_realtime.py --output output/m0/c-realtime-native-01
```

每轮使用新目录。脚本实际等待 10 秒提醒、延后 2 秒后再次展示、完成，再验证工作段暂停与恢复。`--offscreen` 仅供回归测试，不能计为可见窗口证据；所有脚本操作均标记 automation。真人操作仍需另做体验验收，Windows 未因 Mac 验证而自动通过。

当前仍是内存数据；退出进程后不恢复。应用完全退出、系统睡眠期间不保证准时通知，也尚未接入系统通知或声音。窗口收起时小猫显示待处理数量，打开面板并实际显示后才产生展示回执。
