# MiniCode Rebuild

MiniCode Rebuild 是一个从零、分阶段实现的本地终端 AI Coding Agent。当前项目优先建立可运行、可验证的最小闭环，再逐步加入模型适配、工具调用、安全边界、Agent Loop 和会话恢复。

## 当前状态

阶段 0“仓库初始化与工程基线”、阶段 1“核心类型与模型适配层”和阶段 2“工具基础设施”已经完成。

目前已经具备：

- 安装 Python 包；
- 运行 `minicode-rebuild` 命令；
- 查看 CLI 帮助和版本；
- 使用 Provider 无关的消息、模型请求、响应和工具调用类型；
- 使用确定性的 `MockModel` 编排模型层测试；
- 通过 OpenAI-compatible Chat Completions 适配器调用真实服务；
- 注册带 JSON Schema 参数声明的 Python 工具，并导出模型可见声明；
- 在统一边界处理参数校验、未知工具、执行异常和超长结果；
- 执行自动化测试。

真实模型适配器和工具注册表目前是可独立使用的库能力，尚未接入 CLI。具体工作区工具和 Agent Loop 也尚未实现，后续会按 [`docs/REBUILD_LOG.md`](docs/REBUILD_LOG.md) 中的路线图逐阶段加入。

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

本阶段的 schema 校验器有意只实现已文档化的 JSON Schema 子集；工作区路径保护和具体读写工具属于后续阶段。

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
