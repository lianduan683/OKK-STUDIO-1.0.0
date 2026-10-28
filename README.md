# OKK STUDIO 1.0.0

Windows 多机位视频剪辑完整安装版。此独立版本基于原 OKK STUDIO 5.12.4，保留深色工作区、时间线编辑、代理、透明叠加、调色、转场与特效库。

## 下载与安装

**[下载 OKK-STUDIO-1.0.0-Setup.exe](https://github.com/lianduan683/OKK-STUDIO-1.0.0/releases/download/v1.0.0/OKK-STUDIO-1.0.0-Setup.exe)**

运行安装器，选择安装位置后继续即可。安装器自动下载同一发行版的程序和资源包，逐包核对 SHA256，安装完成后提供桌面与开始菜单入口。无需手动拼接压缩包，也无需另装 Python、FFmpeg 或 MPV。

完整组件包含 Python 运行环境、FFmpeg、MPV、GPU 运行库、辅助工具和完整 337 项特效资源。组件总下载量约 5.78 GB，解压后约 6.47 GB；建议为下载、临时文件和安装目录预留至少 15 GB 空间。

安装到新的目录；原源码目录和先前 EXE 版保留。完整步骤与网络问题处理见 [INSTALLATION.md](INSTALLATION.md)。

## 运行条件

- Windows 10/11，64 位处理器和系统。
- 安装适合本机显卡的 NVIDIA、AMD 或 Intel 驱动。硬件编码格式由启动时的实际能力检测决定。
- **目前缓存需要可写的非 C 盘、非系统盘固定磁盘，优先使用 D:。只有 C 盘的电脑不能按当前缓存策略正常启动。** 可用 `OKK_CACHE_DIR` 指定其他符合条件的固定磁盘目录。
- 在线安装需要访问 GitHub 及其发行版附件下载服务。

## 功能

- 最多 6×6、36 机位监看，自定义网格；1080p 代理生成与已有代理链接。
- 时间线多选、交叉框选、拖动切点、上下换轨、删除、撤销和 Ctrl+滚轮缩放。
- 右键插入轨道；上方轨道处于画面顶层；多段素材首尾接齐合并。
- 透明视频堆叠、中文分类特效库、可浮动和组合的面板。
- 暂停保留缓存、返回窗口后同步呈现、过期帧回收与缓存限额。
- NVIDIA NVENC、AMD AMF、Intel QSV 编码路径；具体格式以本机检测结果为准。

Ryzen AI Max+ 395 / Radeon 8060S 可使用现有 AMD 适配路径，但尚未完成该设备的实机验证。软件目前不使用其 NPU。复杂透明合成仍可能降低帧率；不保证任意素材或机位数量下零延迟。

## 源码与许可证

[下载完整 1.0.0 应用源码与安装器构建脚本](https://github.com/lianduan683/OKK-STUDIO-1.0.0/releases/download/v1.0.0/OKK-STUDIO-1.0.0-source.zip)。源码包不含大型媒体、外部二进制、用户工程或缓存。仓库中的安装脚本和组件清单便于核对发行版；重新构建应用请使用源码包中的 `source/` 目录与 PyInstaller spec。

本应用的许可证见 [LICENSE](LICENSE)。外部运行库、辅助工具及资源按各自许可使用，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。原项目与历史版本位于 [lianduan683/OKK-STUDIO](https://github.com/lianduan683/OKK-STUDIO)。
