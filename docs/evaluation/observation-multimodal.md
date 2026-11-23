# 多模态 Observation 验收

## 对照与评分

旧版固定视觉和状态两次推理使用 `e05a734`；合并两组通过同一生产端入口运行，固定组只替换采样门。公开录像对照在每条录像上重新开始，旧版与固定合并每 30 秒抽帧。事件合并每 1.5 秒检查候选画面，首次等待前台稳定，上传至少间隔 30 秒。录像时间使用解码 PTS 且各组使用同一批原片。另以 4 秒抽样作为高密度诊断。

选用 GUI-World 的 S01-01 `software/212.mp4`（8.342 秒，开发界面）、S01-02 `website/175.mp4`（10.700 秒，短视频）、S05-03 `multi/45.mp4`（47.052 秒，研究网页）。复用冻结实验包的哈希与 9 个逐帧锚点，原标注由模型辅助目检，未做人审签字。评分只比较 `ide`、`video`、`browser` 三种屏幕类型；近期可回答要求采集时刻不晚于提问、相差至多 4.55 秒，且识别正确；历史可引用允许引用此前识别正确的画面并保留它的采集时刻。这两项衡量上下文能支持的回答，不是运行聊天生成后的正确率。录像缺少系统焦点、锁屏与可信键鼠，所有已发布的可打扰状态应为 `unknown`。[GUI-World 数据与 CC BY 4.0](https://huggingface.co/datasets/ONE-Lab/GUI-World)

## 公开录像实测

本机 Ollama `qwen3.5:2b` 运行两种提示结构。用量是本机实际返回值，这些调用没有云端费用。

| 30 秒对照 | 模型调用 | 输入／输出 token | 近期可答 | 历史可引用 | 非 `unknown` 发布 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 旧版分开 | 8 | 4742／423 | 2／9 | 6／9 | 0／4 |
| 固定合并 | 4 | 3447／288 | 2／9 | 6／9 | 0／4 |
| 事件合并 | 4 | 3461／285 | 1／9 | 4／9 | 0／8 |

事件组在两个短片各调用一次，在长片调用两次，本批调用数与固定合并相同。短期覆盖损失来自首次稳定等待、长片采样与评估锚点错位、以及开发画面被本地小模型分类为文档或视频。高密度诊断中三组分别调用 36、18、4 次，近期覆盖为 6／9、6／9、1／9。同一短片中的多个锚点相关，结果不提供样本外质量或节省幅度估计。

## 系统信号回放

90 秒夹具驱动生产者、SQLite、实时 reader 和 System 2 DSH 投影，使用合成前台、系统时钟、窗口图像和模型响应。旧版分开调用 8 次（5 图和 3 文，其中有一张合成 Mochi 窗口），固定合并调用 1 次，事件合并调用 3 次且在本地尝试了 4 次截图。固定组的 30 秒和 60 秒槽分别为 Mochi 和系统信号缺失，只有 0 秒槽可采。

| 90 秒回放 | 可回答文档检查点 | Mochi 窗口中的上一活动 | 未授权 DSH 名称 |
| --- | ---: | --- | ---: |
| 旧版分开 | 0／16 | 无文档名 | 0 |
| 固定合并 | 16／16 | `Beta.md`，确认于 24 秒，`previous` | 0 |
| 事件合并 | 16／16 | `Beta.md`，确认于 24 秒，`previous` | 0 |

短时切换到 B 时，事件组通过所授权的原生窗口标题取得名称，保持 B 消失前的确认时刻。锁屏和键鼠缺失场景不输出旧窗口为当前。缺少键鼠和锁屏资料的检查点在三组均保持 `unknown`。迟到夹具在 3 秒获得 A 的截图，20 秒收到模型后仍归 A 的原活动，SQLite 标为 `realtime_eligible=0`。在 12 秒撤销截图权限的相同夹具中，结果只记录 `redacted_out`，没有场景或名称负载。没有真实用户输入、真实 OS 采集时延或真人打扰偏好数据。

合并两组对 20 个快照中的 13 个当前活动声明检查了归属，均无错认。另把固定组的一次视觉结果保持在同一窗口超过 15 秒，最终投影仍可用新鲜原生事实确认 A，原图不作为当前场景。脏标志夹具在 9 秒向同一窗口的 A 加入 VS Code 的未保存标志。13.5 秒可读取 A 的 `unsaved_changes_appeared` 及 9 秒时间戳。进入 B 或 Mochi 后该事实仅保留在 A 的历史，无云端接收方文档授权时名称和标志均未透出。此次验证看到的是未保存标志出现，无法从标志确定具体修改或保存结果。

## 百炼能力与用量抽检

以中国内地的现有百炼 `qwen3.7-flash` 路由，分别验证三种公开录像的首帧。用隔离的路由设置副本记录服务返回的 token。公开原片没有原生系统信号或获准文档名称，抽检覆盖视觉与结构化回包。表中费用按公开价每百万输入 ¥0.2、输出 ¥0.8 估算，区间为 0 到 32K tokens。实际账单可能受免费额度、折扣或缓存影响。[阿里云百炼模型定价](https://help.aliyun.com/zh/model-studio/model-pricing)

| 真实请求 | 核对结果 | 输入／输出 token | 估算人民币 |
| --- | --- | ---: | ---: |
| 旧版视觉与状态，开发界面 | 旧视觉有用量但未形成有效记录。文本返回 `unknown` | 1219／135 | ¥0.0003518 |
| 合并，开发界面 | `ide`／`work`，识别代码编辑器和文件浏览区 | 1305／64 | ¥0.0003122 |
| 合并，短视频 | `browser`／`leisure`，场景要素包含视频播放器。粗类别没有命中 `video` | 1271／62 | ¥0.0003038 |
| 合并，研究网页 | `browser`／`study`，识别网页、结果区和导航 | 1271／50 | ¥0.0002942 |

五次真实请求合计约 ¥0.0012620。旧版与合并开发帧的单样本价格差为 −11.3%；两边提示、返回长度和识别成功情况不同。公开录像与系统回放的模拟调用不合并进云用量。

## 复跑

准备 FFmpeg、已加载 `qwen3.5:2b` 的本机 Ollama、原版和集成版源码、含 `case-manifest.json` 与 MP4 的冻结公开录像实验包。脚本将 JSON 账本和 SQLite 证据写入每个新目录，并检查原片哈希、SQLite 完整性和外键。下例从集成工作区运行，使用项目 Python 环境。

```sh
PYTHONPATH=. python scripts/check_observation_multimodal.py --suite public \
  --gui-world "$GUI_WORLD_DIR" --public-interval 30 --output /tmp/mochi-public-current
PYTHONPATH="$OLD_SOURCE_ROOT" python scripts/check_observation_multimodal.py --suite public \
  --arm split --gui-world "$GUI_WORLD_DIR" --public-interval 30 --output /tmp/mochi-public-baseline
PYTHONPATH=. python scripts/check_observation_multimodal.py --suite desktop \
  --output /tmp/mochi-observation-desktop
PYTHONPATH="$OLD_SOURCE_ROOT" python scripts/check_observation_multimodal.py --suite desktop \
  --arm split --output /tmp/mochi-desktop-baseline
PYTHONPATH=. python scripts/check_observation_multimodal.py --suite desktop --arm event \
  --late-vision-s 17 --output /tmp/mochi-observation-late
PYTHONPATH=. python scripts/check_observation_multimodal.py --suite desktop --arm event \
  --late-vision-s 17 --revoke-at 12 --output /tmp/mochi-observation-revoked
PYTHONPATH=. python scripts/check_observation_multimodal.py --suite desktop --arm event \
  --dirty-at 9 --output /tmp/mochi-observation-unsaved
```
