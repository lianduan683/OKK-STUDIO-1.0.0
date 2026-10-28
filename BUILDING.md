# 构建 1.0.1

需要 Windows x64、Python 3.13、PyInstaller 6.22.3、NumPy、tkinterdnd2，以及 Inno Setup 7.1.0。`source/requirements-build.txt` 是应用构建依赖说明。不要从用户工程、缓存或整个应用目录直接制作发布压缩包。

## 构建脚本

`build_release.py` 只读取指定源码与已审核的小型 UI 资源白名单。它每次在脚本所在目录的 `builds/` 下创建一个新的构建目录，不删除或覆盖既有构建，不修改源代码和系统安全设置。

已解压源码包时，可在该目录运行：

```powershell
python .\build_release.py --check --source-root .\source --iscc 'C:\Program Files (x86)\Inno Setup 7\ISCC.exe'
python .\build_release.py --source-root .\source --iscc 'C:\Program Files (x86)\Inno Setup 7\ISCC.exe'
```

`--check` 只检查源码版本、依赖入口、资源白名单和安装脚本，不调用 PyInstaller 或 Inno 编译器。实际构建命令会生成新 staging、PyInstaller onedir 程序、core.zip、内置 core 的 Setup.exe、源码 ZIP 和 SHA256SUMS.txt。

本地开发可用 `--source-root` 指向当前应用源码根目录，并用 `--old-release` 指向 1.0.0 构建目录以复用已审核小资源和 Python 许可。`--python` 可指定安装了 PyInstaller 等依赖的 Python 解释器，`--python-license` 可指定该运行时的许可文件。

构建结果和日志位于 `builds/<构建编号>/`。该目录中的 `public_repo/` 可供仓库文档更新，`artifacts/` 包含发布附件；`build_report.json` 和 `build.log` / `installer-build.log` 只供本机诊断，不加入公开源码 ZIP。

## 包内容

- 核心程序只含 PyInstaller 运行时、应用模块、16 个白名单 UI 小资源、许可证和公开使用文档。
- 源码 ZIP 另含 `source/tests/*.py` 公开回归测试；仅复制顶层 Python 测试代码，不包含生成的媒体、测试日志、测试缓存或子目录数据。测试代码不进入核心程序。
- 完整特效库和外部媒体运行库不复制进 core，安装时使用固定的 v1.0.0 资源包。
- 用户工程、个人媒体、work、backups、日志、代理缓存及工作区状态均不进入核心包和源码包。
- `resource_packages.json` 记录复用资源包的 URL / SHA256；`small_assets.json` 固定允许进入核心包的 UI 资源内容。
- 新构建默认不签名。正式面向启用 SAC 的设备分发前，应准备受信任的代码签名流程，并重新进行签名后打包与校验。

## 验收范围

本机可将新 core 与固定资源包解压到一个全新的验证目录，启动该目录中的 EXE 并用合成工程测试；这不会改写已有安装的卸载注册信息。测试缓存仍须满足程序原有的非 C 盘、非系统盘固定磁盘要求。

此方式验证运行时与资源完整性，不代表正式 Setup 的安装或升级注册流程已经实测。完整安装、卸载和沿用旧目录的升级流程应在隔离的 Windows 虚拟机中验证，避免覆盖正在使用的程序及未保存工程。
