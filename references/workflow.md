# 分析脚本与计划

`scripts/align.py` 依赖 Python 3、NumPy、SciPy、FFmpeg、FFprobe。先检查现有运行时；缺少依赖时使用已配置运行时或清楚说明缺少什么，不假定宿主有某个用户的 Conda 安装。下例绝对路径应替换为当前任务目录与技能目录。

## 执行

1. 用原生桥采集 `before.json`（见 `resolve.md`），选取目标串烧 `media.id`。默认候选为媒体池全部视频；有无关视频时可重复传入 `--source-id`。

```text
python <skill>/scripts/align.py prepare --snapshot <task>/work/before.json --target <media-id> --workdir <task>/work/alignment
python <skill>/scripts/align.py match --manifest <task>/work/alignment/manifest.json --out <task>/work/alignment/plan.json
```

`prepare` 读取源帧率。若 A1 已有该串烧，按它在时间线中的实际入点、裁切、位置和分段重建分析音频；A1 为空时使用完整目标音频，后续在结果副本追加主音频。当前帮助脚本不支持主音频变速或多个不同目标源；这时先调整清单或渲染实际主音轨作为分析目标，不能假装布局相同。

2. 看输出和 `plan.json`。`review` 列出需要复查的区间；可直接修正分段，无需重新计算所有素材。
3. 编译：

```text
python <skill>/scripts/align.py compile --manifest <task>/work/alignment/manifest.json --plan <task>/work/alignment/plan.json --out <task>/work/alignment/apply.lua --export <task>/outputs/project_aligned.drp --name 波形对齐完成
```

保留示范时加 `--resume-frame <相对时间线起点的帧数>`。如原片段结束于绝对帧 88747，时间线起点 86400，填 `2347`；不要传绝对帧。续接应先用第二组轨道时加 `--first-pair 1`。`--video-tracks 2,3 --audio-tracks 2,4 --master-track 1` 可修改布局；prepare 与 compile 的主轨选择须一致。

编译拒绝未处理的 `review`。实际复查后可修正计划、清空 `review` 并填写 `review_resolution`，或明确使用 `--accept-review`。这是要求代理检查证据，不是要求用户额外确认。

4. 用 Lua 桥执行 `apply`，然后：

```text
python <skill>/scripts/align.py verify --config <task>/work/alignment/apply.json
```

中间 WAV、快照、匹配点、计划和日志留在 `work/`，交付文件放 `outputs/`。缓存键包含文件路径、大小、修改时间。

## 计划格式

```json
{
  "target_duration": 40.0,
  "review": [],
  "segments": [
    {"media_id":"source-id","name":"原曲","target_start":0,"target_end":15.5,"source_start":30.25,"score":0.94},
    {"media_id":"source-id","name":"原曲","target_start":15.5,"target_end":40,"source_start":10,"score":0.91}
  ]
}
```

秒数相对于目标时间线起点；源时间相对于关联文件。第二段是同曲向后跳，应切开。同曲的多个连续片段使用同组轨道，换歌再切换。所有真实未知区间都需处理，不能伪造成连续匹配。

## 参数与局限

- 默认每 0.5 秒检查 2 秒窗，稳定段最少 1.5 秒；快速切片可用 `match --step 0.25 --window 1 --min-segment 0.5`，随后复查切点。
- 使用 8 kHz 单声道频谱定位，2 kHz 波形互相关确定偏移；它们用于分析，成品仍引用原视频。
- 相关系数约 0.5 且多窗偏移稳定通常值得检查采用；0.8 以上常是强证据。阈值不是通用质量保证，需看录音、静音和处理情况。
- 相邻源残差给出转场附近的候选边界，统一量化成时间线帧。交叉淡化没有唯一正确的硬切点，普通精度下取声音主导权交接附近即可。
- 不支持大幅变速、变调和不同演唱版本的自动套用。持续漂移时先查文件版本、时间线速度和采样解释，再决定分段或时间伸缩；不要一直增加微小片段。
