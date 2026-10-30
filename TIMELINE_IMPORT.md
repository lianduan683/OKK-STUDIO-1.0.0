# 时间线导入

在编辑工作区选择“文件 → 导入时间线…”，也可从顶部“工程”菜单进入。选择文件和其中一条时间线，选择导入视图，然后检查片段数量、兼容性说明和离线素材，最后点击“导入时间线”。导入替换当前编辑时间线，可用 Ctrl+Z 恢复；完整工程中的其他工作区保持原样。

| 文件格式 | 常见来源与处理 |
| --- | --- |
| FCP7 XML `.xml` | Premiere Pro、DaVinci Resolve、Final Cut Pro 7 的交换文件。读取视频、音频、轨道、素材入点、机位嵌套和标记；多个 sequence 可选择。 |
| FCPXML `.fcpxml`、`.fcpxmld` | Final Cut Pro、Resolve。读取 asset-clip、复合片段、连接片段、多机位各角度及独立音视频角度选择。bundle 可选择其中的 `Info.fcpxml`。 |
| CMX 3600 `.edl` | Resolve、Premiere、Avid 等。导入切点和源文件注释；需指定帧率，支持 29.97/59.94 丢帧。第一条有效事件的记录时间归零，事件间空隙保留。 |
| OpenTimelineIO `.otio` | Resolve、OTIO 工作流。读取外部媒体、间隙、并行轨道及裁切后的嵌套；集合中的时间线可选择。 |
| AAF `.aaf` | Avid、Premiere、Resolve 等。内置 OpenTimelineIO AAF 适配器，转换为统一剪辑结构；支持外部链接媒体，嵌入媒体需先在源软件中提取并链接。 |
| MLT `.mlt`、`.kdenlive` | Shotcut、Kdenlive。读取 producer/chain、playlist、blank、tractor，以及音轨和逐帧入出点。 |

“合并为一条多机位时间线”将节目显示在一条视频轨上，保留全部机位源素材、节目切换点及独立音轨；右键节目轨“在时间线上打开多机位”显示各机位源轨。展开后可通过同一菜单“合并显示为多机位时间线”返回，文件菜单也提供两个入口。切换视图不渲染媒体、不改变时间偏移或节目切点，可撤销，并随工程保存。合并视图工程使用多机位工程格式 16，需要此版本或更新版本打开。

离线素材可按根文件夹递归重连，也可逐个指定。只有唯一的同名文件会自动匹配；存在同名文件时保留离线，避免链接到错误机位。也允许确认后保留离线结构导入。素材路径按时间线文件所在目录解析，支持 file URL、中文和空格。单文件上限 64 MB。

导入范围是剪辑结构，插件、调色、变换、合成、音量自动化、生成器和字幕样式不保证转换，读取时会列出说明。变速、倒放和不能明确还原的重叠转场会阻止导入，并提示先在源软件中渲染或转为切点。视频图层按上层优先生成节目切换；复杂透明合成请在源软件渲染。EDL 缺少可靠的源文件时间码起点，非零源时间码请使用 XML/OTIO 以确保入点准确。

Premiere `.prproj`、Resolve `.drp/.drt`、Vegas `.veg` 等原生工程不直接解析，请先导出上述交换格式。推荐跨软件交换优先使用 XML/FCPXML，其次使用 OTIO/AAF；EDL 适合简单切点清单。

格式依据：[Apple FCPXML DTD](https://developer.apple.com/library/archive/documentation/Miscellaneous/Conceptual/LegacyDTDsFinalCutPro/FCPXMLDTDv1.7/FCPXMLDTDv1.7.html)、[OpenTimelineIO 适配器说明](https://github.com/AcademySoftwareFoundation/OpenTimelineIO/blob/main/docs/tutorials/adapters.md)、[AAF 适配器](https://github.com/OpenTimelineIO/otio-aaf-adapter)。
