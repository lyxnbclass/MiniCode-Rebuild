"""Model-visible on-demand skill loader."""

from __future__ import annotations

from collections.abc import Mapping

from minicode_rebuild.core import JsonValue
from minicode_rebuild.skills import SkillCatalog, SkillError
from minicode_rebuild.tooling import ToolContext, ToolDefinition, ToolResult


def _load_skill(
    arguments: Mapping[str, JsonValue], context: ToolContext
) -> ToolResult:
    catalog = context.state.get("skill_catalog")
    if not isinstance(catalog, SkillCatalog):
        catalog = SkillCatalog(context.cwd)
    try:
        skill = catalog.load(str(arguments["name"]))
    except SkillError as exc:
        return ToolResult.error("skill_error", str(exc))
    return ToolResult.success(
        f"SKILL: {skill.name}\nPATH: {skill.path}\n\n{skill.content}"
    )


load_skill_tool = ToolDefinition(
    name="load_skill",
    description="Load one relevant workspace SKILL.md by its discovered name.",
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": 64}
        },
        "required": ["name"],
        "additionalProperties": False,
    },
    handler=_load_skill,
)


__all__ = ["load_skill_tool"]
