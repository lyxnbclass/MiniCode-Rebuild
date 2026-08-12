"""Built-in tool definitions."""

from minicode_rebuild.tools.command import run_command_tool

from minicode_rebuild.tools.read_only import (
    READ_ONLY_TOOLS,
    glob_search_tool,
    grep_files_tool,
    list_files_tool,
    read_file_tool,
)
from minicode_rebuild.tools.write import (
    WRITE_TOOLS,
    edit_file_tool,
    patch_file_tool,
    write_file_tool,
)

MUTATING_TOOLS = (*WRITE_TOOLS, run_command_tool)

__all__ = [
    "MUTATING_TOOLS",
    "READ_ONLY_TOOLS",
    "WRITE_TOOLS",
    "edit_file_tool",
    "glob_search_tool",
    "grep_files_tool",
    "list_files_tool",
    "patch_file_tool",
    "read_file_tool",
    "run_command_tool",
    "write_file_tool",
]
