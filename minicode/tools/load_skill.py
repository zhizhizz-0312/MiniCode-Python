from __future__ import annotations

from minicode.skills import load_skill
from minicode.skill_system import stats as skill_stats
from minicode.tooling import ToolDefinition, ToolResult


def _validate(input_data: dict) -> dict:
    name = input_data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name is required")
    return {"name": name.strip()}


def create_load_skill_tool(cwd: str) -> ToolDefinition:
    def _run(input_data: dict, _context) -> ToolResult:
        name = input_data["name"]
        skill = load_skill(cwd, name)
        if skill is None:
            # A failed load is the other half of the success-rate metric: the
            # skill was asked for and could not be delivered.
            skill_stats.record_run(name, success=False)
            return ToolResult(ok=False, output=f"Unknown skill: {name}")
        # This is the moment a matched skill actually gets used, which is what
        # turns "matched N times" into a conversion and success rate.
        skill_stats.record_run(skill.name, success=True)
        return ToolResult(
            ok=True,
            output="\n".join(
                [
                    f"SKILL: {skill.name}",
                    f"SOURCE: {skill.source}",
                    f"PATH: {skill.path}",
                    "",
                    skill.content,
                ]
            ),
        )

    return ToolDefinition(
        name="load_skill",
        description="Load a local SKILL.md by name.",
        input_schema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        validator=_validate,
        run=_run,
    )
