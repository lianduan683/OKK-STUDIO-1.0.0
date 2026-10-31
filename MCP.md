# OKK STUDIO MCP 接口

安装版可在“工程 → 启用 / 关闭 MCP 连接”开启本机桥；外部 MCP 服务使用源码包并按下方步骤创建 `.venv-mcp`。也可双击 `start-editor-mcp.cmd` 启动源码版。
软件沿用原有的缓存磁盘策略及 FFmpeg/MPV 资源；启动和预览仍需要原有运行资源。

接口提供 **23 个工具**，同时支持 stdio 和本机 Streamable HTTP。
通过 MCP 检测时，AI 模型由接入的 harness 提供；MCP 服务不调用模型 API，不要求软件额外配置模型密钥。
软件“调色 → 曝光检测”另支持用户自填三类模型 API，详见 [曝光检测说明](EXPOSURE.md)。

## 快速接入

1. 双击本目录的 `start-editor-mcp.cmd`，在源码版打开工程。也可以正常启动源码版，点击“工程 → 启用 / 关闭 MCP 连接”。
2. 将 `mcp-config.json` 中的服务条目加入支持 `mcpServers` 格式的 harness 配置。配置文件只作为示例提供，没有改动现有 harness 的全局配置。
3. 在 harness 中要求 AI：“连接 OKK STUDIO，统计当前工程每个素材用了几秒、当前切点多少，然后给我一个剪辑计划。”AI 应先调用 `get_project` 取得 revision，再调用 `connect_live`。

通用 stdio 配置（移动目录或换电脑后修改两条绝对路径）：

```json
{
  "mcpServers": {
    "okk-studio": {
      "command": "C:/path/to/OKK-STUDIO/.venv-mcp/Scripts/python.exe",
      "args": ["C:/path/to/OKK-STUDIO/source/okk_mcp_server.py"]
    }
  }
}
```

Codex 可以将 `codex-mcp.toml` 的条目加入用户配置 `~/.codex/config.toml`，或受信任工程的 `.codex/config.toml`。
也可以在 PowerShell 中执行下面的注册命令，然后重新加载 MCP 连接：

```powershell
codex mcp add okk-studio -- 'C:\path\to\OKK-STUDIO\.venv-mcp\Scripts\python.exe' 'C:\path\to\OKK-STUDIO\source\okk_mcp_server.py'
```

Codex 格式已对照[官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp)及本机 `codex mcp add --help` 核对。
不同 harness 的配置入口、包装字段和图片支持有所不同；本项目验证的是标准 MCP 协议，没有逐一运行所有品牌客户端。

## 安装源码服务依赖

在包含 `source` 的工程根目录执行（Python 3.12）：

```powershell
python -m venv .venv-mcp
.\.venv-mcp\Scripts\python.exe -m pip install -r .\source\requirements-mcp.txt
```

运行软件界面还需安装 `source/requirements-build.txt` 中的 GUI 依赖，及既有 FFmpeg/MPV 资源。
源码包中保留 `requirements-mcp.txt`，GUI 的本机连接桥本身不依赖 MCP SDK。

## 两种工作方式

### 连接正在运行的软件

开启菜单中的 MCP 连接后，本机桥写入 `%LOCALAPPDATA%\OKK STUDIO\mcp\<实例>.json`。
文件包含随机连接凭据，只用于本机进程，不要分享。退出软件或关闭连接会清理文件。

服务默认不自动选择软件窗口。可通过 `list_live_instances` 和 `connect_live` 选择实例；
单个可连接实例可省略实例 ID。也可以在服务参数中加 `--live`，或 `--instance <实例ID>`。
多实例必须明确选择。软件界面繁忙时拒绝应用修改；所有工程读写均在 Tk 主线程执行。

### 操作已保存的工程

无需启动软件即可读取和编辑 `.multicam.json` 或包含新时间线的 `.rtxproj`：

```powershell
.\.venv-mcp\Scripts\python.exe .\source\okk_mcp_server.py --project 'D:\Projects\演出.rtxproj'
```

也可启动空白服务，再调用 `open_project(path, expected_revision)`。
离线模式使用独立内存副本，修改后调用 `save_project_copy` 另存，再在软件里打开结果。
没有嵌入 OKK 时间线的旧完整工程，应先在软件中打开并另存。

### Streamable HTTP

```powershell
.\.venv-mcp\Scripts\python.exe .\source\okk_mcp_server.py --transport streamable-http --port 8766
```

支持该传输的本机 harness 可连接 `http://127.0.0.1:8766/mcp`。
该服务只监听本机，供本机进程使用，未提供远程认证或公网部署配置。
stdio 模式由 harness 启动子进程；命令执行后保持等待输入属于正常状态。
两种传输使用[官方 Python MCP SDK](https://py.sdk.modelcontextprotocol.io/run/)。

## 工具清单

| 工具 | 用途 |
| --- | --- |
| `get_capabilities` | 编辑操作字段、能力与边界 |
| `list_live_instances` / `connect_live` | 查看并连接软件实例 |
| `open_project` / `get_project` | 打开工程、读取 revision 和素材列表 |
| `get_usage_report` | 按片段、机位、原文件统计使用帧数/秒数、切点及空隙 |
| `get_timeline` | 分页读取成片选用范围和源素材入出点 |
| `get_subtitles` | 读取已有字幕供语义分析 |
| `get_source_frame` | 返回某机位在时间点的源帧，供多模态模型判断画面 |
| `start_exposure_scan` / `get_exposure_scan` | 开始只读曝光抽样，查询进度及本地指标 |
| `get_exposure_frame` | 返回检测缓存中的实际 PNG 图像和帧信息 |
| `submit_exposure_reviews` / `cancel_exposure_scan` | 回写模型判断到报告，或取消检测/复核 |
| `preview_edits` / `apply_edits` | 验证并应用同一份编辑计划 |
| `undo_edit` / `redo_edit` | 撤销/重做一批操作 |
| `save_project_copy` | 另存工程；明确覆盖时保留 `.bak` 备份 |
| `seek_playhead` | 定位软件播放头（实时模式） |
| `export_video` / `get_export_job` / `cancel_export` | 后台导出、查询结果、取消 |

另提供 `okk://capabilities` 资源和 `edit_assistant` 提示模板。
列表工具支持 `offset`、`limit`（1–200），返回 `next_offset`。

## 曝光检查流程

1. 实时模式先连接软件，再 `get_project` 获取 `revision`。
2. `start_exposure_scan(expected_revision=revision, scope="cameras", interval=5, max_frames=120, view="graded")` 返回 `scan_id` 和 `scan_revision`。`scope="program"` 检测成片选用的机位；`cameras` 检测各机位。
3. 轮询 `get_exposure_scan`，逐页读取抽样记录，再对待复核帧调用 `get_exposure_frame(scan_id, frame_id)`。该工具返回真实图像内容，harness 需支持图像输入。
4. 判断主体细节是否丢失，区分舞台暗背景、灯具反光、有意剪影与曝光错误。不能只照抄本地亮度标签。
5. 用下列结构提交到 `submit_exposure_reviews(scan_id, scan_revision, reviews, model)`：

```json
[
  {"frame_id":"00001","result":{"classification":"uncertain","confidence":0.6,"reason":"主体面部偏暗，需核对拍摄意图。","intentional_lighting":false,"regions":["画面中央面部"]}}
]
```

分类允许 `normal`、`overexposed`、`underexposed`、`mixed`、`uncertain`；`confidence` 是模型自评。
结果仅写入检测报告，不修改片段或调色，也不占剪辑撤销步骤。实时模式下检测结果同步显示在软件中。
工程或调色改变后，旧快照提交会被拒绝，需重新抽样。抽样不代表逐帧质检，最多 300 帧；
`graded` 含相应色轮、曲线和 LUT，不含转场、字幕和最终图层合成；图层工程请使用各机位检测。

## AI 剪辑流程

AI 先观察工程、源帧和已有字幕，依据用户要求生成操作数组。
操作支持：节目分割、原片分割、分段换机位、调整成片范围、移动/删除时间线片段、
机位命名、时间标记、音轨开关、图层模式和字幕更新。`get_capabilities` 给出每项准确字段。

示例：

```json
[
  {"op": "set_camera", "start": 0, "end": 5, "camera": 2},
  {"op": "split_program", "time": 3},
  {"op": "add_marker", "time": 5}
]
```

1. `get_project` 得到当前 `revision`。
2. `preview_edits(operations, expected_revision)` 返回 `plan_id`、`base_revision`、前后统计及新片段 ID，工程此时不变。
3. 检查后调用 `apply_edits(plan_id, expected_revision=base_revision)`。整批只占一个撤销步骤，界面立即刷新。
4. 按需要另存或导出。软件中手工编辑后旧 revision 会被拒绝，必须重新读取与预览。

一次最多 500 项操作；计划有效期 30 分钟，最多保留 20 份。原片分割产生的新 ID 在预览中返回，
可在应用后用于下一批计划。所有时间均为**主时间线的绝对秒数**，范围采用左闭右开，按工程帧率对齐。
`split_source` 遵守软件原有的音视频链接开关。删除只移除时间线实例，不删除源文件。
`set_camera` 会拒绝未录制区间；图层叠加开启时需先切回多机位模式，换机位才影响输出。
需要裁掉某段原片时，可先分割、根据返回 ID 选择，再删除不需要的实例；这不会自动波纹删除整个节目时间。

导出使用既有渲染引擎，默认 CPU H.264/AAC 和原分辨率，可查询/取消。导出读取一个冻结工程快照，
不阻止软件继续编辑。必须保持 MCP 服务运行到任务完成；现有输出文件会被拒绝。
FFmpeg/FFprobe 不在源码 `bin` 或 PATH 时，给服务加 `--app-dir '软件程序目录'`。

## 统计口径与能力边界

- `program_cut_count`：当前成片范围内部的实际机位切换与显式节目分割的去重切点，不把成片起点、终点当成刀数。
- `camera_switch_count`：实际机位变化次数；`explicit_split_count`：显式节目分割数，两者可能发生在同一帧。
- `source_split_boundary_count`：同机位、同原片且源时间连续的相邻素材实例边界，按原片及机位分别计数。
- `current_cut_boundary_count`：节目切点和上述原片分割按时间位置去重后的数量。
- `historical_cut_action_count`：返回 `null`。旧工程没有完整操作日志，无法可靠恢复历史上累计按过多少次剪切。
- 使用时长来自当前成片范围、机位选择、素材覆盖和原片入点，报告逐段源入/出点。未使用片段保留为 0。
- 图层模式按从上到下的可能可见图层统计；遇不透明图层停止累计更低图层。透明图层累计可能超过成片总长，未逐像素分析遮挡。
- 转场按名义切点归属统计，不将混合余量重复算入素材时长。音频使用时长未单独统计。
- 返回的是原片帧，未应用调色、转场和最终合成。没有内置 ASR、自动听音或独立视觉模型；内容判断由 harness 提供，已有字幕可供读取。
- 完整工程另存保留其它分节；实时编辑使用软件的现有撤销栈。当前持久化格式继续使用原有工程结构。

## 验证

```powershell
.\.venv-mcp\Scripts\python.exe -m unittest discover -s source/tests -p 'test_okk_*.py' -v
```

覆盖帧统计、负起点、范围与重叠片段、透明图层、计划原子性、版本冲突、另存备份与完整工程保留、
真实 Tk 时间线的应用/撤销、本机桥认证、过期请求，以及官方 MCP 客户端的 stdio/HTTP 交互。
使用合成媒体验证源帧和真实视频导出，并检查 2025-03-26 协议的客户端握手兼容。

历史源码测试结果（2026-10-31，并非本次构建完整测试结果）：MCP 与既有 CLI 相关 **29/29 通过**，其中新增 MCP 测试 25 项。
额外运行编辑模型、工程兼容、完整会话、快捷键与既有节目 UI 回归，65 项中 62 项通过、3 项失败。
3 项均位于原有 `test_program_edit_ui`：菜单含分隔项导致旧标签断言错误、连续方向键事件与旧断言不符、
工程已保存为格式 14 而旧断言仍要求格式 8。独立运行该文件、未加载任何本次 MCP 模块时仍复现相同失败。
本任务保留这些既有测试和对应功能，未把接口验证描述为全库回归通过。结果汇总见根目录 `MCP_VALIDATION.json`。
