# Resolve 接入与写入

先发现现有连接器／脚本 API 和安装说明。Windows 开发说明通常在 `%PROGRAMDATA%/Blackmagic Design/DaVinci Resolve/Support/Developer/Scripting/README.txt`。以实际安装版本为准，不猜安装盘符。

外部脚本因访问范围、版本或动态库问题返回空对象／退出时，改用内置 Lua 控制台；不要重复启动崩溃的解释器或修改安全与外部脚本访问设置。

## 内置控制台

Windows UI 先遵循当前 computer-use 技能，选取真实返回的窗口，操作后刷新。通过“工作区 → 控制台”打开，不假定快捷键有效。

本次验证环境为 Resolve 20.0.0 beta 38，以下是实测而非所有版本的普遍规定：

- 全局 `resolve` 或 `fu:GetResolve()` 可用；`Resolve()` 和 `bmd.scriptapp('Resolve')` 返回空对象。
- 独立控制台窗口标题为 `Resolve`；切换／删除临时时间线后可能失去焦点。先观察、聚焦输入框，输入后确认文字，再单独 Return，读日志确认执行。
- 截图可能同时包含主窗口及有界控制台图像。使用最新截图坐标，不反复重试失效 UIA 索引或旧坐标。

```lua
bridge = dofile('C:/absolute/skill/scripts/resolve_bridge.lua')
bridge.snapshot(resolve or fu:GetResolve(), 'C:/absolute/task/work/before.json')
-- 分析并编译计划后：
bridge.apply(resolve or fu:GetResolve(), 'C:/absolute/task/work/alignment/apply.lua')
```

这是应用自身 Lua API，不可通过 UI 执行 PowerShell、CMD 或系统终端命令。

## 时间单位和版本差异

- 区分时间线帧率和源帧率。例如时间线 `24000/1001`，源可能是 12、24、30 或 `30000/1001`。
- `recordFrame = timelineStart + 相对时间线帧`。
- `sourceFrame = sourceSeconds × sourceFPS`。
- `sourceSpan = timelineDurationFrames × sourceFPS / timelineFPS`。
- 小数源帧保留音频入点。实测 `GetLeftOffset(true)` 返回精确时间线帧偏移，除以时间线帧率得到秒。`GetSourceStartFrame()` 返回整数，不能用它证明小数被丢弃。
- 实测 `endFrame = startFrame + sourceSpan` 得到目标时长，机械减 1 会短一帧。其他版本可能不同：必须检查新片段的实际 Start/End/Duration，有限次修正，不能把一次约定视为通用规则。
- 相邻切点只量化一次。12 fps 源放到约 24 fps 时间线时，源端点全取整可能无法达到奇数帧时长。
- 与已有片段重叠时，追加返回对象可能存在但其方法返回空值；先检查冲突，不能仅凭对象存在判断成功。
- Lua 方法无返回值时，`tostring((obj:Method()))` 可避免零参数 tostring 报错；但空返回值仍需检查版本支持，不能当成 0。

## 保留与完成

桥先备份工程、核对原时间线与快照一致，再复制为结果时间线。遇到已有结果名、导出文件或备份文件时先检查上次状态，不直接重复追加或覆盖。

分别用 `mediaType=1/2` 追加视频与原音频，指定各自轨道，二者入点、位置和时长一致后 `SetClipsLinked({v,a},true)`。保留主串烧；只静音识别为参考音频的轨道，不改变其他声音。

核对原时间线仍在、已有片段未变、主串烧未被移动／裁掉／叠加、新片段符合计划、素材在线、导出的 DRP ZIP 可读。失败时保留副本和日志供检查，不自动删除原工作。
