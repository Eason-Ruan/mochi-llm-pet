# C · P3 动态界面与状态

当前已加入分区布局与桌宠入口，最新打开与退出方式见 [P4 体验完善](c-p4.md)。状态功能现在位于 C 页的「状态与纠正」分区；本文保留 P3 的实现和验收记录。

本阶段把现有 UiSpec 六类组件渲染接入 C 页面，并实现状态查看、有期限的用户纠正、提前清除和到期回退。状态仍由 A 的观察管线产生，C 不重新采样或复制估计规则。提醒和工作段继续使用原有共享服务。

## 打开与使用

关闭旧窗口，再双击 [start_c_workspace.command](../scripts/start_c_workspace.command)。在 C 页上方的「当前状态」可以直接看到有效状态和来源。点击「查看依据与纠正状态」展开详细内容：

1. 查看当前有效状态和基础估计，它们可能不同。纠正有效时采用纠正，但基础采样和估计继续更新。
2. 查看原始采样来源、时间、能力和缺口，以及基础估计所依据的样本与证据。
3. 选择你当前的状态，填写原因，选择持续时间，再点击「应用临时纠正」。原因必填，不会自动把工作段开始当作用户标签。
4. 生效后显示纠正来源、原因、创建时间和截止时间。可以点击「清除纠正，恢复估计」。
5. 到期后采用当时仍有效的基础估计；若原始证据也过期，显示暂时无法判断。

持续时间可选 5 秒、30 秒、5 分钟、30 分钟和 1 小时。当前启动的是虚拟时间模式，需在联调台点击「快进」验证到期；真实等待不会推进场景时间。纠正与其他数据同样只保留在当前进程。

手动操作来源为 user，自动验收为 automation，脚本回放为 scripted。界面明确区分这些来源；自动验收通过不代表获得了真实人工状态标签。

## 动态渲染设计

[UiSpecRenderer](../deskpet/c_ui/renderer.py) 提供 `render_spec(UiSpec 或 dict)`，采用公共模型校验；[状态页](../deskpet/c_ui/state_panel.py)把收到的真实缓存 ObservationFrame 转换为 UiSpec，再由同一 renderer 呈现和收集操作。因此本阶段已经实际用到动态组件，状态页的表单没有另写一套固定输入控件。

| 类型 | 实现 | 当前用途 |
| --- | --- | --- |
| text | 普通文本 QLabel | 说明、原始采样、纠正信息 |
| card | QGroupBox 和普通文本内容 | 有效状态与基础估计 |
| list | QListWidget | 证据和依据样本 |
| text_input | QLineEdit | 用户纠正原因 |
| select | QComboBox | 用户状态和有效期 |
| button | QPushButton | 应用纠正和清除 |

这是现有协议的扁平组件子集：不新增嵌套布局、脚本、任意组件或公共字段。控件按 component_id 维护，输入按 data_key 绑定。text/card 直接显示绑定数据，list 绑定数组；button 的 data_key（若设置）绑定一个基础 payload 对象，再合并输入字段。clear 动作保持空 payload。状态页将中文选项转换为契约枚举和秒数，再经 SurfaceContext 提交 UiAction。

动态输入以同一 surface_id 下兼容的 component_id、类型和 data_key 保留。普通数据刷新不重建控件，不覆盖用户草稿。结构变化必须增加 surface_revision；旧表面的回调不能借新快照提交操作。等待结果期间禁用动作按钮，失败或拒绝保留输入。

renderer 只接受公共 UiAction 白名单，状态页进一步限制为 state.override 和 state.clear_override。未知组件、重复 ID、无绑定输入、非法选项、未知动作和过期 revision 均提示界面不可用并禁用操作。外部文本按普通文本显示，不执行表达式或代码。

## 接入边界与样例

- [c-state-ui.json](../examples/m0/c-state-ui.json) 是可通过公共模型验证的六类组件样例，供后续 UiSpec 生产方参考。
- 当前可运行来源为 C 页从共享观察快照构建的描述。宿主快照没有通用 UiSpec 推送字段，本次没有声称接通真实模型生成界面。
- renderer 可单独接受合法 UiSpec；后续通用任务表面或模型推送需与 A 明确路由和表面版本的维护方，再经同一验证入口接入。
- C 状态页使用 observation.context_revision 作为自己的表面语义版本，状态更改后更新动作绑定。任务表面仍须遵循任务版本和宿主协议。

## 宿主结果关联

原有 state.override / state.clear_override 只返回 correction_applied / correction_cleared，无法判断来自哪个界面操作。本次在宿主的状态结果中附带提交 UiAction 的 event_id；已有 command_id 的任务／工作段结果保持原模型字段不变。

合法格式的状态动作若含非法参数，宿主返回带 event_id 的 rejected / INVALID_ARGUMENT，并发出 ui.rejected 事件，让对应表面解除等待并保留输入。采样失败和其他服务异常仍沿用原有诊断路径。这是 queued gateway 的结果关联补充，没有改动公共领域 Schema 或观察算法。

## 验证方式

```sh
uv run --offline python -m pytest -q tests/m0
uv run --offline python -m deskpet.m0 examples --check
uv run --offline python -m deskpet.m0 replay --suite m0 --adapters examples/m0/c-product.adapters.json --output output/m0/c-p3/replay
uv run --offline python scripts/smoke_c_state.py --output output/m0/c-p3/native
uv run --offline python scripts/smoke_c_reminders.py --output output/m0/c-p3/reminders-native
uv run --offline python -m deskpet.m0 gui --profile scenario --adapters examples/m0/c-product.adapters.json --auto-smoke --output output/m0/c-p3/host-native
```

原生检查需解锁桌面并保持窗口可见。`smoke_c_state.py --offscreen` 只验证 Qt 行为，不能算原生显示证据。

状态流程覆盖六种组件、空原因拒绝、C 页设置纠正、原始信号更新而纠正仍有效、纠正到期、再次纠正和提前清除、原始证据失效与恢复、非法有效期的关联拒绝，以及工作段不隐式生成标签。renderer 测试覆盖输入和控件保留、旧回调拒绝、动作白名单、错误描述降级、结构更新和关闭后的提交反馈。

本机最终结果记录在 `output/m0/c-p3/summary.json`，原始日志、回放、trace 与截图保留在同一目录下，不作为源码提交。Windows、真实模型界面生成、完整人工体验评审和长期真实采样仍待后续验收。

## 2026-11-19 验收记录

| 检查 | 结果 | 本机证据 |
| --- | --- | --- |
| 全部 M0 测试 | 190 passed | `output/m0/c-p3/pytest.log` |
| 公共样例与 Schema | 24 个样例、20 个场景通过 | `output/m0/c-p3/examples.json` |
| 离线回放 | 20/20 通过 | `output/m0/c-p3/replay-final/report.json` |
| 动态状态页原生流程 | 13 项检查通过 | `output/m0/c-p3/native/c-state-report.json` |
| 提醒闭环原生回归 | 26 项检查通过 | `output/m0/c-p3/reminders-native/c-reminders-report.json` |
| 宿主原生回归 | 29 项检查通过 | `output/m0/c-p3/host-native/native-gui-report.json` |
| 截图检查 | 已查看有效纠正及清除后的状态页，文字、表单和按钮可读 | `output/m0/c-p3/native/active-correction.png`、`state-panel.png` |

最终源码指纹为 `860bc45d6ec9a97da5dc6cd350e3e4549b06efc8c8e4841f7c92f14020f0fb39`，回放与三轮原生检查一致。此次状态操作有可关联的 event_id，非法有效期也返回对应拒绝，任务／工作段结果仍能按原模型校验。以上不作为全仓库测试或 Windows 验收结论；修改仍保留在本地，未提交或推送。

[C 实现计划](adr/0008-c-implementation-plan.md) · [P2 提醒闭环](c-p2.md) · [观察契约](contracts/observation.md)
