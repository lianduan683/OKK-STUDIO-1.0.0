# 第三方组件与来源

OKK STUDIO 自有代码的许可证见根目录 `LICENSE`。本安装包中的外部组件不因与本应用一同分发而变为 MIT 许可；各组件原有版权与许可证文件保留在其对应目录。

| 组件 | 来源与随包说明 |
| --- | --- |
| Python 3.12 运行环境 | [Python](https://www.python.org/)；许可证来自 CPython 发行版 `LICENSE.txt`，应随包保留为 `licenses/Python-3.12-LICENSE.txt`。 |
| NumPy | [NumPy](https://numpy.org/)；许可及所含第三方代码说明见 `numpy-*.dist-info/licenses/`。 |
| Tcl/Tk | [Tcl/Tk](https://www.tcl-lang.org/)；随包许可见 `_tk_data/license.terms` 等对应目录。 |
| tkinterdnd2 / TkDND | [tkinterdnd2](https://github.com/Eliav2/tkinterdnd2)；许可见 `tkinterdnd2-*.dist-info/licenses/LICENSE` 和组件目录。 |
| FFmpeg | [FFmpeg](https://ffmpeg.org/)；Windows 二进制来源为 [Gyan builds](https://www.gyan.dev/ffmpeg/builds/)。GPU 导出运行库的精确下载地址与校验值见 `bin/gpu_export/runtime_sources.json`，许可见该目录 `LICENSE`。这些构建含 GPL 组件，不应描述为仅 LGPL 构建。 |
| MPV / libmpv | [mpv](https://mpv.io/) 与 [mpv 源码](https://github.com/mpv-player/mpv)。libmpv 使用 [shinchiro Windows 构建](https://github.com/shinchiro/mpv-winbuild-cmake)，精确来源和校验值见 `bin/gpu_live/runtime_sources.json`。保留 `mpv-Copyright`、`mpv-LICENSE.GPL` 和 `mpv-LICENSE.LGPL`；不可据此将实际构建描述为仅 LGPL。 |
| Gyroflow | [Gyroflow](https://gyroflow.xyz/) 与 [源码](https://github.com/gyroflow/gyroflow)；随包许可见 `bin/gyroflow/GYROFLOW_LICENSE.txt`。 |
| ExifTool / Perl 运行组件 | [ExifTool](https://exiftool.org/)；随包许可见 `bin/exiftool/exiftool_files/LICENSE` 和 `Licenses_Strawberry_Perl.zip`。 |
| Inno Setup | [Inno Setup](https://jrsoftware.org/isinfo.php) 与 [源码](https://github.com/jrsoftware/issrc)；用于构建安装器。安装器内的归档解压组件来自其发行版。 |

完整特效素材位于 `assets/effects_library/`，清单记录了素材身份与来源。应用 MIT 许可证不自动授予第三方素材的额外使用权；素材适用的许可仍以原权利方授权为准。

本文件是组件与许可证位置索引，不替代各组件许可证全文，也不改变其条款。
