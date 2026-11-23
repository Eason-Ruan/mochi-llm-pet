# 给本地 Agent：B SQLite 原生桌面取证

目标：对 `d/evaluation-m0` 上的 B SQLite 适配器进行 Windows/macOS 原生验收。数据库、密钥、截图、日志全部留在被忽略的 output 目录，不 git add。不要把 offscreen 或夹具结构检查记作 native / 真实恢复通过。

## 1. 准备与环境记录

1. 执行 git status --short --branch、git remote -v、git rev-parse HEAD。若有用户未提交工作先保留；从项目仓库取得评估分支。记录 OS、Python、Qt 版本、测试提交与当前时间。
2. `uv sync --extra dev`。先跑 `uv run python -m pytest -q tests`；没有 uv 则在独立 venv 按 pyproject 安装。环境代理导致 provider mock 测试失败时，仅为该测试进程移除代理，保留原错误证据，不修改业务代码掩盖环境问题。
3. 确保没有 QT_QPA_PLATFORM=offscreen/minimal。macOS 首次需要时允许 Python/终端的辅助功能或输入监测；按当前系统实际提示记录已授予/未授予，不把未知能力写成真实传感器成功。Windows 在真实交互桌面运行。
4. 创建全新的 `output/b-native`，MOCHI_TASK_DB 指向其中固定的绝对路径 `tasks.sqlite3`；重启必须复用它。后续各次运行使用不同日志目录，不能覆盖前次 trace。

POSIX：

```sh
export MOCHI_TASK_DB="$PWD/output/b-native/tasks.sqlite3"
uv run python -m deskpet.m0 gui --profile live-smoke --scenario b-native-before \
  --adapters examples/m0/b-sqlite.adapters.json --output output/b-native/before
```

PowerShell：

```powershell
$env:MOCHI_TASK_DB = Join-Path $PWD 'output/b-native/tasks.sqlite3'
uv run python -m deskpet.m0 gui --profile live-smoke --scenario b-native-before `
  --adapters examples/m0/b-sqlite.adapters.json --output output/b-native/before
```

## 2. 正常提醒与取消竞争

1. 建 hard 提醒，最早时间设 10 秒；创建草稿、确认，等待真实时间。取界面截图，记录 task_id、version、delivery_id。必须出现同 ID 的 delivery.requested 与 delivery.displayed，后者 source=native、simulated=false、clock=wall。显示时任务仍 registered；点击完成后应成为 completed，版本只在用户命令时增加。
2. 新建提醒，确认后保存决策版本，取消，再注入旧决策。应有 decision.rejected / CANCELLED，任务保持 cancelled，取消提交后没有该任务的新 delivery.displayed；已在取消前发生的真实历史不删去。
3. 用一个仍 registered 的提醒点击延后，核对 occurrence 增加，旧卡片消失、旧版本回执拒绝，新到期时间前不展示。保存 trace、原生截图及任务快照。

## 3. 退出期间到期与再次启动

1. 建 hard 提醒，设为 60 秒后，确认。记 task_id、确认版本和 UTC 到期时间，关闭程序或终止**已确定 PID 的该次 Python 进程**。不要使用会结束其他进程的广泛 pkill 命令。
2. 等到确认到期之后，以相同 MOCHI_TASK_DB 启动第二次 GUI，将 scenario 改为 b-native-after，output 改为 output/b-native/after。
3. 检查同 task_id 的 registered/version/occurrence 均保留；到期补发仅产生一个稳定 delivery_id；收到 native 回执后再记 displayed_at。再退出并第三次启动到 output/b-native/third，已回执提醒不应再发出请求。
4. soft 提醒重启后，旧 available 观察不能自行触发提前展示；需接到新观察。无法取得桌面证据时应为 unknown，到窗口最迟仍兜底。验证新启动的观察 revision 从 1 开始也可正常接入，创建新计划不应持续 STALE_CONTEXT。

## 4. 原生已绘制、回执丢失的可控故障

需要确定性观察“真实卡片已画出但 B 尚未记展示”时，在 output/b-native/fault_adapter.py 写入以下临时故障适配器；它只拦截 B 收到的异步回执，不改公共源码。将 output/b-native 加入 PYTHONPATH，创建 output/b-native/fault.adapters.json：`{"task_service":"fault_adapter:factory"}`。

```python
import os
from pathlib import Path
from deskpet.task_service import SQLiteTaskService

class DropReceipt(SQLiteTaskService):
    def record_receipt(self, receipt):
        if os.environ.get("MOCHI_DROP_DISPLAY_RECEIPTS") == "1":
            target = self.path.parent / "dropped-receipts.jsonl"
            with target.open("a", encoding="utf-8") as stream:
                stream.write(receipt.model_dump_json() + "\n")
            return None  # 故障注入：明确丢回执，不能据此声称 B 已保存展示。
        return super().record_receipt(receipt)

def factory(*, clock, delivery, emit):
    return DropReceipt(clock, delivery, emit, path=os.environ["MOCHI_TASK_DB"])
```

1. 设 MOCHI_DROP_DISPLAY_RECEIPTS=1，启动使用该 JSON 的 GUI，创建/确认一个 hard 提醒。原生卡片出现后，截屏并记录 dropped-receipts.jsonl。只读查询 SQLite 中该任务 displayed_at 应仍为 null、delivery 为 pending；保留查询结果。不要将故障适配器的返回 None 解释成成功回执。
2. 终止本次 Python PID。清除 MOCHI_DROP_DISPLAY_RECEIPTS，使用正式 b-sqlite.adapters.json 和同一数据库、全新的 output 重启；等待不短于 5 秒。
3. 比较重启前后 delivery_id、task_id、task_version、occurrence 必须相同。新 GUI 按同一 ID 展示卡片，收到 native 回执后 SQLite displayed_at 才变为非空。原生 GUI 进程也已退出时重新出现卡片是恢复行为，不能声称跨 GUI 崩溃严格只绘制一次。
4. 另建一条相同故障状态的提醒，先取消再重启；此后旧投递不得重新展示。恢复时旧任务命令需要当前版本和新 command_id。

## 5. 输出证据与回传结论

导出只读任务和交付摘要（用户终端可直接运行，数据库路径来自环境变量）：

```python
import json, os, sqlite3
from pathlib import Path
path = Path(os.environ["MOCHI_TASK_DB"])
db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
db.row_factory = sqlite3.Row
print(json.dumps({
    "tasks": [json.loads(r["view"]) for r in db.execute("select * from tasks order by task_id")],
    "deliveries": [dict(r) for r in db.execute("select * from deliveries order by delivery_id")],
    "integrity": db.execute("pragma integrity_check").fetchone()[0]
}, ensure_ascii=False, indent=2))
db.close()
```

将每轮查询保存为 before / after / third JSON，连同各轮 trace.jsonl、native-gui-report.json（若生成）、卡片截图、dropped-receipts.jsonl、PID、提交 SHA、OS/Qt、启动参数、权限状态和所有失败原始日志交回。D 按 b_event_id 去重审计事件，按 delivery_id 统计不同交付，保留 b_recorded_at 与本轮发送时间的区别。

回传表逐项标 passed / failed / not_run：真实显示、完成语义、取消竞争、延后新 occurrence、退出期间到期补发、丢回执同 ID 恢复、第三次启动不重复、SQLite integrity。仅当 source=native 且实际屏幕截图可核对时填写 native passed；看不到真实桌面就记录限制。系统休眠唤醒另做实机测试，不能用 FakeClock 或普通进程重启代替。
