"""Earnings preview agent.

Usage:
    export OPENAI_API_KEY=...
    python -m earnings_preview.agent AAPL
    MOCK=1 python -m earnings_preview.agent MSFT  # tools require local case context

The agent runs a tool-use loop: the model decides which data to pull, reads the
results, and writes a preview note. A final check flags any number in the note
that never appeared in a tool result (a cheap guard against invented figures).
"""
import json
import os
import sys

from .checks.numbers import check_numbers
from .core.config import MODEL
from .core.path import PROMPT_DIR
from .tools import TOOL_FUNCS, TOOL_SCHEMAS

MAX_STEPS = 10
SYSTEM_PROMPT = (
    PROMPT_DIR / "agent.md"
).read_text(encoding="utf-8")


def run_tool(name: str, args: dict) -> dict:
    func = TOOL_FUNCS.get(name)
    if func is None:
        return {"error": f"unknown tool {name}"}
    try:
        return func(**args)
    except TypeError as e:  # bad arguments from the model
        return {"error": f"bad arguments for {name}: {e}"}






def run_agent(ticker: str, client=None, verbose: bool = True) -> dict:
    if client is None:
        from openai import OpenAI

        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    openai_tools = [
        {
            "type": "function",
            "name": schema["name"],
            "description": schema["description"],
            "parameters": schema["input_schema"],
            "strict": False,
        }
        for schema in TOOL_SCHEMAS
    ]
    inputs = [
        {
            "role": "user",
            "content": f"Write an earnings preview for {ticker.upper()} using the available tools. Use the tools to gather data, and then write a note in markdown format. Every number in the note must come from a tool result. If data is missing or a tool errored, say so under 'Data gaps'.",
        }
    ]
    tool_outputs: list[str] = []

    for step in range(MAX_STEPS):
        resp = client.responses.create(
            model=MODEL,
            max_output_tokens=4000,
            instructions=SYSTEM_PROMPT,
            input=inputs,
            tools=openai_tools,
        )
        inputs.extend(resp.output)

        if resp.status != "completed":
            return {
                 "note": resp.output_text or "",
                "unverified_numbers": check_numbers(
                    resp.output_text or "", tool_outputs
                ),
                "steps": step + 1,
                "error": (
                    f"response status: {resp.status}; "
                    f"details: {resp.incomplete_details}"
                ),
            }

        calls = [
            item for item in resp.output
            if item.type == "function_call"
        ]
        if not calls:
            note = resp.output_text or ""
            result = {
                "note": note,
                "unverified_numbers": check_numbers(note, tool_outputs),
                "steps": step + 1,
            }
            if not note.strip():
                result["error"] = "model returned no final note"
            return result

        for call in calls:
            try:
                args = json.loads(call.arguments)
                if not isinstance(args, dict):
                    raise ValueError("tool arguments must be a JSON object")
            except (ValueError, TypeError) as e:
                args = None
                out = {"error": f"invalid arguments for {call.name}: {e}"}
            else:
                out = run_tool(call.name, args)

            out_json = json.dumps(out, default=str)
            tool_outputs.append(out_json)

            if verbose:
                status = "ERROR" if "error" in out else "ok"
                print(
                    f"  [{step + 1}] {call.name}"
                    f"({call.arguments}) -> {status}",
                    file=sys.stderr,
                )

            inputs.append({
                "type": "function_call_output",
                "call_id": call.call_id,
                "output": out_json,
            })
    return {
        "note": "",
        "unverified_numbers": [],
        "steps": MAX_STEPS,
        "error": "hit step limit without a final note",
    }
        

if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python agent.py TICKER")
    result = run_agent(sys.argv[1])
    print(result["note"])
    if result.get("error"):
        print(f"\n[!] {result['error']}", file=sys.stderr)
    if result["unverified_numbers"]:
        print(f"\n[!] Numbers not found in any tool output, please verify: {', '.join(result['unverified_numbers'])}", file=sys.stderr)