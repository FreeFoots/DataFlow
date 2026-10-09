"""Validate human responses at both acceptance and workflow resume boundaries."""
from __future__ import annotations


def clarification_answer(question: dict, option_id: str = "", answer: str | None = None) -> dict[str, str]:
    option_id = option_id.strip()
    text = (answer or "").strip()
    if bool(option_id) == bool(text):
        raise ValueError("请选择一个方向，或用自己的话补充需求")
    if text:
        if not question.get("allow_free_text"):
            raise ValueError("当前补充信息需要选择已有选项")
        if len(text) > 500:
            raise ValueError("补充内容请控制在500字以内")
        return {"question": question.get("question", ""), "answer": text}
    option = next((item for item in question.get("options", []) if item["id"] == option_id), None)
    if option is None:
        raise ValueError("无效的澄清选项")
    return {"question": question.get("question", ""), "answer": option.get("label", option_id),
            "direction": option.get("description", ""), "option_id": option_id}
