# Jianying Performance Operator — Codex Skill

覆盖 macOS 剪映工程全流程：工程导入/迁移、打开/加载/时间线解析、素材重链、缓存校验与恢复、后台复合/算法任务队列、响度依赖以及导出准备。媒体导入只是其中一环。基于一次真实排查整理决策与证据边界，优先保留有效缓存，避免反复建新草稿、重链、重启和清缓存引起重复生成。区分工程解析、媒体/磁盘访问和后台任务积压；当前没有首次打开/解析补丁提速基准。不是自动修改剪映的插件，也不承诺通用提速倍数。

反复迁移丢链接时，使用 [迁移依赖与第二份导入验证流程](references/migration-and-cache.md)：原始目录复制不会自动修正外部绝对路径，“保留原有位置”不等于收集全部媒体，缺失生成 MP4 也不等于内层复合已压平。本机已验证 **草稿参数 → 修改 → 导入素材 → 复制至草稿** 能在 `Resources/local` 收集两条源 MOV，源文件保留且副本哈希一致；先重链、复制、保存复开，**再**批量配置响度/准备与导出，避免中途改变来源新增任务。仍需核对其他缺失与打包交付依赖、在第二位置/目标 Mac 导入验证；未测试前不承诺 `.jypack` / `.jyproj` 包含全部素材和缓存。

## 安装

需要已安装 Codex，并有 Git。仓库根目录就是 skill；Python 不是使用本 skill 的必要条件。克隆到持久目录，然后建立 Codex 可发现的符号链接：

```bash
skill_src="$HOME/.local/share/codex-skills/jianying-performance-operator"
skill_dst="${CODEX_HOME:-$HOME/.codex}/skills/jianying-performance-operator"
if test ! -e "$skill_dst" && test ! -L "$skill_dst"; then
  mkdir -p "$HOME/.local/share/codex-skills" || exit 1
  git clone https://github.com/JackLee992/jianying-performance-operator-skill.git \
    "$skill_src" || exit 1
  mkdir -p "$(dirname "$skill_dst")" || exit 1
  ln -s "$skill_src" "$skill_dst" || exit 1
else
  printf '%s\n' "Skill destination exists; inspect it before installing: $skill_dst" >&2
  exit 1
fi
```

安装前明确拒绝已有文件、目录、有效或失效符号链接，避免 `ln` 在已有目录内嵌套创建链接。重复安装先检查现有 skill 和克隆目录，不使用 `ln -sf` 覆盖。代码自动遵循自定义 `CODEX_HOME`。让 Codex 重新发现 skills（必要时开启新聊天/重启应用），调用：

```text
Use $jianying-performance-operator to investigate why this migrated project
keeps preparing compounds, preserve valid caches, and finish the export.
```

Skill 默认允许自动发现。自身不会执行任何命令；操作由 Codex 根据用户授权和当前可用工具完成。修改工程、退出应用或导出前先保存并确认范围。UI 自动化优先使用可访问性元素，不依赖历史坐标。原版应用、原工程、源媒体和安全策略保持可恢复。

## 内容与证据

[SKILL.md](SKILL.md) 是入口，按问题只读相关 references。已测到任务列表主线程压力、长内部复合的工作量、响度依赖和一次导出目录绕行；两任务并行已完成同一工程冷派生缓存 ABBA：串行均值 267.869 秒、两任务均值 268.886 秒，未测到提速；OS 页缓存未控制。一次单项任务取消与稍后重试通过，但关闭响度勾选没有取消已启动准备，取消全部/立即重试未验收。编码提交链背压仍是待核实假设。

两任务候选另完成约 20 分 55 秒原生导出完整解码与保存复开；约一分钟内四份缓存未见重写，不能外推长期稳定或首次打开加速。

原生分离音频准备虽约 5.252 秒完成，OFF/OFF PCM 对照失败，仍限于条件实验；不默认分离，也无完整导出倍速结论。工程打开/解析、后台准备、完整导出分别报告；特定补丁的 smoke/导出通过不能推广为所有版本提速。

二进制实验另见 [patch repository](https://github.com/JackLee992/jianying-macos-performance-patches)。本仓库无厂商软件、媒体、工程、私人路径或原始日志。MIT 只覆盖原创 skill 文本，不授予第三方软件权利。

如已有 Codex skill-creator，可运行其 `scripts/quick_validate.py` 检查本目录。卸载时只移除上面的 skill 链接；先确认是符号链接，不递归删除媒体、应用或工程。
