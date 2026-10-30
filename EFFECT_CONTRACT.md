# OKK 特效与转场标准接口 v1.0

本规范用于把不同来源的效果接入 OKK STUDIO：先转换为声明式效果包，再编译到现有渲染对象。特效与转场共用元数据、参数、资源、版本和校验机制，区别是特效接受一路画面，转场接受 A/B 两路画面。

当前交付是可调用的 Python SDK、JSON Schema、命令行工具和示例。**尚未接入特效库的图形化导入按钮，也不会自动解析任意达芬奇/PR 文件。** 不改变已有工程保存格式；新接口由调用方接入项目编辑、撤销、保存和渲染。

## 1. 统一的数据流

```text
达芬奇 / Premiere Pro / 其他来源
  → 来源适配器：识别节点、参数、动画和外部依赖
  → ConversionReport：可映射项、未支持项、还原程度
  → 效果包 effect.json + assets/
  → load_package()：结构、路径、资源哈希校验
  → compile_effect()：实例参数与时间校验，生成原生对象
  → 同一编译结果交给预览或导出
```

第三方预设不能只改扩展名当成 OKK 效果。PR 预设记录效果及其设置，实际效果由宿主和插件执行；PR 的效果/转场插件有对应 SDK 接口。[Adobe 预设说明](https://helpx.adobe.com/premiere/desktop/add-video-effects/apply-video-effects/effect-presets.html)、[Premiere 插件类型](https://ppro-plugins.docsforadobe.dev/intro/premiere-pro-plugin-types/)。Fusion 宏/模板有 `.setting` 等宿主文件与模板机制，需分析其节点和依赖后转换。[Blackmagic Fusion 手册](https://documents.blackmagicdesign.com/UserManuals/FusionManual.pdf)。

## 2. 效果包和实例分开

```text
my-effect/
  effect.json           # 一个版本的效果定义
  assets/
    media.mov           # 可选：叠加视频、灰度 PNG 或 LUT
```

一个包对应一个效果；一个效果可在时间线上有多个实例。实例只保存引用、启用状态、时间和参数覆盖值。多个效果按调用方提供的列表顺序执行；v1 不提供通用节点图调度器，不能把多个对象的 FFmpeg 字符串直接拼接成任意复杂图。

| 顶层字段 | 约定 |
| --- | --- |
| `schema` | 固定 `okk.effect/1.0`，未知协议版本拒绝加载 |
| `id` | 稳定、带命名空间，如 `com.example.effects.glow`；禁止用显示名称作标识 |
| `version` | `主.次.修订`，实例精确锁定版本；升级由调用方显式迁移 |
| `kind` | `effect` 或 `transition` |
| `name/category/description` | 特效库显示和检索信息 |
| `source` | 来源应用、格式、原始标识、还原程度和转换说明 |
| `render` | 输入数量、色彩空间、Alpha 和时间邻域要求 |
| `implementation` | 选择已经实现的渲染适配器，不是任意命令或代码入口 |
| `parameters` | 参数 ID → 类型、名称、单位、默认值、范围/枚举、动画支持 |
| `assets` | 资源 ID → 包内相对路径、SHA256、用途及叠加视频帧率 |

所有对象拒绝未知字段，防止拼错参数后被静默忽略。JSON 限制 1 MiB，拒绝重复字段和 NaN/Infinity。资源使用 `/` 分隔的相对路径，拒绝绝对路径、`..`、盘符和解析后越界的链接；加载和编译时均校验 SHA256。文件类型和哈希校验不等于媒体解码校验，导入器仍需调用 FFprobe 检查实际编码、帧率及 Alpha。

完整结构见 `source/effect_contract_schema.json`。Schema 负责结构约束；SDK 继续检查参数单位、后端能力、资源内容和时间关系，**Schema 通过不能替代 SDK 编译**。

## 3. 当前可用的渲染适配器

| engine | 输入 | 输出原生对象 | 可执行内容 |
| --- | --- | --- | --- |
| `okk.grade.v1` | 1 路 | `creative_core.ColorGrade` | 曝光、对比、饱和度、色温、色轮等数值；可引用 LUT |
| `okk.effects.v1` | 1 路 | `effects_core.EffectSettings` | 现有亮度/对比/饱和/伽马、锐化、模糊、暗角、淡入淡出、翻转与直角旋转 |
| `okk.overlay.v1` | 1 路 + 资源 | `effects_core.EffectSettings` | 直通 Alpha 或黑底滤色视频叠加、不透明度 |
| `okk.xfade.v1` | A/B | `transitions_core.TransitionSpec` | 现有独立转场渲染器的 xfade 模式 |
| `okk.mask.v1` | A/B + 资源 | `transitions_core.TransitionSpec` | 静态灰度 PNG 的线性排序擦除 |

节目时间线当前支持的 xfade 模式少于独立转场渲染器。调用 `compiled.program_transition()` 时再检查节目支持能力，不支持则返回 `E_HOST_CAPABILITY`，不会偷偷替换成叠化。`capabilities()` 返回两套模式清单。

叠加视频按声明帧率从零开始、循环、铺满主画面，并使用现有渲染器的缩放与合成方式；不支持素材入点、保持末帧、变速、任意混合模式或资源音频。支持的容器为 MOV/MP4/MKV/WebM，但能否解码仍取决于当前 FFmpeg。Alpha 素材需包含有效透明通道。

v1 只接受宿主预先归一化到 **Rec.709 显示域、straight Alpha** 的输入；接口声明不会自动完成 ACES、Log 或 HDR 变换，也不保证全链路保留 Alpha。`temporal_radius=0` 表示不支持读取主素材的前后帧，不限制叠加视频自身带动画。已有编解码位深、像素格式及缩放由渲染调用方决定。

## 4. 参数和时间规则

参数类型：`number/integer/boolean/enum`。参数 ID 直接对应适配器的字段名；导入器负责把源参数名、单位和取值变换为这些字段，不能靠显示名称猜测。

| 引擎/参数 | 单位 | 中性值/范围 |
| --- | --- | --- |
| grade.exposure | `ev` | 0；-4～4 |
| grade.contrast/saturation | `ratio` | 1；0～3 |
| grade 其他数值参数 | `ratio` | 范围按 SDK 中现有 ColorGrade 定义 |
| effects.brightness/contrast/saturation/gamma | `percent` | 0；-100～100 |
| effects.blur/sharpen/vignette | `percent` | 0；0～100 |
| effects.fade_in/fade_out | `seconds` | 0；0～10，合计不得超过实例时长 |
| effects.rotation | `degrees` | 枚举 0/90/180/270，顺时针 |
| effects.hflip/vflip | `boolean` | false |
| overlay.opacity | `ratio` | 1；0～1 |

例如 PR 的模糊像素半径与 OKK 的模糊百分比不是同一单位，转换器必须说明使用的映射算法和差异。当前 OKK 模糊 0～100 对应已有渲染器 sigma 0～5；这不能证明与源软件模糊逐像素一致。

v1 参数 `animatable` 必须为 false，实例 `keyframes` 必须为 `{}`。已保留动画入口，但目前遇到动画会明确拒绝；不能丢弃源关键帧后称为成功转换。源软件中的动画可以先渲染到叠加素材中保留。

帧率用有理数 `{num:30000, den:1001}`。所有帧编号从 0 开始，时长为整数帧；最后才用有理数转换为旧渲染器的秒。

- 特效：`timing={start_frame, duration_frames}`，作用范围为左闭右开的 `[start, start+duration)`，本地特效时钟从 0 开始。调用方负责截取素材和应用该范围。
- 转场：`timing={cut_frame, duration_frames}`，A 为出画、B 为入画；节目模式窗口以切点居中，保持主时间线总时长。持续时间只支持 0.1～4 秒；不自动缩短。
- 当前转场适配器只接受 1～120 FPS，叠加素材只接受 1～240 FPS；超出旧渲染器能力时报错，避免被其内部归一化过程静默改帧率。调用方须将输入帧率统一到实例帧率。
- 独立拼接渲染器使用重叠拼接模型，可能缩短总时长。`TransitionSpec` 本身不决定时间线语义，调用方必须选择正确的宿主路径。
- 灰度遮罩以暗到亮的顺序揭示 B，动画由既有线性阈值算法生成。遮罩不能表达源转场的缩放、畸变或时间变化。
- 宿主还必须检查切点确实存在、工程末尾、相邻转场是否重叠及 A/B 素材余量；无足够余量时报错，不能用冻结帧掩盖。

## 5. SDK 调用

在 `source` 目录或将该目录加入 Python 模块搜索路径后：

```python
from effect_contract import load_package, read_json, compile_effect
from effects_core import build_effect_filters

package = load_package('effect_contract_examples/blur')
instance = read_json('effect_contract_examples/blur-instance.json')
compiled = compile_effect(package, instance)

# 先依据 anchor_seconds / duration_seconds 截取素材。
# 预览和导出消费同一个 compiled.native，调用方负责图标签和资源生命周期。
filters = build_effect_filters(compiled.native, duration=compiled.duration_seconds)
# ['gblur=sigma=2.2500']
```

`compiled.native` 的类型由上表确定；不能把 grade/transition 传入 `build_effect_filters`。带叠加的 EffectSettings 使用现有 `build_effect_filter_graph`。ColorGrade 使用 `grade_filter`，TransitionSpec 使用 `build_transition_video_graph`。禁用实例返回 `native=None`，调用方直接旁路。

转场可适配到节目模型：

```python
package = load_package('effect_contract_examples/dissolve')
instance = read_json('effect_contract_examples/dissolve-instance.json')
compiled = compile_effect(package, instance)
transition = compiled.program_transition()  # 切点 4 秒，时长 0.8 秒
# 通过现有编辑事务添加 transition，再对完整候选工程执行 validate/余量检查。
# 独立拼接时传 compiled.native，并以 compiled.asset_dir 作为 mask_dir。
```

`compile_effect(..., base_grade=existing_grade)` 只替换定义中的调色参数，保留已有 LUT/曲线等其他字段，并复制输入对象。省略 base_grade 则从默认值开始。SDK 不写工程、不启动渲染。

`compiled.fingerprint` 包含效果包、实例、资源哈希、适配器版本和生成的原生参数。它是效果描述指纹，**不能单独用作最终帧缓存键**。宿主缓存键还须加入素材内容/入点、渲染时间、输出尺寸、像素格式、色彩变换、渲染器版本和前序效果签名。资产重新定位可能改变含绝对路径的原生描述指纹。

## 6. 命令行与素材转换

在 `github-release-work/source` 目录执行：

```powershell
python effect_package_cli.py capabilities
python effect_package_cli.py validate effect_contract_examples/blur
python effect_package_cli.py compile effect_contract_examples/blur effect_contract_examples/blur-instance.json
python effect_package_cli.py compile effect_contract_examples/dissolve effect_contract_examples/dissolve-instance.json
```

导出素材后可以直接封装；以下命令中的路径需换成真实素材，新建的目标目录不得已存在：

```powershell
# 达芬奇 / PR 中先导出带 straight Alpha 的 CFR 叠加视频，再封装。
python effect_package_cli.py pack D:/exports/glow.mov D:/effects/glow-v1 --id com.example.glow --name 柔光 --kind overlay --application davinci-resolve --source-format fusion-render --fps 30000/1001 --blend alpha

# 黑底光效选 --blend screen；灰度排序图选 mask；已导出的调色 LUT 选 lut。
python effect_package_cli.py pack D:/exports/wipe.png D:/effects/wipe-v1 --id com.example.wipe --name 画笔擦除 --kind mask --application premiere-pro --source-format exported-mask
python effect_package_cli.py pack D:/exports/look.cube D:/effects/look-v1 --id com.example.look --name 胶片风格 --kind lut --application davinci-resolve --source-format cube
```

封装器复制并校验资源后发布完整目录，不会转码或验证像素内容。所有包均需再配一个实例使用；本工具不自动安装到软件库。视频 `.mov` 也可能没有 Alpha，需要导出者确认，不能仅根据扩展名判断。

## 7. 达芬奇 / PR 转换器的标准接入点

```python
class SourceAdapter(Protocol):
    def inspect(self, source: Path) -> ConversionReport: ...
    def convert(self, source: Path, destination: Path) -> ConversionReport: ...
```

`ConversionReport` 包括 `fidelity/mapped/unsupported/warnings/package_path`。适配器必须先列出未支持特性；`unsupported` 非空时，不得将只转换部分内容的结果静默发布为完整转换成功。若用户选择烘焙，需要记录烘焙方式、丢失的编辑能力和限制。

| 转换程度 | 意义 |
| --- | --- |
| native | OKK 原生定义 |
| mapped | 明确映射的参数或已导出的 LUT；不表示与来源逐像素一致 |
| approximate | 算法或表现近似，必须说明差异 |
| baked | 已渲染为素材，可保留素材内动画，失去源节点编辑能力 |

| 来源内容 | 转换策略 | 当前交付 |
| --- | --- | --- |
| 已导出的 LUT | 引用资源 → grade | 已实现封装 |
| 已导出的透明/黑底视频 | 作为叠加素材 → overlay | 已实现封装 |
| 已制作的灰度排序图 | 线性阈值擦除 → mask | 已实现封装，通常为近似 |
| `.prfpset` / Fusion `.setting` / `.drfx` | 按版本解析结构、逐项映射节点/参数/动画，再生成标准包 | 仅定义来源适配器接口；未实现通用解析 |
| OFX / PR 原生二进制插件 | 实现对应宿主 ABI、资源与参数生命周期，或重写算法 | 未实现插件宿主，不能改名或解包后直接执行 |
| 完整双画面变形/光流/跟踪转场 | 重建双输入算法；无法重建时需要源宿主渲染 | v1 无通用动态双输入图；叠加视频不能替代它 |

OpenFX 的插件执行需要宿主提供图像、参数及渲染动作等接口，因此本标准包不等于一个 OFX 宿主。[OpenFX 渲染接口](https://openfx.readthedocs.io/en/latest/Reference/ofxRendering.html)。

后续新增渲染器应在 SDK 中显式注册实现、声明能力并添加验证；效果包自身不得携带任意 Python、DLL 入口、shell 或 FFmpeg 命令。需要新能力时更新协议及适配器版本，旧版保持明确拒绝。

## 8. 错误和接入验收

所有 SDK 业务错误继承 `ValueError`，并提供机器可读 `ContractError.code`；CLI 成功返回 0，校验/IO 失败返回 2 并输出 JSON 错误。

| 主要错误码 | 含义 |
| --- | --- |
| E_SCHEMA / E_VERSION | 结构、未知字段或协议/效果版本不符 |
| E_PARAMETER / E_PARAMETER_BINDING | 参数值、类型、单位或后端字段不符 |
| E_ASSET_PATH / E_ASSET_MISSING / E_ASSET_HASH / E_ASSET_FORMAT | 资源越界、丢失、内容变化或格式不支持 |
| E_TIMING | 帧率、帧范围或转场窗口不合法 |
| E_BACKEND / E_CAPABILITY / E_HOST_CAPABILITY | 后端或宿主能力不足 |
| E_UNSUPPORTED_ANIMATION | 参数动画当前不能执行 |
| E_NATIVE_VALIDATION | 不满足已有原生模型约束 |

接入转换器后，需使用固定合成画面和同一色彩管理设置，对照源软件的起始/中间/结束帧、参数边界、透明通道、非整数帧率和资源缺失情形。预览与导出必须使用同一个实例参数快照；实际还原程度应以对照样片为准。目前没有提供真实 PR/达芬奇源预设，因此尚未进行来源软件的画面对照验收。
