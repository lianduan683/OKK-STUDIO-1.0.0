# 曝光检测与模型接入

入口：底部“调色” → “曝光检测”，或“工程 → 曝光检测”。保留独立调色页的色轮、RGB 曲线、标准机位匹配及统一导出页。

## 使用

选择“当前片段 / 整条成片 / 所有机位”，再选择原片或当前调色效果，设置间隔及抽样上限，点击“开始本地筛查”。本地计算暗部、高亮和通道剪切比例，标出待复核疑点。

选择结果可看抽样图和依据，双击或点击“定位时间线”回看。模型 API 可复核所选帧；每次最多 20 帧。“选择未复核疑点”只选择本地提示异常且尚未复核的帧，也可手动选择正常帧检查漏报。报告可导出 JSON。

## 三类接口格式

“模型接入 / MCP”中提供以下选项，模型名须由用户填写，并支持图片输入。兼容服务或中转服务可填写自己的 Base URL。

| API 家族 | 界面选项 | Base URL 示例 | 请求路径与认证 |
| --- | --- | --- | --- |
| OpenAI 兼容 | OpenAI · Chat Completions | `https://api.openai.com/v1` | `/chat/completions`，Bearer Key |
| OpenAI 兼容 | OpenAI · Responses | `https://api.openai.com/v1` | `/responses`，Bearer Key |
| Anthropic | Claude · Messages | `https://api.anthropic.com/v1` | `/messages`，`x-api-key` |
| Google | Gemini · generateContent | `https://generativelanguage.googleapis.com/v1beta` | `/models/{model}:generateContent`，`x-goog-api-key` |

Base URL 应包含版本路径，也可填完整请求端点。JSON 模式为可选项，服务支持时开启。三类格式对应四个可选请求协议。

点击“发送所选帧至模型复核”后才向该地址发送所选 PNG 和辅助曝光指标；本地筛查和保存连接信息不会调用模型。请求无自动重试；失败后保留已有本地及模型结果，提示检查端点、模型、Key 或配额。取消会停止后续帧，正在执行的网络请求可能等待至超时。

Key 为掩码输入，只保留当前会话，可手动清除。保存连接信息仅写入协议、地址、模型名、超时与 JSON 选项，不保存 Key。连接配置位置：`%LOCALAPPDATA%\OKK STUDIO\exposure-model.json`。模型返回内容必须符合检测 JSON，解析失败不会伪装为“正常”。

请求结构依据 [OpenAI Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)、[OpenAI Responses JSON 输出](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses)、[Claude Messages](https://platform.claude.com/docs/en/build-with-claude/working-with-messages) 和 [Gemini generateContent](https://ai.google.dev/api/generate-content)。各供应商及中转服务支持的模型、权限与附加要求以其实际服务为准。

## 通过已有 MCP 检测

MCP 保留既有接口，新增 5 个曝光工具，总计 23 个工具。外部 harness 读取真实抽样图，使用自身的视觉模型判断，再回写同一份报告；软件不需要接收 harness 的 Key。步骤和字段见 [MCP 说明](MCP.md)。

源码包中的 `mcp-config.json` 是配置示例，需按源码目录位置修改并建立 `.venv-mcp` 环境。启动软件后开启 MCP，再让 harness 先 `get_project`、`list_live_instances`、`connect_live`。支持 stdio 和本机 Streamable HTTP。配置示例未自动改动用户的全局 harness 设置。

## 判断范围

检测按间隔抽样，最多 300 帧。超出上限时均匀抽取；未采样时刻仍可能存在问题。暗部/高亮比例是疑点指标，不是拍摄质量结论。模型需结合主体、细节及有意明暗判断；模型置信度属于自评。

“当前调色效果”包括机位、选用片段、全局调色及 LUT；不包括转场、字幕和最终图层合成。HDR PQ/HLG 原素材会标为需复核，当前指标未经显示亮度标定。检测及模型复核仅写报告，不自动修正曝光；工程变化后应重新检测。

## 本机验证

已用合成明暗视频验证实际 FFmpeg 抽帧、原片入点、调色前后差异、HDR 提示、取消及工程保持不变；通过本地模拟 HTTP 服务验证四种协议的图片、认证与返回解析；验证 MCP stdio、HTTP、实时界面同步和旧快照拒绝。

未使用用户的 Key 调用付费供应商，真实服务仍需填入用户自己的视觉模型与 Key。安装版使用安装器提供的运行资源；MCP 源码服务需另行准备 Python 环境。
