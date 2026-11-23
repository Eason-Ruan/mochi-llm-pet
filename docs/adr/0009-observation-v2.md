# 0009 · 独立 Observation v2

状态为独立实现，设计来自 [Notion ADR](https://app.notion.com/p/420eb246ea0681169f3af32e46ee9ce5)。

```mermaid
flowchart LR
    A[系统前台和键鼠空闲秒数] --> B[SQLite Activity]
    C[公共录屏抽帧] --> B
    B --> D[本机 VLM 转写和脱敏]
    B --> E[近期窗口聚合]
    D --> E
    E --> F[本机 LLM 判断与摘要]
    F --> G[SQLite 有效 Observation]
```

- Activity 按 Work Review 的轮询、连续片段和截图触发设计实现。相同的应用或页面延长当前片段，锁屏、会话切换和采样中断切断计时。macOS 轮询 1.5 秒，常规截图间隔 30 秒，应用和页面切换有独立冷却。系统采集键鼠综合空闲秒数。活动行先提交，视觉结果按实际采集时间异步补齐。[Work Review 采集循环](https://github.com/wm94i/Work-Review/blob/main/src-tauri/src/main.rs)
- VLM 分开调用。SQLite 保存场景分类、经程序二次过滤的短转写和摘要，以及视觉处理结果。原始画面只在内存和本机回环请求中流转。脱敏依赖模型和程序模式匹配，覆盖范围需用有标注的样本衡量。
- 默认聚合最近 60 秒。程序计算可观察时长、前台和屏幕的各自切换、输入恢复与信号时效。独立文本模型输出活动类别和状态摘要。默认 TTL 为 15 秒，支撑判断的证据较早失效时可缩短。实时模式启动时发起首轮状态请求，此后默认至少间隔 30 秒发起一轮，每轮最多落库一条 Observation。模型失败或返回结果已过时时，本轮改为发布当前的 `unknown`。两轮之间的场景变化或证据过期会使旧结果失效，有效 Observation 可能暂时为空。
- 有效 Observation 字段为 `activity_kind`、`state`、`summary`、`reason_codes`、`observed_at`、`valid_until`、`context_revision`。录屏可以给出活动类别，纯录屏没有真实系统交互信号时可打扰状态为 `unknown`。SQLite 单独保存 Activity、屏幕观测和历次状态。使用独立 CLI 验收。

## 运行

准备 FFmpeg 和禁用云模型的本机 Ollama，拉取两个模型。录屏回放使用新建的数据库，实时模式可继续使用同一个实时数据库。

```sh
ollama pull qwen3-vl:2b-instruct
ollama pull qwen2.5:1.5b
python -m deskpet.observation_v2 replay public-recording.mp4 \
  --database /tmp/mochi-observation-replay.sqlite --interval-s 4 --max-frames 8 --require-models
```

回放以视频帧的展示时间驱动虚拟时钟，起点对应 `2000-01-01T00:00:00Z`。每个抽帧间隔有一条 Activity，缺少系统元数据时留空。`--require-models` 要求每帧均产生有效的 VLM 结果和经校验的 LLM 结果。SQLite 中的捕获成功与 VLM 成功由不同字段表示。

```sh
python -m deskpet.observation_v2 live --database /tmp/mochi-observation-live.sqlite --duration-s 65
```

实时模式默认采集系统元数据。`--capture-screen` 开启本机主屏采集，要求操作系统已明确报告屏幕解锁。macOS 支持对部分 Chromium 浏览器用 `--browser-metadata` 启用页面检测，可能需要 Automation 授权。可检测的私密窗口或登录、支付等敏感标题会跳过截图并省略标题、域名。1Password、Bitwarden 和 Keychain 默认排除。可重复使用 `--exclude-app` 和 `--exclude-domain` 排除应用及已识别域名。平台不给出私密模式信号时，判断依赖窗口标题提示。采集后会重新确认前台和会话是否一致。主屏画面可能包含其他窗口。采样结束后会收尾已开始的模型调用，实时模型请求的超时上限取 TTL 和配置超时中的较小值。Windows 和 Linux 原生采样需要各自的桌面环境复测。

## System 2 的 Observation 上下文

实时采集复用同一个本地 SQLite。采集程序重启时使上一轮当前记录失效，并保留已发布历史。SQLite 记录发布时间和上下文失效时刻。回放使用独立数据库。旧日志可查询历史。

```sh
python -m deskpet.observation_v2 context --database /tmp/mochi-observation-live.sqlite
```

上下文按同一 `as_of` 读取，包含 `current`、`current_status` 和过去一小时的 `history`。历史保留最近三条、最多八个简表段、最近八个断档边界及截断前统计。相邻同活动类别、同状态且间隔不超过 45 秒的记录合并，摘要取段内最后一条非空值。超过八段时保留最近四段，其余位置优先较早的 `available` 或 `away`，其次活动类别变化，同优先级选较新的。

在工作台代码中以 `build_environment(observation_database=...)` 启用此读取路径，Planner 实现 `ObservationContextPlanner.plan_with_context` 接收整个快照。M0 回放入口也接受 `--observation-database`，查询使用该场景的虚拟时钟。现有脚本 Planner 用新鲜的当前状态判断可打扰性；软提醒执行前复核当前状态、版本和有效期。到期兜底继续由任务服务处理。首版传递现有描述性 `summary`，事实校验见 Notion 子文档。


录像及标注归 GUI-World 数据集，授权为 CC BY 4.0。用于复跑的片段有 [VS Code 与 Docker Compose](https://huggingface.co/datasets/ONE-Lab/GUI-World/blob/main/software/212.mp4)、[编辑器与浏览器切换](https://huggingface.co/datasets/ONE-Lab/GUI-World/blob/main/multi/70.mp4)、[打开 Bilibili](https://huggingface.co/datasets/ONE-Lab/GUI-World/blob/main/website/76.mp4) 和 [TikTok 视频播放](https://huggingface.co/datasets/ONE-Lab/GUI-World/blob/main/website/175.mp4)。[数据集与许可](https://huggingface.co/datasets/ONE-Lab/GUI-World)

## 独立验收结果

用 `qwen3-vl:2b-instruct` 和 `qwen2.5:1.5b` 以 4 秒间隔回放四段公开录屏，14 帧均取得有效 VLM 与 LLM 响应。SQLite 完整性、外键、采集时间绑定和空系统字段已检查。纯录屏的可打扰状态全部为 `unknown`。

| 片段 | 抽帧数 | 活动类别 |
| --- | ---: | --- |
| VS Code 与 Docker Compose | 3 | 2 帧 `work`，1 帧 `unknown` |
| 编辑器与浏览器切换 | 5 | 1 帧 `work`，4 帧 `unknown` |
| TikTok 视频播放 | 3 | 3 帧 `leisure` |
| 打开 Bilibili | 3 | 2 帧 `leisure`，1 帧 `unknown`；页面入口的分类可再校准 |

合成画面中的姓名、邮箱、路径和密码值均未出现在 VLM 输出中。真实文本模型在合成 OS 信号上分别给出持续编辑 `working`、短暂切换 `unknown`、锁屏 `away`。真实 macOS 冒烟测了无截图的原生采集。原生截图的进程内采集与切窗丢弃由自动化测试覆盖。代码回归共 473 项。
