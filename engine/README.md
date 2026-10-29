# failover-imagegen-tool

通过 OpenAI 兼容中转站生成/编辑图片的 skill，核心特性：**多中转站 × 多模型两级级联故障切换**——一个模型失败先换本站下一个模型，本站模型用尽自动换下一个中转站，直到成功或全部失败。

纯 Python 标准库实现，无第三方依赖（Python 3.8+）。

## 能力一览

- **单张生图/编辑**：`generate`（`--input-image` 走 `/v1/images/edits` 参考图编辑）
- **批量生图**：`batch --prompts-file`（一行一图，单张失败不停继续，结束输出 成功/失败 汇总）
- **模型发现**：`--models`（查各站 `/v1/models` 并过滤生图模型，配置不再靠猜）
- **源健康体检**：`--check`（可达性/密钥/延迟 + 从 `generated/*.meta.json` 聚合各站历史出图数、平均耗时、切换救回数）
- **级联故障切换**：站优先级（外层）× 站内模型顺序（内层），meta.json 记录每一次尝试
- ★**首选源**：config.json 顶层 `"prefer": "<源名>"` 指定生图第一个尝试的源（与数组顺序无关，禁用时回退首个启用源）
- **故障转移可关**：config.json 顶层 `"failover": false` 时只用★首选源的首个模型，失败直接报错不切换（默认开启；App「中转源」页可切换）

## 目录结构

```
failover-imagegen-tool/
├── SKILL.md               # skill 定义（ZCode/Codex 等按此触发）
├── README.md
├── config.example.json    # 配置示例（含多模型级联写法，占位符非真实密钥）
├── prompts/example.txt    # 提示词文件示例
├── scripts/
│   ├── failover_imagegen_tool.py  # 生图主脚本：级联切换、批量、模型发现、体检
│   ├── setup.py           # 中转源配置管理（CLI + tkinter GUI）
│   └── failovercfg.py     # 共享配置模块
└── tests/
    ├── mock_relay.py      # 本地模拟中转站（用于验证，不访问外网）
    └── run_tests.py       # 23 项端到端验证
```

## 安装为 ZCode skill

把本目录复制（或 junction）到以下任一位置即可被发现：

```
~/.agents/skills/failover-imagegen-tool/     # 用户级（推荐）
<project>/.agents/skills/failover-imagegen-tool/
```

安装后在 ZCode 里说"用 failover-imagegen-tool 生成一张…"即可触发；也可直接命令行调用脚本。

## 配置中转站

每个中转源 4 项信息：名称、Base URL（服务商提供的 OpenAI 兼容根地址，通常以 `/v1` 结尾）、API Key、模型 ID（可多个组成站内级联）。

```bash
python scripts/setup.py add --name example-source --base-url https://relay.example.com/v1 --api-key sk-xxx \
  --model gpt-image-2.5-flare --model dall-e-3      # 多次 --model = 站内级联，按顺序尝试
python scripts/setup.py list                 # 查看源与优先级（密钥脱敏显示）
python scripts/setup.py up --name example-source      # 上移提升优先级；down 下移
python scripts/setup.py disable --name example-source # 临时禁用；enable 启用；remove 删除
python scripts/setup.py gui                  # 图形界面（需 tkinter；模型可下拉选/手输，逗号分隔多个）
```

不知道站上有什么模型？先发现再配置：

```bash
python scripts/failover_imagegen_tool.py --models    # 列出各站生图相关模型
python scripts/failover_imagegen_tool.py --check     # 体检：可达性/密钥/延迟 + 历史统计
```

配置可保存在 skill 目录 `config.json` 或本机统一配置目录，也可用环境变量 `FAILOVER_IMAGEGEN_TOOL_CONFIG` 指定路径；**配置包含 API Key，禁止提交或分享**。优先使用图形界面填写密钥：命令行参数可能留在终端历史或进程列表中。

## 生成图片

```bash
python scripts/failover_imagegen_tool.py generate \
  --prompt-file prompts/example.txt \
  --name test \
  --source example-source \      # 可选：锁定某源；缺省按优先级自动级联切换
  --size 4k \
  --force
```

- 提示词：优先 `--prompt-file`（避免长提示词进入终端历史）；`--prompt` 也可。
- 尺寸别名：`2k`=2560x1440（默认）、`4k`=3840x2160、`2k-portrait`=1440x2560、`4k-portrait`=2160x3840、`square`=2048x2048；也可直接写 `3840x2160`。
- 编辑模式：`--input-image ref.png` 走 `/v1/images/edits`（multipart 上传参考图）。
- 输出：当前项目 `generated/test-YYYYMMDD-HHMMSS-4k.png` + `test-...-4k.meta.json`（含提示词、文件绝对路径、源/模型、服务主机和每次尝试结果；可能含敏感信息）。
- `--verbose` 打印请求细节，日志可能含敏感信息，分享前应检查。

## 批量生图

```bash
python scripts/failover_imagegen_tool.py batch --prompts-file jobs.txt --name blog --size 2k
```

`jobs.txt` 一行一条提示词（`#` 开头为注释，空行跳过）；产物命名 `blog-01-时间戳-2k.png`、`blog-02-…`。单张失败记录原因后继续下一张，结束打印"成功 X / 失败 Y / 共 N"与逐条清单；退出码 0=全部成功、1=有失败。

## 故障切换规则（站×模型两级级联）

| 失败类型 | 行为 |
|---|---|
| HTTP 404 / 5xx、响应无图 | 换本站下一个模型；本站模型用尽换下一站 |
| 网络错误 / 超时 / 连接拒绝、HTTP 408 / 429 | 换下一个中转站 |
| HTTP 401 / 403（该源密钥失效）| 换下一个中转站 |
| 图片下载失败、返回内容不是图片 | 换下一个中转站 |
| HTTP 400 / 422 等其他 4xx | **立即停止**：请求本身不合法（模型/尺寸/提示词），换站换模型大概率同样失败 |

全部尝试失败时退出码 1，并打印每次尝试（站/模型/错误）的汇总。

## 常见问题

- **No config found**：先 `setup.py add` 保存至少一个已启用源，再 `--list-sources` 检查。
- **不知道填什么模型**：跑 `--models` 看该站支持的生图模型，选一个或多个填入。
- **模型或尺寸不支持（400/422）**：查服务商文档确认模型 ID 和允许尺寸，只调对应参数；不要为绕过 4xx 反复重试。
- **429/超时**：多为限流或过载；多模型/多源配置下脚本自动切换。注意：超时若发生在服务端已接受请求之后，切换可能造成重复生成/计费，先检查输出目录与中转站记录。
- **实际尺寸与请求不一致**：以输出图片实际宽高为准（见 `.meta.json` 的 `size_mismatch`）；上游可能不支持请求尺寸，本地放大不等于原生 4K 细节。
- **防泄露**：用 `--prompt-file`，不要把密钥写进提示词；不分享配置、终端历史或完整请求日志。图片、提示词和 meta.json 都可能包含敏感内容，分享前逐项检查。

## 验证

```bash
python tests/run_tests.py
python tests/test_url_privacy.py
```

在本地起模拟中转站（127.0.0.1），26 项端到端验证：站级/模型级故障切换、URL/b64 响应、致命 400 不切换、尺寸、编辑模式、批量失败续跑、模型发现过滤、体检统计、常见密钥格式打码、GUI 冒烟与自测。不访问外网、不需要真实密钥。
