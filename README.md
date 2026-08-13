# MiniCode Rebuild

MiniCode Rebuild 是一个从零、分阶段实现的本地终端 AI Coding Agent。当前项目优先建立可运行、可验证的最小闭环，再逐步加入模型适配、工具调用、安全边界、Agent Loop 和会话恢复。

## 当前状态

阶段 0“仓库初始化与工程基线”至阶段 10“可观测性、质量与发布准备”已经完成。

目前已经具备：

- 安装 Python 包；
- 运行 `minicode-rebuild` 命令；
- 查看 CLI 帮助和版本；
- 使用 Provider 无关的消息、模型请求、响应和工具调用类型；
- 使用确定性的 `MockModel` 编排模型层测试；
- 通过 OpenAI-compatible Chat Completions 适配器调用真实服务；
- 注册带 JSON Schema 参数声明的 Python 工具，并导出模型可见声明；
- 在统一边界处理参数校验、未知工具、执行异常和超长结果；
- 安全解析工作区路径，阻止绝对路径、`..` 和符号链接逃逸；
- 读取文件、列举目录、按 glob 查找路径和按正则搜索 UTF-8 文本；
- 限制单次读取窗口、目录/搜索结果、搜索文件大小和最终工具输出；
- 通过默认拒绝、一次授权和会话精确授权保护文件变更与命令执行；
- 原子创建或覆盖文件、执行精确编辑和事务式多替换补丁；
- 以参数数组和 `shell=False` 在工作区内执行有界前台命令；
- 在有最大步数的 Agent Loop 中调用模型、顺序执行工具并回填结构化结果；
- 明确区分最终响应、空响应、模型异常和步数上限四种停止原因；
- 使用交互式 CLI 连续对话，或通过 Headless 模式执行单次任务；
- 在终端查看工具调用状态、停止结果和基础会话统计；
- 通过友好配置错误、安全权限提示和 Ctrl-C/EOF 处理退出；
- 估算中英文与工具协议的上下文 token，按阈值自动压缩旧轮次；
- 定向裁剪超长工具结果，保留头尾证据和结构化元数据；
- 使用 `/compact` 手动压缩，并在摘要器失败时回退到本地摘要；
- 在工作区内持久化会话、统计和完整 transcript，并支持跨进程恢复；
- 在内置文件工具修改前记录 Checkpoint，先预览、再确认 Rewind；
- 使用修改后哈希阻止 Rewind 覆盖 Agent 之后发生的外部编辑；
- 扫描工作区 `.minicode/skills/<name>/SKILL.md`，仅注入有界元数据，并通过 `load_skill` 按需加载正文；
- 在 Agent、会话与工具边界注册进程内 Hooks，隔离并显式报告 Hook 失败；
- 将脱敏生命周期元数据写入工作区 JSONL 日志，并通过时间线查看运行过程；
- 离线检查 Python、运行配置、Provider 配置、会话存储与 Skills readiness；
- 使用 Ruff、Mypy、分支覆盖率、构建、安装和跨平台 CI 作为发布质量门禁；
- 执行自动化测试。

阶段 0 至阶段 10 的基础路线已经完成。后续高级能力必须从阶段 11 清单中单独选择、设计、测试和提交。

## 可观测性与 Readiness

每次 CLI 会话默认把生命周期元数据追加到工作区 `.minicode-rebuild/events.jsonl`。日志只包含时间、事件名、session ID、工具名、成功状态、错误代码和停止原因；不保存用户提示、工具参数、工具输出或 API Key。该目录已从 Git 和模型通用文件工具中隔离。

查看最近 100 条脱敏事件：

```bash
minicode-rebuild --timeline
minicode-rebuild --timeline 20
```

交互模式可以使用 `/timeline`。离线检查 Provider 与本地运行条件：

```bash
minicode-rebuild --readiness
minicode-rebuild --demo --readiness
```

`--readiness` 不会向 Provider 发送请求或验证余额，只检查本地配置结构。普通模式缺少 API Key 时返回非零状态；`--demo --readiness` 不要求 Key。

## Skills 与 Hooks

项目 Skill 放在工作区的 `.minicode/skills/<name>/SKILL.md`。目录名使用字母、数字、点、下划线或连字符；可选 frontmatter 中的 `name` 必须与目录名一致。运行时只把名称和描述放入系统提示，模型判断相关后才调用只读的 `load_skill` 工具加载单个正文。交互模式可用 `/skills` 查看当前发现结果。

Hooks 是供 Python 嵌入方使用的进程内扩展点。`HookManager` 支持 `agent_start`、`agent_stop`、`session_create`、`session_resume`、`session_save`、`before_tool` 和 `after_tool`。Handler 收到只读数据快照；普通异常不会中断 Agent 或工具，但会写入 Hook report，并由默认终端运行时显示 `[hook:error]`。Hooks 不绕过工作区、权限、Checkpoint 或工具参数校验。

本阶段不加载任意 Hook 配置或外部脚本，也不实现 MCP；这些能力需要独立威胁模型和验收标准。

## 环境要求

- Python 3.11 或更高版本
- pip

## 安装

建议先创建虚拟环境：

```bash
python -m venv .venv
```

Windows：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

macOS 或 Linux：

```bash
./.venv/bin/python -m pip install -e ".[dev]"
```

## 使用

安装后查看帮助：

```bash
minicode-rebuild --help
```

查看版本：

```bash
minicode-rebuild --version
```

也可以通过 Python 模块启动：

```bash
python -m minicode_rebuild --help
```

## 模型配置

阶段 1 默认使用 DeepSeek 的 OpenAI-compatible 接口：

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `MINICODE_MODEL` | `deepseek-v4-pro` | 模型名称 |
| `OPENAI_BASE_URL` | `https://api.deepseek.com` | API 基址或完整 `/chat/completions` 地址 |
| `MINICODE_MODEL_TIMEOUT` | `120` | 请求超时秒数，必须是正整数 |
| `MINICODE_MAX_STEPS` | `12` | 每轮最大模型调用步数，必须是正整数 |
| `MINICODE_SYSTEM_PROMPT` | 内置安全提示 | 覆盖本进程使用的系统提示 |
| `MINICODE_CONTEXT_TOKENS` | `16000` | 单轮输入的启发式上下文预算 |
| `MINICODE_CONTEXT_TRIGGER` | `0.8` | 达到预算比例后自动压缩，范围 `(0, 1]` |
| `MINICODE_KEEP_RECENT_TURNS` | `4` | 压缩时保留的最近完整轮次数 |
| `MINICODE_TOOL_RESULT_TOKENS` | `1500` | 单条历史工具结果的估算 token 上限 |
| `MINICODE_SUMMARY_TOKENS` | `1200` | 结构化摘要的估算 token 上限 |
| `DEEPSEEK_API_KEY` | 无 | DeepSeek API Key |
| `OPENAI_API_KEY` | 无 | 通用 OpenAI-compatible API Key，优先级高于 `DEEPSEEK_API_KEY` |

项目不会自动加载 `.env`。运行调用代码前，应由终端、进程管理器或其他安全配置机制注入环境变量。缺少密钥时，真实适配器配置会给出明确错误；`MockModel` 不需要任何密钥。

## CLI 运行方式

无需密钥先运行完整 MockModel 工具演示：

```powershell
minicode-rebuild --demo "inspect this workspace"
```

使用真实 OpenAI-compatible 模型执行一次 Headless 请求：

```powershell
$env:OPENAI_API_KEY="your-key"
minicode-rebuild "分析当前项目结构"
```

也可以从标准输入读取单次任务：

```powershell
"解释 README" | minicode-rebuild --headless
```

启动基础交互模式：

```powershell
minicode-rebuild --interactive
```

列出当前工作区保存的会话，或恢复最近一次会话：

```powershell
minicode-rebuild --list-sessions
minicode-rebuild --interactive --resume latest
minicode-rebuild --resume <session-id> "继续上次任务"
```

交互模式提供 `/help`、`/session`、`/sessions`、`/transcript`、`/checkpoints`、`/rewind-preview [checkpoint-id]`、`/rewind [checkpoint-id]`、`/stats`、`/compact` 和 `/exit`。`/rewind` 总会先显示预览，只有随后完整输入 `yes` 才修改文件；发现 Agent 写入后又有外部修改时会拒绝覆盖。

会话 JSON 位于工作区 `.minicode-rebuild/sessions/`，已从 Git 与内置文件工具中隔离。Checkpoint 只覆盖 `write_file`、`edit_file` 和 `patch_file` 的 UTF-8 文件变更；`run_command` 的任意副作用不在 Rewind 范围内。写文件和运行命令仍会显示风险与操作详情，并要求选择一次允许、会话允许或拒绝。Headless 模式默认拒绝所有变更；只有明确传入 `--allow-mutations` 才会在本次运行内逐项自动批准，并在标准错误输出警告。

每轮会输出模型步数、工具次数、模型返回的 token 用量和压缩次数。上下文估算是跨 Provider 的保守启发式，不等同于服务端精确 tokenizer；工具结果会优先裁剪，旧轮次按用户输入边界摘要，并始终保留最近完整轮次和主系统提示。会话恢复加载的是受预算约束的工作历史，`/transcript` 则保留完整、未压缩的用户消息、assistant 工具调用和工具结果。

最小的库调用边界如下：

```python
from minicode_rebuild.config import ModelSettings
from minicode_rebuild.core import Message, MessageRole, ModelRequest
from minicode_rebuild.models.openai_compatible import OpenAICompatibleAdapter

settings = ModelSettings.from_env()
model = OpenAICompatibleAdapter(settings)
response = model.complete(
    ModelRequest(
        messages=(Message(role=MessageRole.USER, content="Hello"),),
    )
)
print(response.content)
```

## 工具注册表

阶段 2 提供可执行工具的最小公共边界。处理器只会在参数通过 schema 校验后运行；普通异常会转换为失败结果，工具输出也会统一限制长度。

```python
from pathlib import Path

from minicode_rebuild.tooling import (
    ToolContext,
    ToolDefinition,
    ToolRegistry,
    ToolResult,
)

echo = ToolDefinition(
    name="echo",
    description="Return one text value.",
    input_schema={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    },
    handler=lambda arguments, context: ToolResult.success(arguments["text"]),
)
registry = ToolRegistry([echo])
result = registry.execute("echo", {"text": "hello"}, ToolContext(Path.cwd()))
print(result.output)
```

本阶段的 schema 校验器有意只实现已文档化的 JSON Schema 子集；阶段 4 的写入、编辑和命令执行工具复用同一注册与结果边界。

## 只读工作区工具

阶段 3 提供四个可以直接注册的定义：`read_file`、`list_files`、`glob_search` 和 `grep_files`。所有路径先解析为真实路径，再检查是否仍属于 `ToolContext.cwd`；工作区内绝对路径可用，任何指向工作区外的绝对路径、`..` 或符号链接都会被拒绝。

```python
from pathlib import Path

from minicode_rebuild.tooling import ToolContext, ToolRegistry
from minicode_rebuild.tools import READ_ONLY_TOOLS

registry = ToolRegistry(READ_ONLY_TOOLS)
result = registry.execute(
    "grep_files",
    {"pattern": "ToolRegistry", "include": "**/*.py", "limit": 20},
    ToolContext(Path.cwd()),
)
print(result.output)
```

安全和输出边界：

- `read_file` 默认读取 8,000 个字符，单次最多 16,000 个字符，并返回继续读取所需的 offset；
- `list_files`、`glob_search` 和 `grep_files` 都有结果数量上限；
- `grep_files` 最多扫描 5,000 个文件，跳过超过 1 MiB、非 UTF-8 或不可读的文件，并把单行预览限制为 500 个字符；
- 常见缓存、虚拟环境、构建和版本控制目录不会被递归搜索；
- 每个工具仍受注册表 20,000 字符的最终输出上限保护。

## 写入、编辑和命令执行工具

阶段 4 提供 `write_file`、`edit_file`、`patch_file` 和 `run_command`。它们默认拒绝执行，调用方必须通过 `PermissionManager` 注入明确决策；`allow_once` 只允许当前请求，`allow_session` 只复用完全相同的文件路径或命令签名。

```python
from pathlib import Path

from minicode_rebuild.permissions import PermissionManager
from minicode_rebuild.tooling import ToolContext, ToolRegistry
from minicode_rebuild.tools import MUTATING_TOOLS

permissions = PermissionManager(prompt=lambda request: "allow_once")
registry = ToolRegistry(MUTATING_TOOLS)
result = registry.execute(
    "edit_file",
    {"path": "demo.py", "old": "value = 1", "new": "value = 2"},
    ToolContext(Path.cwd(), permissions=permissions),
)
print(result.output)
```

安全边界：

- 所有文件路径和命令工作目录必须位于 `ToolContext.cwd` 内；
- 文件变更先完整计算新内容并生成有限 diff，授权后通过同目录临时文件和 `os.replace` 原子提交；
- `edit_file` 默认要求唯一精确匹配，`patch_file` 的所有替换必须先在内存中成功；
- `run_command` 只接受单个可执行文件名和独立参数数组，始终使用 `shell=False`；
- 命令默认超时 30 秒、最大 300 秒，最终输出仍限制为 20,000 字符。

## 最小 Agent Loop

阶段 5 提供 `run_agent_turn()`：它把用户消息和可选历史组装成 `ModelRequest`，向模型声明当前注册工具，执行模型返回的工具调用，再以 `tool_call_id` 关联的 JSON 工具消息继续请求模型。

```python
from pathlib import Path

from minicode_rebuild.agent import run_agent_turn
from minicode_rebuild.core import ModelResponse
from minicode_rebuild.models import MockModel
from minicode_rebuild.tooling import ToolContext, ToolRegistry

result = run_agent_turn(
    model=MockModel([ModelResponse(content="Done")]),
    tools=ToolRegistry(),
    context=ToolContext(Path.cwd()),
    user_message="Inspect this project",
    max_steps=12,
)
print(result.stop_reason.value, result.content)
```

循环边界：

- 默认最多请求模型 12 步，必须显式使用正整数才能调整；
- 未知工具、非法参数和工具执行失败都会作为结构化工具结果回填，不会直接击穿循环；
- 普通模型异常转换为 `model_error`，`KeyboardInterrupt` 和 `SystemExit` 保持可传播；
- 空文本且没有工具调用时以 `empty_response` 停止；持续调用工具时最终以 `max_steps` 停止；
- 本阶段只提供同步库 API，CLI 接线、流式输出、重试、上下文压缩和会话持久化属于后续阶段。

## 测试

```bash
python -m pytest -q
```

完整质量门禁：

```bash
python scripts/release_check.py
```

它依次运行 Ruff、Mypy、分支覆盖率测试、`compileall`、使用当前已安装构建依赖的 sdist/wheel 构建和无网络 MockModel 演示。当前覆盖率门槛为 85%。GitHub Actions 会在 Windows 与 Ubuntu、Python 3.11 与 3.13 上执行相同门禁。

只运行可复现演示：

```bash
python scripts/demo.py
```

## 跨平台与发布检查清单

- Windows 使用 `\.venv\Scripts\python.exe`，macOS/Linux 使用 `./.venv/bin/python`；项目业务命令仍通过参数数组和 `shell=False` 执行。
- 两个符号链接安全测试在未授予 Windows 创建符号链接权限时会跳过；CI 的 Ubuntu 任务覆盖该路径。
- 终端输出、Skill、会话和事件日志统一使用 UTF-8；Windows 文件替换与权限位行为已有平台保护。
- 发布前确认 Ruff、Mypy、覆盖率测试、编译、构建、全新环境 wheel 安装和 Mock 演示全部通过。
- 检查 Git diff 中没有 `.env`、API Key、会话、事件日志、缓存、构建产物或无关目录。
- 核对 README、`--help`、版本号、Python 版本范围、PR 测试结果和实际行为一致。
- 真实 Provider 验收需由用户自行提供有效 Key；默认质量门禁不发起计费请求。

## 开发原则

- 从零实现，不整体复制参考项目。
- 每次只完成一个可验证阶段。
- 测试、文档、代码和 Git 记录保持同步。
- 不提交 API Key、`.env`、缓存或本地虚拟环境。

完整的阶段设计、参考分析和真实验证结果见 [`docs/REBUILD_LOG.md`](docs/REBUILD_LOG.md)。
