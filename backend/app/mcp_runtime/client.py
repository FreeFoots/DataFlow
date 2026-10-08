from __future__ import annotations

import asyncio
from typing import Any

from mcp import Client
from mcp.server import MCPServer


class LocalMcpClient:
    """通过MCP进程内传输发现并调用工具。"""

    def __init__(self, server: MCPServer) -> None:
        self.server = server

    def list_tools(self) -> list[dict[str, Any]]:
        return asyncio.run(self._list_tools())

    async def _list_tools(self) -> list[dict[str, Any]]:
        async with Client(self.server) as client:
            result = await client.list_tools()
            return [
                {
                    "name": tool.name,
                    "title": tool.title,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                    "output_schema": tool.output_schema,
                    "annotations": (
                        tool.annotations.model_dump(by_alias=True, exclude_none=True)
                        if tool.annotations
                        else {}
                    ),
                }
                for tool in result.tools
            ]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return asyncio.run(self._call_tool(name, arguments))

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        async with Client(self.server) as client:
            result = await client.call_tool(name, arguments)
        # 退出MCP会话后再抛出错误，避免异步任务组包装业务异常。
        if result.is_error:
            messages = [
                str(getattr(block, "text", ""))
                for block in result.content
                if getattr(block, "text", "")
            ]
            raise RuntimeError("; ".join(messages) or f"MCP工具调用失败：{name}")
        if result.structured_content is None:
            raise RuntimeError(f"MCP工具未返回结构化结果：{name}")
        return dict(result.structured_content)
