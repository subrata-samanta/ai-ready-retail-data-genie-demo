"""A stand-in Databricks workspace for running notebooks/Genie_MCP_Router_Demo.ipynb offline.

It serves just what the notebook calls:
  * two Genie-space MCP servers at /api/2.0/mcp/genie/<space_id>, built with the official `mcp` Python SDK
    (Streamable HTTP; one answers with plain JSON, the other with server-sent events), each with the tools
    Databricks exposes for a space: query_space_<id>(query) and poll_response_<id>(conversation_id, message_id);
  * the Genie spaces REST API (title, description, sample questions) and the current user;
  * the serving-endpoints list and an OpenAI-compatible chat endpoint whose "LLM" routes by keyword overlap
    between the question and each tool's description, and checks the message format a real endpoint would reject.

Every request must carry `Authorization: Bearer <TOKEN>`. It is a test double, not an emulator of Databricks.

    python tests/fake_genie_mcp.py 8766      # then: DATABRICKS_HOST=http://127.0.0.1:8766 DATABRICKS_TOKEN=test-token
"""
from __future__ import annotations

import contextlib
import json
import re
import socket
import sys
import threading
import time
import uuid
from typing import Any

import uvicorn
from mcp.server.mcpserver import MCPServer
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

TOKEN = "test-token"
SALES_ID = "01f0aaaa1111bbbb2222cccc3333dddd"
STOCK_ID = "01f0eeee4444ffff5555aaaa6666bbbb"

SPACES = {
    SALES_ID: {
        "title": "FreshCart Sales Analytics",
        "description": "Net sales, gross margin, basket size and transactions by store, region, product category "
                       "and fiscal week, for POS and online orders.",
        "sample_questions": ["What were net sales by region last week?"],
        "json_response": True,            # this server answers with application/json
        "structured": True,               # tool results carry structuredContent
    },
    STOCK_ID: {
        "title": "FreshCart Inventory",
        "description": "Stock on hand, days of cover, out-of-stock rate and stock value by store and product.",
        "sample_questions": [],           # none: the notebook has the LLM write a question for it
        "json_response": False,           # this server answers with text/event-stream
        "structured": False,              # tool results are JSON text only
        "private_config": True,           # include_serialized_space is refused (like a CAN RUN user)
    },
}

LLM_ENDPOINTS = [
    {"name": "databricks-gte-large-en", "task": "llm/v1/embeddings", "state": {"ready": "READY"}},
    {"name": "databricks-meta-llama-3-3-70b-instruct", "task": "llm/v1/chat", "state": {"ready": "READY"}},
    {"name": "databricks-claude-sonnet-4-5", "task": "llm/v1/chat", "state": {"ready": "READY"}},
]

STOPWORDS = set("""a an and the of for by in on to is are was were what which who how many much did does do with
from this that these those it its at as or be last week year our my me show tell about give list per each all any
there their them than then""".split())


class State:
    def __init__(self):
        self.messages: dict[str, dict] = {}       # message_id -> {"space": id, "query": str, "polls": int}
        self.tool_calls: list[tuple[str, str]] = []   # (space_id, tool name)
        self.llm_requests: list[dict] = []
        self.lock = threading.Lock()


STATE = State()


# ---------------------------------------------------------------- Genie answers

def _sales_answer(question: str) -> tuple[str, str, list[str], list[list]]:
    if "store" in question.lower():
        return ("Store 104 Toronto Queen St had the highest net sales last week.",
                "SELECT store_name, MEASURE(net_sales) AS net_sales FROM sales_metrics GROUP BY ALL "
                "ORDER BY net_sales DESC LIMIT 3",
                ["store_name", "net_sales"],
                [["Toronto Queen St", "61234.50"], ["Seattle Pike", "58010.10"], ["Austin Lamar", "55002.75"]])
    return ("West led net sales last week with $182,450, ahead of Central and East.",
            "SELECT region, MEASURE(net_sales) AS net_sales FROM sales_metrics "
            "WHERE is_last_completed_fiscal_week GROUP BY ALL ORDER BY net_sales DESC",
            ["region", "net_sales"],
            [["West", "182450.00"], ["Central", "151220.40"], ["East", "149873.95"], ["Canada", "98120.00"]])


def _stock_answer(question: str) -> tuple[str, str, list[str], list[list]]:
    return ("Out-of-stock rate is highest in Dairy (4.1%); stock on hand is 412,300 units.",
            "SELECT category, MEASURE(out_of_stock_rate) AS oos_rate, MEASURE(stock_on_hand) AS units "
            "FROM inventory_metrics GROUP BY ALL ORDER BY oos_rate DESC",
            ["category", "oos_rate", "units"],
            [["Dairy", "0.041", "51200"], ["Produce", "0.032", "88310"], ["Bakery", "0.018", "20450"]])


def _completed(space_id: str, msg_id: str, conv_id: str, question: str) -> dict:
    text, sql, cols, rows = (_sales_answer if space_id == SALES_ID else _stock_answer)(question)
    if SPACES[space_id]["structured"]:          # rows as the statement-execution protobuf JSON
        data = [{"values": [{"string_value": v} for v in r]} for r in rows]
    else:                                       # rows as plain arrays
        data = rows
    return {
        "conversationId": conv_id, "messageId": msg_id, "status": "COMPLETED",
        "content": {
            "textAttachments": [text],
            "queryAttachments": [{
                "query": sql, "description": f"Answer to: {question}",
                "statement_response": {
                    "statement_id": uuid.uuid4().hex,
                    "manifest": {"schema": {"columns": [{"name": c} for c in cols]}, "total_row_count": len(rows)},
                    "result": {"data_array": data},
                },
            }],
        },
    }


def _make_mcp(space_id: str) -> MCPServer:
    spec = SPACES[space_id]
    server = MCPServer(name=f"genie-{space_id}")

    def wrap(result: dict):
        return result if spec["structured"] else json.dumps(result)

    def query_space(query: str):
        conv_id, msg_id = uuid.uuid4().hex, uuid.uuid4().hex
        with STATE.lock:
            STATE.messages[msg_id] = {"space": space_id, "query": query, "polls": 0, "conv": conv_id}
            STATE.tool_calls.append((space_id, f"query_space_{space_id}"))
        return wrap({"conversationId": conv_id, "messageId": msg_id, "status": "EXECUTING_QUERY"})

    def poll_response(conversation_id: str, message_id: str):
        with STATE.lock:
            STATE.tool_calls.append((space_id, f"poll_response_{space_id}"))
            msg = STATE.messages.get(message_id)
            if msg is None or msg["conv"] != conversation_id or msg["space"] != space_id:
                raise ValueError(f"Unknown message {message_id} in conversation {conversation_id}")
            msg["polls"] += 1
            polls = msg["polls"]
        if polls < 2:                           # first poll: still running, so the client must poll again
            return wrap({"conversationId": conversation_id, "messageId": message_id, "status": "EXECUTING_QUERY"})
        return wrap(_completed(space_id, message_id, conversation_id, msg["query"]))

    structured = spec["structured"]
    for fn in (query_space, poll_response):    # structured output needs a dict return type, plain text a str
        fn.__annotations__["return"] = dict[str, Any] if structured else str
    server.add_tool(query_space, name=f"query_space_{space_id}", structured_output=structured,
                    description="Query the Genie space with a natural language question. Returns the conversation "
                                "and message IDs and the status; poll with poll_response until COMPLETED.")
    server.add_tool(poll_response, name=f"poll_response_{space_id}", structured_output=structured,
                    description="Poll a Genie message started with query_space until its status is COMPLETED.")
    return server


# ---------------------------------------------------------------- the "LLM"

def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", text.lower()) if len(w) > 2 and w not in STOPWORDS}


def _validate_messages(messages: list[dict]) -> str | None:
    """What a real chat endpoint would reject: a tool message without a matching tool call, bad roles."""
    open_calls: set[str] = set()
    for m in messages:
        role = m.get("role")
        if role not in ("system", "user", "assistant", "tool"):
            return f"invalid role {role!r}"
        if role == "assistant":
            for c in m.get("tool_calls") or []:
                if not isinstance(c.get("function", {}).get("arguments"), str):
                    return "tool_calls[].function.arguments must be a string"
                open_calls.add(c["id"])
            if not m.get("tool_calls") and not m.get("content"):
                return "assistant message needs content or tool_calls"
        if role == "tool":
            if m.get("tool_call_id") not in open_calls:
                return f"tool message for unknown tool_call_id {m.get('tool_call_id')!r}"
            if not isinstance(m.get("content"), str):
                return "tool message content must be a string"
            open_calls.discard(m["tool_call_id"])
    return None


def _completion(message: dict) -> dict:
    return {"id": "chatcmpl-" + uuid.uuid4().hex[:12], "object": "chat.completion", "created": int(time.time()),
            "model": "fake", "choices": [{"index": 0, "message": message,
                                          "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}


def _llm(body: dict) -> dict:
    messages, tools = body["messages"], body.get("tools") or []
    last = messages[-1]
    text = last.get("content") or ""
    if not tools:
        if "Reply with the single word OK" in text:
            return _completion({"role": "assistant", "content": "OK"})
        if "needs data from BOTH" in text:
            return _completion({"role": "assistant",
                                "content": "Which stores had the highest net sales last week, and what is their "
                                           "out-of-stock rate?"})
        if "FreshCart Inventory" in text:
            return _completion({"role": "assistant", "content": "Which product categories have the highest "
                                                                "out-of-stock rate?"})
        return _completion({"role": "assistant", "content": [{"type": "text", "text": "What were net sales?"}]})

    tool_results = [m for m in messages if m.get("role") == "tool"]
    if tool_results:                           # second round: answer from what the tools returned
        firsts = [m["content"].split("Answer: ", 1)[-1].splitlines()[0] for m in tool_results]
        return _completion({"role": "assistant", "content": "Here is what the Genie spaces found: " + " ".join(firsts)})

    question = next(m["content"] for m in reversed(messages) if m["role"] == "user")
    q = _words(question)
    scores = []
    for t in tools:
        f = t["function"]
        scores.append((len(q & _words(f["description"])), f["name"], f["parameters"]))
    hits = [s for s in scores if s[0] > 0]
    if not hits:
        return _completion({"role": "assistant", "content": "That question is outside both Genie spaces, "
                                                            "so I did not query them."})
    chosen = hits if (" and " in question.lower() and len(hits) > 1) else [max(hits)]
    calls = []
    for _, name, params in chosen:
        arg = next(iter(params.get("properties") or {"query": {}}))
        calls.append({"id": "call_" + uuid.uuid4().hex[:8], "type": "function",
                      "function": {"name": name, "arguments": json.dumps({arg: question})}})
    return _completion({"role": "assistant", "content": [{"type": "text", "text": ""}], "tool_calls": calls})


# ---------------------------------------------------------------- REST endpoints

async def me(request: Request):
    return JSONResponse({"id": "1001", "userName": "dana@freshcart.example", "displayName": "Dana Developer",
                         "active": True})


async def get_space(request: Request):
    space_id = request.path_params["space_id"]
    spec = SPACES.get(space_id)
    if spec is None:
        return JSONResponse({"error_code": "RESOURCE_DOES_NOT_EXIST", "message": f"Space {space_id} not found"}, 404)
    out = {"space_id": space_id, "title": spec["title"], "description": spec["description"],
           "warehouse_id": "abc123"}
    if request.query_params.get("include_serialized_space") == "true":
        if spec.get("private_config"):
            return JSONResponse({"error_code": "PERMISSION_DENIED",
                                 "message": "You need CAN EDIT on the space to read its configuration."}, 403)
        out["serialized_space"] = json.dumps({"version": 2, "config": {"sample_questions": [
            {"id": uuid.uuid4().hex, "question": [q]} for q in spec["sample_questions"]]}})
    return JSONResponse(out)


async def list_endpoints(request: Request):
    return JSONResponse({"endpoints": LLM_ENDPOINTS})


async def invocations(request: Request):
    name = request.path_params["name"]
    if name not in {e["name"] for e in LLM_ENDPOINTS if e["task"] == "llm/v1/chat"}:
        return JSONResponse({"error_code": "RESOURCE_DOES_NOT_EXIST", "message": f"Endpoint {name} not found"}, 404)
    body = await request.json()
    with STATE.lock:
        STATE.llm_requests.append(body)
    problem = _validate_messages(body.get("messages") or [])
    if problem:
        return JSONResponse({"error_code": "BAD_REQUEST", "message": problem}, 400)
    return JSONResponse(_llm(body))


async def not_found(request: Request):
    return JSONResponse({"error_code": "ENDPOINT_NOT_FOUND", "message": f"No API found for {request.url.path}"}, 404)


def _app() -> Starlette:
    mcp_apps = []
    for space_id, spec in SPACES.items():
        mcp_apps.append(_make_mcp(space_id).streamable_http_app(
            streamable_http_path=f"/api/2.0/mcp/genie/{space_id}", json_response=spec["json_response"]))

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with contextlib.AsyncExitStack() as stack:
            for child in mcp_apps:
                await stack.enter_async_context(child.router.lifespan_context(child))
            yield

    routes = [Route("/api/2.0/preview/scim/v2/Me", me),
              Route("/api/2.0/genie/spaces/{space_id}", get_space),
              Route("/api/2.0/serving-endpoints", list_endpoints),
              Route("/serving-endpoints/{name}/invocations", invocations, methods=["POST"])]
    for child in mcp_apps:
        routes += child.routes
    routes.append(Route("/{path:path}", not_found, methods=["GET", "POST", "PUT", "PATCH", "DELETE"]))
    return Starlette(routes=routes, lifespan=lifespan)


class _Auth:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            if headers.get(b"authorization") != f"Bearer {TOKEN}".encode():
                await Response('{"error_code":"UNAUTHENTICATED"}', 401, media_type="application/json")(
                    scope, receive, send)
                return
        await self.app(scope, receive, send)


class FakeGenieWorkspace:
    def __init__(self, port: int):
        self.port = port
        self.host = f"http://127.0.0.1:{port}"
        self.state = STATE
        self._server = uvicorn.Server(uvicorn.Config(_Auth(_app()), host="127.0.0.1", port=port,
                                                     log_level="warning", lifespan="on"))
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    @classmethod
    def start(cls) -> "FakeGenieWorkspace":
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        ws = cls(port)
        ws._thread.start()
        deadline = time.time() + 15
        while not ws._server.started:
            if time.time() > deadline:
                raise RuntimeError("stand-in workspace did not start")
            time.sleep(0.05)
        return ws

    def stop(self):
        self._server.should_exit = True
        self._thread.join(timeout=10)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8766
    print(f"DATABRICKS_HOST=http://127.0.0.1:{port} DATABRICKS_TOKEN={TOKEN}")
    print(f"GENIE_SPACE_ID_1={SALES_ID} GENIE_SPACE_ID_2={STOCK_ID}")
    uvicorn.run(_Auth(_app()), host="127.0.0.1", port=port, log_level="warning")
