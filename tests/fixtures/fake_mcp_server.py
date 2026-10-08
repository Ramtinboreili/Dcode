#!/usr/bin/env python3
"""A tiny MCP server over stdio for tests: newline-delimited JSON-RPC, a handful of tools."""

import json
import os
import sys
import time

TOOLS = [
    {"name": "add", "description": "Add two numbers and return the sum.",
     "inputSchema": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}, "required": ["a", "b"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "write_note", "description": "Write a note to the server's notebook (changes state).",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "fail", "description": "Always reports an error.", "inputSchema": {"type": "object"}},
    {"name": "slow", "description": "Sleeps for the given seconds.",
     "inputSchema": {"type": "object", "properties": {"seconds": {"type": "number"}}}},
    {"name": "whoami", "description": "Returns the TOKEN environment variable and the working folder.",
     "inputSchema": {"type": "object"}},
    {"name": "picture", "description": "Returns an image and structured data.", "inputSchema": {"type": "object"}},
]
NOTES = []


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def call(name, args):
    if name == "add":
        return {"content": [{"type": "text", "text": str(args["a"] + args["b"])}]}
    if name == "write_note":
        NOTES.append(args.get("text", ""))
        return {"content": [{"type": "text", "text": f"saved note {len(NOTES)}"}]}
    if name == "fail":
        return {"content": [{"type": "text", "text": "disk is full"}], "isError": True}
    if name == "slow":
        time.sleep(float(args.get("seconds", 5)))
        return {"content": [{"type": "text", "text": "woke up"}]}
    if name == "whoami":
        return {"content": [{"type": "text", "text": f"token={os.environ.get('TOKEN', '')} cwd={os.getcwd()}"}]}
    if name == "picture":
        return {"content": [{"type": "image", "data": "AAAA", "mimeType": "image/png"}], "structuredContent": {"w": 2}}
    return None


def main():
    print("not json: a server that logs to stdout", flush=True)  # must be skipped by the client
    print("fake server starting", file=sys.stderr, flush=True)
    if "--crash" in sys.argv:
        print("cannot open the notebook", file=sys.stderr, flush=True)
        sys.exit(3)
    many = "--many" in sys.argv
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        method, request_id = message.get("method"), message.get("id")
        if request_id is None:
            continue  # a notification
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": 900, "method": "roots/list"})  # a request of our own, mid-handshake
            send({"jsonrpc": "2.0", "id": request_id, "result": {
                "protocolVersion": message["params"]["protocolVersion"], "capabilities": {"tools": {}},
                "serverInfo": {"name": "Fake notebook", "version": "1.0"}}})
        elif method == "tools/list":
            if many and not message.get("params", {}).get("cursor"):
                extra = [{"name": f"tool_{i}", "description": "filler " * 30, "inputSchema": {"type": "object"}} for i in range(40)]
                send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": extra, "nextCursor": "page2"}})
            else:
                send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            result = call(message["params"]["name"], message["params"].get("arguments") or {})
            if result is None:
                send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "Unknown tool"}})
            else:
                send({"jsonrpc": "2.0", "id": request_id, "result": result})
        else:
            send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"no {method}"}})


if __name__ == "__main__":
    main()
