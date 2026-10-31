# IMG Switch（img-switch）

> 本机 Agent 的中转站生图总开关 —— 为支持的 Agent 按需接入生图，也可随时断开。

IMG Switch 把一个 OpenAI 兼容的**中转站生图技能**按需安装进本机各 AI 编程 Agent（ZCode / Codex / Claude Code / Trae / CodeBuddy / MateClaw / Cursor / 跨工具 `.agents` 标准），并提供一个 ccswitch 风格的图形控制台管理一切。适合没有 GPT 套餐、但想在自己的工作流里稳定出图的用户。

## 下载与安装

当前提供 **v0.1.0-beta.5 测试版**。安装包已包含运行程序，**无需预装 Python**。

| 系统 | 下载 |
| --- | --- |
| Windows x64 | [下载安装程序（.exe）](https://github.com/Chris-debug-0225/img-switch/releases/download/v0.1.0-beta.5/IMG-Switch-0.1.0-Windows-x64-unsigned-setup.exe) |
| macOS Apple Silicon（M 系列） | [下载 Apple Silicon 安装包（.dmg）](https://github.com/Chris-debug-0225/img-switch/releases/download/v0.1.0-beta.5/IMG-Switch-macOS-arm64-unsigned.dmg) |
| macOS Intel | [下载 Intel 安装包（.dmg）](https://github.com/Chris-debug-0225/img-switch/releases/download/v0.1.0-beta.5/IMG-Switch-macOS-x86_64-unsigned.dmg) |

[全部版本与源码](https://github.com/Chris-debug-0225/img-switch/releases) · [SHA-256 校验文件](https://github.com/Chris-debug-0225/img-switch/releases/download/v0.1.0-beta.5/SHA256SUMS.txt)

> **测试版提示：** 安装包尚未正式签名，macOS 版也未公证，系统可能显示安全提示或阻止打开。请只从本项目 Release 下载，并在确认来源和完整性后自行决定是否安装。

### Windows 安装

1. 下载上表中的 Windows 安装程序，双击运行并按提示完成安装。
2. 从开始菜单打开 **IMG Switch**。安装默认只针对当前用户，并会创建开始菜单快捷方式。
3. 按下方「首次使用」配置中转源并接入 Agent。

卸载前，请先在应用的「Agent 接入」页关闭需要断开的开关，再通过 Windows 的应用设置卸载。卸载程序不会自动移除已接入的技能，配置也会保留。

### macOS 安装

1. 在苹果菜单  →「关于本机」查看芯片或处理器：Apple M 系列选择 **Apple Silicon**，Intel 处理器选择 **Intel**。不确定时可参考 [Apple 的说明](https://support.apple.com/zh-cn/116943)。
2. 打开下载的 `.dmg`，将 **IMG Switch** 拖入 **Applications（应用程序）** 文件夹，然后从「应用程序」启动。
3. 若 macOS 阻止打开，在确认文件可信且未被篡改后，可查看 [Apple 的安全打开说明](https://support.apple.com/zh-cn/102445)。
4. 按下方「首次使用」配置中转源并接入 Agent。

卸载前，请先在「Agent 接入」页关闭需要断开的开关，再将「应用程序」中的 **IMG Switch** 移到废纸篓。配置默认保留。

## 首次使用

1. **准备服务：** 先获取支持图片生成、兼容 OpenAI 接口的服务地址、API Key 和可用生图模型。IMG Switch 本身不提供密钥或生图服务。
2. **添加中转源：** 打开「中转源」→「＋ 添加中转源」，填写自定义名称、Base URL（通常以 `/v1` 结尾）、API Key 和至少一个生图模型，然后点击「保存」。可点「⟳ 获取」拉取模型；如果服务不支持获取列表，可手动填写模型。
3. **检查连接：** 可在中转源卡片上点击「探测」，检查服务是否可达、密钥是否可用。
4. **接入 Agent：** 打开「Agent 接入」，开启要使用的 Agent 右侧开关。页面只显示本机检测到的 Agent；也可点击「全部接入」。
5. **开始生图：** 在目标项目中打开已接入的 Agent，提出图片生成请求，例如「生成一张山间日出的插画」。默认图片和对应记录保存在该项目的 `generated/` 目录。

需要停止某个 Agent 的接入时，在「Agent 接入」关闭对应开关；「全部断开」可一次断开全部接入，配置仍保留。API Key 保存在本机配置中，并会在请求时发送给你配置的中转源；不要提交或分享配置文件。

## 功能特性

- **Agent 按需接入**：本机装了哪些 Agent 就显示哪些，每个独立开关；只让部分 Agent 走中转站，其余保持原生
- **两级故障转移**：站优先级（外层）× 站内多模型（内层），一个模型失败先换本站下一个模型，本站用尽自动换下一个中转站；可一键关闭（关闭后只用★首选源）
- **★ 首选源**：显式指定生图第一个尝试的源，与排序无关；▲▼ 调整后备顺序
- **模型实时获取**：配置中转源时点「⟳ 获取」直接拉取该站 `/v1/models` 生图模型，不再手填猜测
- **探测**：每个源一键健康检查（可达性 / 密钥 / 延迟）
- **批量生图 / 模型发现 / 源体检**：引擎 CLI 一并支持（`--models`、`--check`、`batch`）
- **本机配置**：API Key 保存在本机配置文件中；生成记录和日志可能包含敏感信息，分享前须检查

## 从源码运行与构建

### Windows 源码运行

```bash
# 需要 Python 3.10+（含 tkinter）
python app.py gui      # 图形界面
python app.py on       # 命令行：全部接入（遵守排除名单）
python app.py off      # 全部断开（恢复各 Agent 原生能力，配置保留）
python app.py status   # 查看状态
```

双击 `安装.bat` 可创建桌面/开始菜单快捷方式（走本机 pythonw）。

在「Agent 接入」页开启需要接入的 Agent，然后在「中转源」页添加 Base URL、API Key 和生图模型。之后在项目目录中使用已接入的 Agent 生成图片，图片会保存在该项目的 `generated/`。

### 构建 Windows 测试安装包

在 Windows x64 上安装 Python 3.12、PyInstaller 6.16.0 和 Inno Setup 6，并将 `ISCC.exe` 加入 PATH。然后运行：

```bash
python -m pip install pyinstaller==6.16.0
python packaging/windows/build_windows.py --version 0.1.0 --output dist/windows
```

安装包为未签名的测试版本，Windows 可能显示安全提示。卸载前请先在控制台关闭 Agent 接入；卸载程序不会自动删除已接入的技能和用户配置。分发前应检查构建产物及其依赖的元数据和许可证。

### macOS / Linux 源码运行与 macOS 构建

源码方式运行 `python3 app.py gui`，需要支持 tkinter 的 Python。配置落在 `~/.config/failover-imagegen-tool/`。

在目标架构的 macOS 上使用带 tkinter 的 Python 3.12 构建未签名测试 DMG：

```bash
python3 -m pip install pyinstaller==6.22.3 pycdlib==1.22.0
PYTHON=python3 bash packaging/macos/build.sh dist/macos
```

DMG 包含应用，可拖入 Applications；分别在 Apple Silicon 和 Intel 机器上构建对应版本。测试包未使用 Developer ID 正式签名，也未公证，macOS 可能阻止或提示首次打开。安装包内的独立引擎供接入的 Agent 调用，不依赖系统 Python。关闭接入后可删除应用；用户配置默认保留。

仓库中的 `.github/workflows/test-installers.yml` 支持手动触发构建；推送以 `v` 开头的标签时，三平台构建与检查全部通过后会创建测试 Release。构建完成不等于安装、隐私或许可审查完成，发布前仍须逐项验证。

构建时会清理 CPython 运行时文档中的用户目录示例，并保留第三方许可及运行时修改说明。未经处理的旧测试安装包仍会被隐私扫描拦截；发布时须使用新构建并通过各平台验证的产物。

## 制作源码包

源码归档使用白名单，不包含本地配置、生成图片、系统扩展属性和构建产物：

```bash
mkdir -p dist/source
python3 packaging/source/build_source.py --output dist/source/img-switch-source.zip
```

构建工具不会覆盖已有文件；发布前仍需检查源码包中的内容和第三方许可。

## 目录结构

```
img-switch/
├── app.py               # IMG Switch 主程序（GUI 控制台 + on/off/status CLI）
├── app_tests.py         # App 集成测试（11 项，临时 HOME 隔离）
├── 安装.bat / 卸载.bat    # Windows 快捷方式脚本
├── 打包.bat              # 构建未签名 Windows 测试安装包
├── packaging/           # 两平台构建脚本和许可证材料
├── engine/              # 生图技能本体（可独立使用，见 engine/README.md）
│   ├── SKILL.md         # skill 定义（Agent 按此触发）
│   ├── scripts/         # failover_imagegen_tool.py / failovercfg.py / setup.py
│   └── tests/           # 引擎端到端测试（26 项，本地 mock 中转站，不访问外网）
└── dist/                # PyInstaller 产物（自行构建生成，不入库）
```

## 只想要生图技能（不要控制台）

`engine/` 目录即完整的 failover-imagegen-tool 技能，可独立分发：复制到任何 Agent 的 skills 目录（如 `~/.agents/skills/failover-imagegen-tool/`）即可被识别，用法见 `engine/README.md`。

## 配置与密钥安全

- 统一配置：Windows `%APPDATA%\failover-imagegen-tool\config.json`，macOS/Linux `~/.config/failover-imagegen-tool/config.json`
- 排除名单（Agent 按需接入）：同目录 `app-settings.json`
- **config.json 含 API Key，切勿提交或分享**（仓库 .gitignore 已排除常见配置文件）。忽略规则不能替代发布前检查。
- 升级或关闭旧版已接入技能时，技能目录中的旧配置和临时配置会备份到统一配置目录的 `migration-backups/`；备份同样可能含密钥，请勿提交或分享。
- `.meta.json` 可能包含提示词、本机绝对路径、服务地址和错误信息；图片本身也可能包含敏感内容。分享前请逐项检查，无法确认时不要分享。
- 命令行的 `--api-key` 参数可能出现在终端历史或进程列表中；优先使用图形界面填写密钥。
- 配置示例见 `engine/config.example.json`

## 开源发布前检查

- 使用当前清理后的源码，勿直接发布原始压缩包或未经检查的构建产物。打包时排除本机扩展属性、绝对路径、缓存、配置、日志和生成记录。
- 检查实际待发布文件及 Git 历史；首次提交前确认 Git 提交者名称和邮箱符合匿名要求，后续每次提交也要检查。`.gitignore` 不会移除已跟踪文件或已有历史。
- 如需发布可执行文件，检查其嵌入的路径、元数据和第三方许可信息；不能为匿名而删除必须保留的版权和许可证声明。
- 确认项目源码及依赖的许可和发布权限，避免将个人或组织数据加入示例与测试。

## 运行测试

```bash
python engine/tests/run_tests.py   # 引擎 26 项（mock 中转站，不联网）
python engine/tests/test_url_privacy.py  # URL 凭据显示回归测试
python test_packaged_runtime.py    # 独立引擎与技能安装回归测试
python packaging/windows/test_build_windows.py
python packaging/macos/test_stage.py
python packaging/macos/test_make_dmg.py
python packaging/source/test_build_source.py
python app_tests.py                # App 11 项（安装/卸载/配置迁移/GUI）
```

## License

[MIT](LICENSE)
