# A/B/C/D 自动联调

独立集成分支以 C/D 的 408505b、main 的 d9c4c48 和 B/D 的 cbb2d6e 为起点，保留原提交历史。源目录与原 C/D 环境分开，依赖按 B 分支的 uv.lock 安装；本地集成不表示上游 PR 已合并。

```sh
uv sync --python 3.12 --extra dev --locked
uv run python scripts/run_cd_integration.py --with-sqlite --output output/m0/abcd-native-first
```

执行完整 tests、公共样例、A/B/C/D 组合回放、B 的三进程恢复、原生 C 界面/工作段/状态/桌宠/真实时间提醒、共享宿主，以及 D 的证据与报告工具。加 --offscreen 时仅为离屏诊断。

GUI 每轮使用不同的显式 MOCHI_TASK_DB，位于输出目录的 databases 下；使用 B 的 production factory。回放场景用 isolated_factory 隔离各场景数据库。原生流程结束后重新读取各数据库，核对任务、业务事件和 integrity_check。ServiceWorker 在所属线程退出前调用可选 close，避免把 SQLite 连接释放留给其他线程的析构。

新宿主的 only_if_idle 启动保护与 C/D 的结束接受证据同时保留。当前联调覆盖 A 的共享宿主、观察/纠正、queued action 和展示路由；不能把新 UI 演示或本地 UiSpec 渲染当成上游真实模型 UiSpec 推送通过。

待验证项目包括 Windows 实机、真人体验、上游模型产生的动态 UiSpec、睡眠/唤醒和长期运行。C 的工作段仍为内存状态；B 的任务持久化不表示 C 工作段已持久化。所有自动脚本操作标记 automation，正式用户指标没有因此被测得。
