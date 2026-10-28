# OKK STUDIO 1.0.1 安装说明

## 在线安装和升级

1. 从 [v1.0.1 发行页](https://github.com/lianduan683/OKK-STUDIO-1.0.0/releases/tag/v1.0.1) 下载 `OKK-STUDIO-1.0.1-Setup.exe`。
2. 保存工程，退出正在运行的 OKK STUDIO，然后运行安装器。
3. 选择安装位置。已经安装 1.0.0 时，安装器会优先显示上次位置，沿用同一卸载登记；也可在安装向导中检查并选择位置。
4. 主程序直接从安装器安装；4 份资源包自动下载并分别核对 SHA256。安装完成后从桌面或开始菜单启动。

默认按当前用户安装，不要求管理员权限。安装器不包含显卡驱动。资源下载约 5.75 GB，完整程序约 6.5 GB，建议预留至少 15 GB 空间。

## 缓存磁盘

当前程序仍要求可写的非 C 盘、非系统盘固定磁盘用于缓存，优先使用 D:。只有 C 盘的电脑目前不能正常启动。

需要指定位置时，可在启动前设置环境变量 `OKK_CACHE_DIR`，例如 `E:\OKK STUDIO Cache`。指定位置仍必须符合固定磁盘和非系统盘要求；此版本没有改变该限制。

## 离线组件方式

Setup.exe 已含主程序，无需另下 core.zip。将下列 4 份原始资源 ZIP 放在同一个文件夹，**不要解压或修改 ZIP**：

- [OKK-STUDIO-1.0.0-resources-01.zip](https://github.com/lianduan683/OKK-STUDIO-1.0.0/releases/download/v1.0.0/OKK-STUDIO-1.0.0-resources-01.zip)
- [OKK-STUDIO-1.0.0-resources-02.zip](https://github.com/lianduan683/OKK-STUDIO-1.0.0/releases/download/v1.0.0/OKK-STUDIO-1.0.0-resources-02.zip)
- [OKK-STUDIO-1.0.0-resources-03.zip](https://github.com/lianduan683/OKK-STUDIO-1.0.0/releases/download/v1.0.0/OKK-STUDIO-1.0.0-resources-03.zip)
- [OKK-STUDIO-1.0.0-resources-04.zip](https://github.com/lianduan683/OKK-STUDIO-1.0.0/releases/download/v1.0.0/OKK-STUDIO-1.0.0-resources-04.zip)

在安装器所在目录打开 PowerShell，运行以下命令；把示例目录换成实际组件目录：

```powershell
& '.\OKK-STUDIO-1.0.1-Setup.exe' '/PAYLOADDIR=E:\OKK安装组件'
```

离线方式仍执行相同的 SHA256 校验。资源来自固定的 v1.0.0 发行版，主程序来自当前 1.0.1 安装器；无需重复下载 1.0.0 的 Setup 或 core.zip。

## 下载失败和校验失败

在线安装需要能访问 GitHub 及其发行版附件服务，下载使用系统代理设置。1.0.1 已移除 core.zip 在线下载步骤；资源包仍会校验，不会跳过完整性检查。

下载失败时可重试，或通过浏览器下载上述完整资源包后使用 `/PAYLOADDIR`。不要将 HTML 错误页、未完成下载或重新压缩的文件当作组件包。若仍失败，请保留安装日志和错误中显示的具体文件名。

## 智能应用控制与签名

当前安装器及主程序尚未做受信任的代码签名。Windows 智能应用控制可能提示“无法验证发布者”并拦截；这与下载哈希不一致是不同问题，关闭普通实时杀毒也不会解除 SAC 拦截。

此安装器不会关闭安全防护、修改执行策略、添加白名单或防护排除。长期解决方式是由发布者使用受信任的代码签名证书签署相关可执行组件。微软说明：[SAC 常见问题](https://support.microsoft.com/en-us/windows/security/threat-malware-protection/smart-app-control-frequently-asked-questions)。

## 工程和原版

安装升级不转换或删除个人工程与原片。继续工作时，在应用中打开原工程文件。原有源码文件夹及桌面便携 EXE 版保留；不要把安装目标手动指定为源码目录。

安装完成后不能只移动 `OKK STUDIO.exe`，必须保留完整目录结构和运行库。卸载安装版使用 Windows 的应用卸载入口。
