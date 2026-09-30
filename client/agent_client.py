"""
The full loop this project is about: a local LLM decides whether a tool is
needed, calls it through a real MCP client session, and synthesizes a final
answer from the result.

Uses Ollama (free, local) instead of a paid API, and asks the model for a
strict JSON decision rather than relying on a specific model's native
function-calling support — more portable across models, and it's the same
"structured output reliability" pattern production LLM features need
regardless of whether tools/MCP are involved at all: validate, and repair-
retry once if the model's output doesn't parse.

Usage:
    python agent_client.py "How many orders has Amina Kader placed?"
    python agent_client.py --role admin "Send a notification to #sales saying hi"
"""

import argparse
import asyncio
import json
import re
import sys

import requests
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

OLLAMA_URL = "http://localhost:11434/api/chat"
OLLAMA_MODEL = "llama3.1"


def ask_ollama(messages: list[dict]) -> str:
    response = requests.post(
        OLLAMA_URL,
        json={"model": OLLAMA_MODEL, "messages": messages, "stream": False},
        timeout=60,
    )
    response.raise_for_status()
    return response.json()["message"]["content"]


def extract_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("No JSON object found in model output.")
    return json.loads(match.group(0))


def decide_tool_call(question: str, tools: list) -> dict:
    tool_descriptions = "\n".join(
        f"- {t.name}: {t.description} (args schema: {json.dumps(t.inputSchema)})" for t in tools
    )
    system_prompt = f"""You are an assistant that decides whether a tool call is needed to
answer the user's question. Available tools:

{tool_descriptions}

Respond with ONLY a JSON object, no other text, in one of these two shapes:
{{"tool": "<tool_name>", "args": {{...}}}}
{{"tool": null, "answer": "<direct answer, if no tool is needed>"}}
"""
    messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": question}]
    raw = ask_ollama(messages)

    try:
        return extract_json(raw)
    except (ValueError, json.JSONDecodeError):
        # One repair attempt: show the model its own broken output and ask
        # it to fix the format, rather than failing the whole turn.
        repair_messages = messages + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": "That wasn't valid JSON in the required format. Respond with ONLY the JSON object, nothing else."},
        ]
        raw_retry = ask_ollama(repair_messages)
        return extract_json(raw_retry)  # let this raise if it fails twice — surfaced to the user


async def run_agent(question: str, role: str):
    server_params = StdioServerParameters(
        command=sys.executable,
        args=["../server/mcp_server.py"],
        env={"MCP_CLIENT_ROLE": role},
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools_response = await session.list_tools()
            tools = tools_response.tools

            print(f"[agent] role={role}  question={question!r}")
            decision = decide_tool_call(question, tools)
            print(f"[agent] decision: {decision}")

            if not decision.get("tool"):
                print(f"\nAnswer: {decision.get('answer', '(no answer provided)')}")
                return

            try:
                result = await session.call_tool(decision["tool"], decision.get("args", {}))
                tool_output = result.content[0].text if result.content else "(empty result)"
            except Exception as e:
                print(f"\n[agent] Tool call failed: {e}")
                print("Answer: I couldn't complete that — the tool call failed or was denied.")
                return

            print(f"[agent] tool_output: {tool_output}")

            synthesis_messages = [
                {"role": "system", "content": "Answer the user's question using the tool result below. Be concise."},
                {"role": "user", "content": f"Question: {question}\nTool result: {tool_output}"},
            ]
            final_answer = ask_ollama(synthesis_messages)
            print(f"\nAnswer: {final_answer}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("question")
    parser.add_argument("--role", default="reader", choices=["reader", "admin"])
    args = parser.parse_args()

    asyncio.run(run_agent(args.question, args.role))
