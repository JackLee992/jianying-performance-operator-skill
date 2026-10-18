# 只读缓存观测：看真实文件变化，不猜总任务数

`scripts/observe_cache.py` 用 Python 标准库在 macOS 上观察明确指定工程的物理缓存、选定主进程 CPU/RSS 与可用磁盘空间。无需 pip 包，要求 Python 3.9 或更新版。它不启动/保存/退出应用，不操作 UI，不读媒体内容或做哈希，不改工程/缓存，不自动取消任务或杀进程。它不实现 Parallel2 身份 guard。

## 操作

先确认工程和缓存归本次操作所有、Resources 没有逃出工程的链接；保存、重链和导入设置按 migration reference 完成。观测可以伴随生成运行，但不会把运行中的目录变成一致快照。另选一个**已存在、可写、位于工程之外**的测量目录。所有路径明确传入；替换以下占位路径，从 skill 仓库根目录运行：

```bash
python3 scripts/observe_cache.py \
  --project '/path/to/owned-project' \
  --app '/path/to/selected-Jianying.app' \
  --bench-root '/path/to/owned-measurements' \
  --output '/path/to/owned-measurements/run.jsonl' \
  --stop-file '/path/to/owned-measurements/run.stop' \
  --duration 7200 --interval 5 \
  --warn-free-gib 30 --request-stop-free-gib 25
```

`--app` 也可直接指定所选应用的 `Contents/MacOS/VideoFusion-macOS`。主进程仅按完整 `comm` 精确匹配，不收集全部参数或 CEF 子进程；未匹配就报告未知，不猜 PID。RSS 不是物理 footprint，也不包括整个进程树。

输出和默认 `run.jsonl.summary.json` 必须是新文件；已有文件拒绝覆盖。`--summary` 可指定同一测量目录内的新路径。日志/摘要权限为 0600，包含运行路径、文件身份和时间等本地信息，保存在本地，不把实际 JSONL/摘要上传公开仓库。目录不会自动创建。

创建指定 stop 文件即可只停止观测器；不会取消剪映任务，文件也不会被观测器删除。预先存在的 stop 文件会在首次扫描前停止本轮。低空间警告和 `request_native_stop` 也只是发事件，操作者仍需在原生界面决定是否取消/保存；它们**不自动停止应用或观测器**。阈值按实际磁盘调整，不是通用容量建议。

默认时长 7200 秒，允许 `(0, 43200]`；间隔 0.25–5 秒，默认 5 秒，每次 sleep 不超过 5 秒。工作超时会跳过追赶扫描，不做紧密循环。JSONL 每行 flush，结束时写 terminal 并原子发布一个新摘要；异常断电时没有 terminal 就标注测量不完整。原生动作 T0 由操作者另外记录，不能把观测器启动当作开始准备的时间。

## 怎样解释输出

| 字段/现象 | 能说明什么 | 不能说明什么 |
|---|---|---|
| `unique_observed_output_families` | 本轮至少出现一次的规范缓存目录/UUID family 数 | 全部任务数、当前成片必需数或最小任务数 |
| `current_named_final_color_alpha_pairs` | 当前非空最终命名 color 与 alpha 同组 | 原生完成、编码可读、内容正确或缓存一定可复用 |
| `current_temporary_output_families` | 本次仍有规范临时命名输出的 family | 活动 worker 数或真实总进度 |
| `lifetime_observed_positive_file_growth_bytes` | 从首样本后观察到的正向文件增长估计 | 实际写盘字节、SSD 吞吐或原生百分比 |
| `observable_attempt_sequence` | 该 family 被观察到缩小、重现或 inode 替换的证据序号 | 原生任务身份或准确重试次数 |

color 与 alpha **是一组**。识别规范 UUID 加精确后缀，包括 `_video.mp4`、`_video.mp4.alpha.mp4`、`_video.mp4_temp.mp4` 及其 alpha 侧文件；也接受明确的独立 alpha/audio 后缀。大小写 UUID 归一，同一 UUID 在不同父目录仍是不同 family；任意 `.bak`、未知后缀和非 UUID 名称不猜成任务。临时/最终名称并存仍只算同一 family。

首样本增长为 0，把已有文件当基线。以后按 device/inode 记录正向 size 差；新身份加入当次看到的 size，同 inode 临时→最终重命名不重复计数，硬链接不重复计增长。观察到缩小只记事件不加增长，之后再长只加新的正增量；消失后同身份重现与最后一次 size 比较。净存量 `logical_growth_since_start` 可以下降，与累计正增长不同。

这些是采样证据：同大小重写、两次采样之间完整发生的写/截断/删除、inode 重用和未扫描路径都可能看不到或有歧义。稳定几个样本不等于完成。必须结合原生身份/完成事件、输出最终化和后续完整解码；Parallel2 的当前行显示串扰风险见 benchmark reference。

真正的最小任务数仍需**活动成片的递归依赖闭包与有效缓存判定**，本工具不解析不透明工程或读取原生队列，不提供任务总数分母，也不按 family 数自动去重、清缓存或缩短响度分析区间。

## 物理范围与验证

扫描候选是工程内 `Resources/combination`、`Resources/audioAlg`/`audioAlg`、`Resources/videoAlg`/`videoAlg`、`Resources/loudness`/`loudness`。缺失目录正常。只 stat 真实普通文件，不跟随内部 symlink，内部链接记为跳过；缓存链接逃出工程或无法确认目标时，记录拒绝并停止观测。输出/控制路径必须在声明的测量目录内、工程外，且不能经过符号链接父目录。

九个小型文件系统 fixture 覆盖临时 color+alpha 分组、同 inode 重命名、截断/再增长、删除重建、原路径替换、硬链接、严格后缀与共存名称、symlink 归属、输出路径及只读 CLI。它们使用临时占位文件，不启动剪映；不证明真实工程原生完成或性能收益。

```bash
python3 -m unittest discover -s tests -v
```
