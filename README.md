# MiniCode Rebuild

MiniCode Rebuild 是一个从零、分阶段实现的本地终端 AI Coding Agent。当前项目优先建立可运行、可验证的最小闭环，再逐步加入模型适配、工具调用、安全边界、Agent Loop 和会话恢复。

## 当前状态

阶段 0“仓库初始化与工程基线”、阶段 1“核心类型与模型适配层”、阶段 2“工具基础设施”、阶段 3“只读工作区工具”和阶段 4“写入、编辑和命令执行工具”已经完成。

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
- 执行自动化测试。

真实模型适配器、工具注册表、只读工具和受权限保护的变更工具目前是可独立使用的库能力，尚未接入 CLI。Agent Loop 也尚未实现，后续会按 [`docs/REBUILD_LOG.md`](docs/REBUILD_LOG.md) 中的路线图逐阶段加入。

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
| `DEEPSEEK_API_KEY` | 无 | DeepSeek API Key |
| `OPENAI_API_KEY` | 无 | 通用 OpenAI-compatible API Key，优先级高于 `DEEPSEEK_API_KEY` |

项目不会自动加载 `.env`。运行调用代码前，应由终端、进程管理器或其他安全配置机制注入环境变量。缺少密钥时，真实适配器配置会给出明确错误；`MockModel` 不需要任何密钥。

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

## 测试

```bash
python -m pytest -q
```

## 开发原则

- 从零实现，不整体复制参考项目。
- 每次只完成一个可验证阶段。
- 测试、文档、代码和 Git 记录保持同步。
- 不提交 API Key、`.env`、缓存或本地虚拟环境。

完整的阶段设计、参考分析和真实验证结果见 [`docs/REBUILD_LOG.md`](docs/REBUILD_LOG.md)。
