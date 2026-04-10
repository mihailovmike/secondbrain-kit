"""MCP server for SecondBrain: remember/recall/ask from any AI agent.

Connects to SecondBrain API over HTTP — works globally from any repo.

Usage:
  claude mcp add --global secondbrain -- python /path/to/src/mcp_server.py
"""

import asyncio
import json
import os
import sys

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

API_URL = os.getenv("SECONDBRAIN_API_URL", "http://localhost:8789")
API_KEY = os.getenv("SECONDBRAIN_API_KEY", "")

server = Server("secondbrain")
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=API_URL,
            headers={"X-Api-Key": API_KEY},
            timeout=120.0,
        )
    return _client


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="remember",
            description="Save a fact, decision, or knowledge to SecondBrain. "
                        "Use for important information worth remembering long-term: "
                        "decisions with reasons, project facts, principles, insights.",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The knowledge to remember"
                    },
                    "source": {
                        "type": "string",
                        "description": "Where this came from (e.g. 'conversation', 'project-x')",
                        "default": "mcp"
                    }
                },
                "required": ["text"]
            }
        ),
        Tool(
            name="recall",
            description="Search SecondBrain knowledge graph. Returns relevant context, "
                        "entities, and relationships. Use to find what you know about a topic.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to search for (question or topic)"
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["mix", "local", "global"],
                        "description": "mix (default), local (entity-focused), global (broad)",
                        "default": "mix"
                    }
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="ask",
            description="Ask SecondBrain a question and get an LLM-synthesized answer "
                        "from the knowledge graph. Use for complex questions needing multi-hop reasoning.",
            inputSchema={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The question to answer"
                    }
                },
                "required": ["question"]
            }
        ),
        Tool(
            name="brain_stats",
            description="Get SecondBrain statistics: notes, entities, relations count.",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
    ]


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict):
    client = _get_client()

    if name == "remember":
        text = arguments["text"]
        source = arguments.get("source", "mcp")
        resp = await client.post("/add", json={"text": text, "source": source})
        if resp.status_code == 200:
            data = resp.json()
            return [TextContent(type="text", text=f"Saved to SecondBrain: {data.get('path', 'ok')}")]
        elif resp.status_code == 422:
            return [TextContent(type="text", text=f"Rejected: {resp.json().get('detail', 'quality gate')}")]
        return [TextContent(type="text", text=f"Error: {resp.status_code} {resp.text}")]

    elif name == "recall":
        query = arguments["query"]
        mode = arguments.get("mode", "mix")
        resp = await client.post("/search", json={"query": query, "mode": mode})
        if resp.status_code == 200:
            data = resp.json()
            context = data.get("context", "")
            if isinstance(context, dict):
                context = json.dumps(context, ensure_ascii=False, indent=2)
            return [TextContent(type="text", text=str(context) or "No results found.")]
        return [TextContent(type="text", text=f"Error: {resp.status_code}")]

    elif name == "ask":
        question = arguments["question"]
        resp = await client.post("/ask", json={"question": question})
        if resp.status_code == 200:
            data = resp.json()
            return [TextContent(type="text", text=data.get("answer", "No answer."))]
        return [TextContent(type="text", text=f"Error: {resp.status_code}")]

    elif name == "brain_stats":
        resp = await client.get("/stats")
        if resp.status_code == 200:
            data = resp.json()
            return [TextContent(type="text", text=(
                f"Notes: {data['total_notes']}, "
                f"Entities: {data['entities']}, "
                f"Relations: {data['relations']}, "
                f"Storage: {data['vector_storage']}"
            ))]
        return [TextContent(type="text", text=f"Error: {resp.status_code}")]

    raise ValueError(f"Unknown tool: {name}")


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
