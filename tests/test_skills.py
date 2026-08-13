from __future__ import annotations

from pathlib import Path

from minicode_rebuild.skills import SkillCatalog
from minicode_rebuild.tooling import ToolContext, ToolRegistry
from minicode_rebuild.tools import MUTATING_TOOLS, READ_ONLY_TOOLS, load_skill_tool


def _write_skill(workspace: Path, name: str, content: str) -> Path:
    target = workspace / ".minicode" / "skills" / name / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text(content, encoding="utf-8")
    return target


def test_discovery_returns_metadata_and_prompt_omits_full_content(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "review",
        "---\nname: review\ndescription: Review Python safely.\n---\n\n# Steps\nSECRET BODY\n",
    )

    catalog = SkillCatalog(tmp_path)

    assert [(item.name, item.description) for item in catalog.discover()] == [
        ("review", "Review Python safely.")
    ]
    prompt = catalog.prompt_summary()
    assert "review: Review Python safely." in prompt
    assert "SECRET BODY" not in prompt


def test_load_skill_tool_loads_only_the_named_skill(tmp_path: Path) -> None:
    _write_skill(tmp_path, "one", "# One\n\nFIRST CONTENT")
    _write_skill(tmp_path, "two", "# Two\n\nSECOND CONTENT")
    catalog = SkillCatalog(tmp_path)
    context = ToolContext(tmp_path, state={"skill_catalog": catalog})

    result = ToolRegistry([load_skill_tool]).execute(
        "load_skill", {"name": "one"}, context
    )

    assert result.ok is True
    assert "FIRST CONTENT" in result.output
    assert "SECOND CONTENT" not in result.output


def test_invalid_mismatched_and_traversal_skills_are_rejected(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "wrong",
        "---\nname: different\ndescription: mismatch\n---\n",
    )
    catalog = SkillCatalog(tmp_path)

    assert catalog.discover() == ()
    result = ToolRegistry([load_skill_tool]).execute(
        "load_skill",
        {"name": "../outside"},
        ToolContext(tmp_path, state={"skill_catalog": catalog}),
    )
    assert result.ok is False
    assert result.error_code == "skill_error"


def test_model_tools_cannot_read_or_mutate_skill_storage(tmp_path: Path) -> None:
    target = _write_skill(tmp_path, "safe", "# Safe\n\nInstructions")
    read_result = ToolRegistry(READ_ONLY_TOOLS).execute(
        "read_file",
        {"path": ".minicode/skills/safe/SKILL.md"},
        ToolContext(tmp_path),
    )
    write_result = ToolRegistry(MUTATING_TOOLS).execute(
        "write_file",
        {"path": ".minicode/skills/safe/SKILL.md", "content": "changed"},
        ToolContext(tmp_path),
    )

    assert read_result.error_code == "reserved_path"
    assert write_result.error_code == "reserved_path"
    assert target.read_text(encoding="utf-8") == "# Safe\n\nInstructions"
