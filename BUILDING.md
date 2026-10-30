# 构建 OKK STUDIO 1.0.4

本次构建环境为 Windows x64、Python 3.12.10、PyInstaller 6.22.3、NumPy 2.5.4、tkinterdnd2 0.7.0、Inno Setup 7.1.1 x64。Python 依赖固定在 `source/requirements-build.txt`。

```powershell
python -m pip install -r .\source\requirements-build.txt
python .\build_release.py --check --source-root .\source --iscc 'C:\Program Files\Inno Setup 7\ISCC.exe'
python .\build_release.py --source-root .\source --iscc 'C:\Program Files\Inno Setup 7\ISCC.exe'
```

请将 `--iscc` 改为本机安装的编译器路径。脚本也会查找本目录 `tools/InnoSetup/ISCC.exe`、常见 Inno Setup 7 安装目录和 PATH。

构建脚本仅读取应用模块、顶层公开 Python 回归测试及 16 个白名单 UI 小资源，每次在 `builds/` 下创建新目录；不会覆盖已有构建。输出包括内置核心程序的 Setup.exe、维护用 core.zip、源码 ZIP、SHA256SUMS.txt 及公开仓库文件。

四份大型资源包不重新打包，继续使用 v1.0.0 的固定地址和 SHA256。用户工程、个人媒体、工作区状态、work、backups、日志、代理和缓存均不进入源码或核心包。

## 回归检查

测试使用合成媒体。FFmpeg/FFprobe 可放入 `source/bin` 或配置在 PATH；部分既有集成测试直接使用 `source/bin`。依赖真实媒体运行库的测试在缺少组件时跳过。为测试临时文件指定可写的 TEMP/TMP；完整应用测试还需要满足非系统盘缓存策略。

新增功能与兼容性相关测试包括：`test_camera_rename`、`test_audio_track_preview`、`test_audio_waveform`、`test_timeline_audio_ui`、`test_timeline_audio_model`、`test_timeline_audio_session`、`test_shortcut_markers`、`test_project_open_compat`，以及既有播放、工程和导出测试。

## 安装验证

正式安装脚本支持 `--define=RELEASE_QA` 编译隔离测试安装器：使用独立 AppId，省略桌面/开始菜单图标和安装后启动；文件与资源段相同。测试安装器不作为发布附件。真实升级目录、卸载注册及不同机器行为仍应在隔离 Windows 环境验证。

新构建默认不签名；受信任代码签名需使用发布方自己的证书与签名流程。
