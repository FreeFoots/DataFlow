from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SkillDefinition:
    name: str
    description: str
    instructions: str
    allowed_tools: tuple[str, ...]
    max_tool_calls: int
    output_actions: tuple[str, ...]

    def public(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("instructions", None)
        payload["allowed_tools"] = list(self.allowed_tools)
        payload["output_actions"] = list(self.output_actions)
        return payload


class SkillRegistry:
    """从项目目录加载并校验应用级Skill。"""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path(__file__).resolve().parent
        self._skills = self._load()

    def get(self, name: str) -> SkillDefinition:
        if name not in self._skills:
            raise KeyError(f"Skill不存在：{name}")
        return self._skills[name]

    def list(self) -> list[dict[str, Any]]:
        return [self._skills[name].public() for name in sorted(self._skills)]

    def _load(self) -> dict[str, SkillDefinition]:
        output: dict[str, SkillDefinition] = {}
        for config_path in sorted(self.root.glob("*/config.json")):
            directory = config_path.parent
            instruction_path = directory / "SKILL.md"
            if not instruction_path.exists():
                raise ValueError(f"Skill缺少SKILL.md：{directory.name}")
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            name = str(payload.get("name") or directory.name).strip()
            if not name or name in output:
                raise ValueError(f"Skill名称无效或重复：{name}")
            output[name] = SkillDefinition(
                name=name,
                description=str(payload.get("description") or "").strip(),
                instructions=instruction_path.read_text(encoding="utf-8").strip(),
                allowed_tools=tuple(str(item) for item in payload.get("allowed_tools") or []),
                max_tool_calls=max(0, int(payload.get("max_tool_calls", 0))),
                output_actions=tuple(str(item) for item in payload.get("output_actions") or []),
            )
        return output
