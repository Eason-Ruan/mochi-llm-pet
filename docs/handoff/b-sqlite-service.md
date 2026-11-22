# B · SQLite 任务服务实施与联调

实现入口：`deskpet/task_service/sqlite.py`。真实进程恢复验收：`deskpet/task_service/recovery.py`。公共 models、ports、composition 和内存参考服务保持不变；保留原有契约断言，GUI 测试补充两个回归用例。

## 集成基线

本次实现以现有 M0 TaskService 业务语义为基础，通过独立工厂接入 SQLite。提交基于 `d/evaluation-m0` 的 `e22c3fd5`，保留该分支 D 的指标脚本、数据、测试和新增回放场景。

核心行为包括草稿/确认/编辑/延后/取消/完成、命令去重、版本比较、上下文/TTL 校验、软硬提醒和回执驱动交付。SQLite 适配器补充跨进程事务、持久交付意图及真实重启恢复验证。

同时集成两项 GUI 回归修复：虚拟时间的展示回执不能早于对应请求；恢复已有任务后，新建任务编号跳过已占用 ID。PyQt6 更新到 6.10.0，锁定依赖与现有 macOS 修复一致；Windows 原生行为仍需实机验证。

## 分阶段结果

| 阶段 | 修改及原因 | 验证 | 边界 |
| --- | --- | --- | --- |
| 1 持久化 | 独立 SQLite 适配器保存任务、命令原结果、决策去重、复查、交付和审计事件；继续用内存参考核心执行规则，避免两套业务语义漂移 | 关闭/重开后读回任务、原成功/拒绝结果；SQLite integrity_check | 本地磁盘上的 SQLite，不支持把同一文件放在网络共享盘 |
| 2 事务与去重 | BEGIN IMMEDIATE 将读取最新版本、规则裁决、任务变更、命令去重、事件及待交付写入作为一个提交 | 写入后注入异常、事务中 os._exit、双连接、新进程竞争 CAS / 同 command_id | 操作按取得数据库写锁的顺序裁决；跨进程不保证调用开始时间的先后顺序 |
| 3 调度与回执 | pump 消费注入 Clock；先提交 outbox，再展示；再次核对任务状态/版本/occurrence；回执独立提交；超时与失败用原 delivery_id | 37 项共享任务行为测试、评估分支全部回放、Qt queued 工作流、过期 lease 在 show 前落盘 | 云端 Qt 为 offscreen；需要宿主持续调用 pump；不承担系统唤醒职责 |
| 4 重启恢复 | 持久任务窗口恢复首次调度；持久 recheck 恢复复查；持久 delivery/outbox 恢复缺回执重试；已回执停止重试 | 三个独立进程读写同库；show 后回执提交前强制退出；意图提交后退出再取消；第三次启动核验 | 同 ID 重试 + GUI 幂等；不承诺跨崩溃严格 exactly-once 或用户已注意到提醒 |

验收覆盖原有共享行为、持久化与故障注入、真实进程恢复、评估分支回放、B + D 事件接收器和 Qt 离屏队列链。具体集成测试数量与结果记录在文末；原生验收单独见 [本地 Agent 操作](b-sqlite-native-agent.md)。

## 简单理解关键设计

**版本号像收据上的修改次数。** 界面提交“取消 v2”，只有数据库里仍是 v2 才能写成 v3。另一进程已经改到 v3，旧操作就明确返回 STALE_TASK。相同 command_id 的重试取回原结果，不再修改任务；即使原结果是拒绝，也会保存。

**事务像把一组动作装进同一个信封。** 任务变了而去重记录没写入，会导致重试再做一次。现在任务、去重结果、调度状态和事件一起提交，失败就一起撤销。SQLite 使用同步 FULL 与回滚日志，正常退出不是提交成功的前提；打开不认识的 schema 版本直接报错，不会尝试覆盖。

**outbox 是数据库里的待办清单。** 内存核心的 show/dismiss 先由内部收集器接住，只有意图提交后才调用真实 DeliveryPort。即使进程在显示之后突然退出，原 ID 已在库中，恢复会重试同 ID。C 须按 ID 复用卡片；收到有效 displayed 回执才保存 displayed_at。显示与用户完成仍是两回事。

**取消和展示需要最后再排一次队。** 执行 outbox 时重新检查状态、版本和 occurrence。短暂写锁覆盖最后检查及本地 show/dismiss 调用，避免另一个进程在检查后、展示前提交取消。DeliveryPort 必须像现有 Qt bridge 一样快速入队，不能在这里等待用户、网络或模型；锁超时会明确抛出 SQLite 错误，不会伪报成功。

**计时器只负责叫醒服务。** Qt 的 100ms timer 调用 pump，数据库里的任务窗口才是调度依据。程序停止期间不会弹窗；再次启动时，已过期的 hard/soft 最迟时间会补发。运行中按注入的单调时钟限流；持久化 UTC 时间用于新进程换算剩余等待，不能直接比较两个进程的 monotonic 数字。系统时钟向后调整时会保守延长重试等待，不提供关机唤醒承诺。

**新进程重新取得观察依据。** 旧 observation 保留在库中供审计，但不作为新进程提前展示 soft 提醒的依据。新会话先收到 A 的 observe，允许其 revision 从 1 开始；创建时间早于本次打开的旧决定拒绝为 STALE_CONTEXT。窗口最迟兜底和 hard 到期不依赖观察。公共 v1 没有跨启动 epoch 字段，同一虚拟时间戳不能独立证明决定来自哪次启动；新计划必须使用新的 request_id 与新观察。

## 持久化内容和运行约束

| 表 | 内容 |
| --- | --- |
| tasks | 公共 TaskView JSON；已确认时间窗也是可恢复的首次调度记录 |
| commands | command_id、完整输入指纹、原 CommandResult（包含拒绝） |
| decisions | request_id 与计划指纹 |
| rechecks | task_id、任务版本、执行 UTC 时间、request_id |
| deliveries | 唯一 delivery_id，task/version/occurrence 唯一约束，当前状态、次数、原请求、回执及计时依据；每任务至多一条 active |
| outbox | 已提交、待调用的 show/dismiss；相同 delivery_id 至多一条待 show；重启续做 |
| events | 业务事件审计和出口发送状态 |
| metadata | 数据库 ID、观察审计、会话 ID、抑制重复 deferred 事件的状态 |

适配器每次事务从最新数据库状态构造参考领域核心，避免多个连接各自缓存旧任务。当前采用小规模本地提醒服务的快照读写方式，成本随保存任务/历史数增长；未做大规模历史、长时间浸泡、磁盘耗尽或真实断电压测。数据库扩展保持独立版本，不改公共 schema_version=1。

每实例只属于创建它的领域线程。不要从 GUI 主线程直接调用；继续使用现有 queued gateway。独立测试/脚本用 with 或 close 释放连接。当前 M0 GUI 未提供适配器 close 生命周期钩子，已完成操作均即时提交，进程退出由 OS 释放连接；A 后续可在 ServiceWorker.stop 的领域线程调用可选 close。C 的工作段服务保持原有实现，B 没有暗中把工作段改成持久化。

所有 emit 在业务提交之后。出口失败不撤销命令，SQL events 留存待重试。回放历史事件时，外层 TraceEvent 时间属于当前发送运行，原记录时间在 payload.b_recorded_at；D 按 payload.b_event_id 去重，按 delivery_id 统计不同交付。出口是至少一次，不能承诺崩溃时遥测只发送一次。若现有 TraceRecorder 已吞下其 sink 异常，应另检查 sink_errors；不能从 emit 返回值推断下游文件已写成功。

## 接入与测试

持久工厂必须显式指定 MOCHI_TASK_DB，缺少路径会报错，防止静默用临时库丢任务。

```sh
export MOCHI_TASK_DB="$PWD/output/m0/b-live/tasks.sqlite3"
uv run python -m deskpet.m0 gui --profile live-smoke --scenario b-live \
  --adapters examples/m0/b-sqlite.adapters.json --output output/m0/b-live/gui
```

共享测试和每个 replay 场景必须相互隔离，使用测试工厂。它使用真实磁盘 SQLite，但每个环境一份临时库，不能用于正式提醒或重启验收。

```sh
M0_ADAPTERS=examples/m0/b-sqlite-test.adapters.json \
  uv run python -m pytest -q tests/m0/test_task_service.py
uv run python -m pytest -q tests/test_sqlite_task_service.py tests/test_sqlite_process_recovery.py tests/sqlite
uv run python -m deskpet.m0 replay --suite m0 \
  --adapters examples/m0/b-sqlite-test.adapters.json --output output/m0/b-replay
uv run python -m deskpet.task_service.recovery --output output/m0/b-recovery-new
uv run python -m pytest -q tests
```

PowerShell 对应 `$env:MOCHI_TASK_DB = Join-Path $PWD 'output/m0/b-live/tasks.sqlite3'`，共享测试设 `$env:M0_ADAPTERS = 'examples/m0/b-sqlite-test.adapters.json'`，结束后 Remove-Item Env:M0_ADAPTERS。云端实际使用当前解释器 `python` 运行相同命令；最终全量测试仅对子进程移除 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY 及小写变量以兼容 mock 测试。

恢复命令拒绝覆盖已存在数据库；重复运行换新的输出目录。它读取未修改的 recovery-fixture.json，明确将 first_delivery job 映射到 TaskView 窗口，将 obsolete_job 映射到有版本的 recheck 行。写入进程实际发出 persisted-delivery-1 且不返回回执，退出后由新的进程重开同库。保留 before-restart / opened-in-new-process / before-receipt / after-restart / verified-in-third-process 快照、各进程 PID/database_id、请求列表、两轮 trace 和报告。验收为 real_SQLite_process_restart；原始夹具仍保留其 M0 pending 标记，避免伪造原参考实现的能力。

恢复测试另用 os._exit 绕过 finally/close，覆盖意图已提交但未调用展示、展示已发生但回执尚未提交、命令事务写到一半三类崩溃。恢复证据和 SQLite 文件写入 output，均被忽略；不提交数据库、密钥、截图或运行报告。

## B 与 D 联合运行

`examples/m0/b-d-sqlite-test.adapters.json` 同时装配真实磁盘 SQLite 测试工厂和 DMetricsSink。每个场景独立数据库，保留 D 的完整 TraceEvent 和接收器诊断文件；D 的不同 delivery_id 计数应与回放结果一致。

```sh
uv run python -m deskpet.m0 replay --suite m0 \
  --adapters examples/m0/b-d-sqlite-test.adapters.json --output output/m0/b-d-replay
```

该配置用于隔离回放；真实 GUI 使用 `b-sqlite.adapters.json` 和显式 MOCHI_TASK_DB，可在本地配置中加入 `"metrics": "d_metrics:factory"`。

## 尚需本地取证

云端已覆盖领域行为、进程重启、竞争写入、异常回滚、同 ID 重试、模拟回执及 Qt offscreen 队列链。Windows/macOS 的 native 可见卡片、桌面采样权限、关窗/重开后的真实绘制与系统休眠、真实延迟和磁盘故障未在云端验收。按下一份本地操作单回传证据后再更新平台结论。

## 本次评估分支集成验收

- 基于评估分支 e22c3fd5 集成，原有 D 的 32 个新增文件逐个核对，内容完全保留。
- Linux / Python 3.12.14 / PyQt6 6.10.0 / Qt 6.10.2：完整测试集 **453 passed**，包含 D 的现有测试、B 的共享行为、持久化/崩溃与新增 B + D 事件证据测试。
- 同时装配 SQLite 与 DMetricsSink，评估分支 **25/25** 回放场景通过。
- 三个独立进程打开同一 SQLite 数据库的真实恢复验收通过；待回执复用原 ID、取消任务抑制旧作业、延后任务不提前交付、成功回执在再次启动后停止重试。
- Qt 测试使用 offscreen，不能据此宣布 Windows/macOS 原生桌面或休眠唤醒验收完成。
- 原始报告、SQLite、截图和日志保留在被忽略的 output 目录，未纳入提交。
