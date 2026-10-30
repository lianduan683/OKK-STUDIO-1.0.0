# OKK STUDIO 1.0.2

Windows 多机位视频剪辑应用。本版新增逐轨音频控制、真实波形和 M 时间标记，支持机位命名，改善中文输入法快捷键，修复旧完整工程打开与保存。

## 下载

**[下载 OKK-STUDIO-1.0.2-Setup.exe](https://github.com/lianduan683/OKK-STUDIO-1.0.0/releases/download/v1.0.2/OKK-STUDIO-1.0.2-Setup.exe)** · [发行页](https://github.com/lianduan683/OKK-STUDIO-1.0.0/releases/tag/v1.0.2)

主程序已内置安装器。安装时下载并校验 v1.0.0 的四份固定资源 ZIP（约 5.75 GB），完整安装约 6.5 GB，建议预留至少 15 GB。已有离线资源可通过 `/PAYLOADDIR` 复用。安装与升级见 [INSTALLATION.md](INSTALLATION.md)。

## 新增与修复

- 右键机位自定义名称，名称随工程保存。
- A0 主音频、A1/A2 对应视频原声；可逐轨静音、同时混音或仅一轨出声，播放与导出一致。
- 后台提取真实波形并缓存，静音轨仍显示波形。
- 编辑区域适配中文输入法下的物理按键，文字框继续支持中文输入。
- M 键添加时间标记，支持定位、删除、保存和撤销重做。
- 统一旧 `.rtxproj` 与独立 `.multicam.json` 打开入口，修复完整工程保存路由，保留帧率精度。
- 改善音频时钟、漂移校正和播放器/波形任务释放。

更新详情与验证边界见 [CHANGELOG.md](CHANGELOG.md)。

## 使用条件

Windows 10/11 64 位。缓存沿用可写的非 C 盘、非系统盘固定磁盘策略，优先 D:。仅有 C 盘的电脑目前无法按该策略正常启动。硬件编解码能力以启动检测和实际驱动为准。

本版尚未做受信任代码签名，Windows 智能应用控制可能拦截。新设置或标记会使用工程格式 11，需要新版打开；升级后建议另存副本。

## 源码与构建

[下载 1.0.2 应用源码](https://github.com/lianduan683/OKK-STUDIO-1.0.0/releases/download/v1.0.2/OKK-STUDIO-1.0.2-source.zip)。源码包包含应用模块、回归测试、小型 UI 资源和构建文件；不包含用户工程、媒体、工作缓存或大型第三方二进制。构建步骤见 [BUILDING.md](BUILDING.md)。

应用许可证见 [LICENSE](LICENSE)，第三方组件见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
