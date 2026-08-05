#!/usr/bin/env python3
"""Local Responses-to-Chat bridge for Codex CLI and ModelHub crawl.

Codex CLI 0.146 only supports `wire_api = "responses"` for custom providers,
while the ModelHub crawl endpoint accepts Chat Completions style
`messages + max_tokens`. This small localhost server lets Codex keep its native
Responses harness and translates each request to the crawl endpoint.
"""

from __future__ import print_function

import argparse
import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _now():
    return int(time.time())


def _expand_env(value):
    for name, env_value in os.environ.items():
        value = value.replace("${%s}" % name, env_value)
    return value


def _text_from_content(content):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for part in content:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict):
                text = part.get("text")
                if text is None:
                    text = part.get("input_text")
                if text is None:
                    text = part.get("output_text")
                if text is not None:
                    chunks.append(str(text))
        return "\n".join(chunk for chunk in chunks if chunk)
    return str(content)


def _message_from_response_item(item):
    role = str(item.get("role") or "user")
    content = _text_from_content(item.get("content"))
    if not content:
        return None
    if role not in ("system", "user", "assistant", "tool"):
        role = "user"
    return {"role": role, "content": content}


def _messages_from_input(request):
    messages = []
    instructions = request.get("instructions")
    if instructions:
        messages.append({"role": "system", "content": str(instructions)})

    raw_input = request.get("input")
    if isinstance(raw_input, str):
        messages.append({"role": "user", "content": raw_input})
        return messages
    if not isinstance(raw_input, list):
        return messages

    for item in raw_input:
        if not isinstance(item, dict):
            messages.append({"role": "user", "content": str(item)})
            continue
        item_type = item.get("type")
        if item_type == "message":
            message = _message_from_response_item(item)
            if message:
                messages.append(message)
        elif item_type == "function_call":
            call_id = str(item.get("call_id") or item.get("id") or "")
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": str(item.get("name") or ""),
                        "arguments": str(item.get("arguments") or ""),
                    },
                }],
            })
        elif item_type == "function_call_output":
            messages.append({
                "role": "tool",
                "tool_call_id": str(item.get("call_id") or ""),
                "content": _text_from_content(item.get("output")),
            })
        elif item_type == "reasoning":
            # Chat Completions has no reasoning role. Emitting the summary as a
            # plain assistant message can land between an assistant tool_calls
            # message and its tool replies, which ModelHub rejects with
            # "must be followed by tool messages responding to each tool_call_id".
            continue
        else:
            content = _text_from_content(item.get("content") or item.get("text"))
            if content:
                messages.append({"role": "user", "content": content})
    return _normalize_tool_sequences(messages)


def _normalize_tool_sequences(messages):
    """Put every assistant tool_calls message immediately before its replies.

    Chat Completions requires each tool_call_id in an assistant message to be
    answered by a tool message directly after it. The Responses input can order
    items differently, so pull the matching replies into place and drop any
    tool_call that never received an output.
    """
    replies = {}
    for message in messages:
        if message.get("role") == "tool":
            call_id = str(message.get("tool_call_id") or "")
            if call_id:
                replies.setdefault(call_id, []).append(message)

    ordered = []
    emitted = set()
    for message in messages:
        if message.get("role") == "tool":
            continue
        tool_calls = message.get("tool_calls")
        if not tool_calls:
            ordered.append(message)
            continue
        answered = [c for c in tool_calls if str(c.get("id") or "") in replies]
        if not answered:
            continue
        ordered.append(dict(message, tool_calls=answered))
        for call in answered:
            for reply in replies.get(str(call.get("id") or ""), []):
                if id(reply) not in emitted:
                    emitted.add(id(reply))
                    ordered.append(reply)
    return ordered


def _chat_tools(response_tools):
    tools = []
    for tool in response_tools or []:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") != "function":
            continue
        name = tool.get("name")
        if not name:
            continue
        function = {
            "name": str(name),
            "description": str(tool.get("description") or ""),
            "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
        }
        tools.append({"type": "function", "function": function})
    return tools


def _chat_payload(response_request, max_tokens):
    payload = {
        "model": response_request.get("model"),
        "messages": _messages_from_input(response_request),
        "stream": False,
        # ModelHub gpt-5.x rejects max_tokens: "Unsupported parameter:
        # 'max_tokens' is not supported with this model. Use
        # 'max_completion_tokens' instead." (HTTP 400, code -4003)
        "max_completion_tokens": max_tokens,
    }
    if response_request.get("temperature") is not None:
        payload["temperature"] = response_request.get("temperature")
    tools = _chat_tools(response_request.get("tools"))
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    return payload


def _response_usage(chat_usage):
    chat_usage = chat_usage or {}
    input_tokens = int(chat_usage.get("prompt_tokens") or chat_usage.get("input_tokens") or 0)
    output_tokens = int(chat_usage.get("completion_tokens") or chat_usage.get("output_tokens") or 0)
    total_tokens = int(chat_usage.get("total_tokens") or input_tokens + output_tokens)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


def _output_items_from_chat(chat_response, model):
    choices = chat_response.get("choices") or []
    if not choices:
        text = json.dumps(chat_response, ensure_ascii=False)
        return [_message_item(text)], model

    message = (choices[0] or {}).get("message") or {}
    model = str(chat_response.get("model") or model or "")
    items = []

    for index, call in enumerate(message.get("tool_calls") or []):
        function = call.get("function") or {}
        name = str(function.get("name") or "")
        arguments = function.get("arguments")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments or {}, ensure_ascii=False)
        call_id = str(call.get("id") or "call_%d" % index)
        items.append({
            "type": "function_call",
            "id": "fc_%s" % call_id,
            "call_id": call_id,
            "name": name,
            "arguments": arguments,
            "status": "completed",
        })

    content = message.get("content")
    if isinstance(content, list):
        text = _text_from_content(content)
    elif content is None:
        text = ""
    else:
        text = str(content)
    if text or not items:
        items.append(_message_item(text))
    return items, model


def _message_item(text):
    return {
        "type": "message",
        "id": "msg_%d" % int(time.time() * 1000),
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }


def _responses_object(response_request, chat_response):
    output, model = _output_items_from_chat(chat_response, response_request.get("model"))
    return {
        "id": str(chat_response.get("id") or "resp_%d" % int(time.time() * 1000)),
        "object": "response",
        "created_at": int(chat_response.get("created") or _now()),
        "status": "completed",
        "model": model,
        "output": output,
        "usage": _response_usage(chat_response.get("usage")),
    }


def _write_sse_event(handler, name, payload):
    handler.wfile.write(("event: %s\n" % name).encode("utf-8"))
    handler.wfile.write(b"data: ")
    handler.wfile.write(_json_bytes(payload))
    handler.wfile.write(b"\n\n")
    handler.wfile.flush()


def _stream_response(handler, response):
    in_progress = dict(response)
    in_progress["status"] = "in_progress"
    in_progress["output"] = []
    _write_sse_event(handler, "response.created", {
        "type": "response.created",
        "response": in_progress,
    })
    for index, item in enumerate(response.get("output") or []):
        started_item = dict(item)
        started_item["status"] = "in_progress"
        if item.get("type") == "message":
            started_item["content"] = []
        elif item.get("type") == "function_call":
            started_item["arguments"] = ""
        _write_sse_event(handler, "response.output_item.added", {
            "type": "response.output_item.added",
            "output_index": index,
            "item": started_item,
        })
        if item.get("type") == "message":
            text = ""
            content = item.get("content") or []
            if content and isinstance(content[0], dict):
                text = str(content[0].get("text") or "")
            _write_sse_event(handler, "response.output_text.delta", {
                "type": "response.output_text.delta",
                "item_id": item.get("id"),
                "output_index": index,
                "content_index": 0,
                "delta": text,
            })
            _write_sse_event(handler, "response.output_text.done", {
                "type": "response.output_text.done",
                "item_id": item.get("id"),
                "output_index": index,
                "content_index": 0,
                "text": text,
            })
        elif item.get("type") == "function_call":
            arguments = str(item.get("arguments") or "")
            _write_sse_event(handler, "response.function_call_arguments.delta", {
                "type": "response.function_call_arguments.delta",
                "item_id": item.get("id"),
                "output_index": index,
                "delta": arguments,
            })
            _write_sse_event(handler, "response.function_call_arguments.done", {
                "type": "response.function_call_arguments.done",
                "item_id": item.get("id"),
                "output_index": index,
                "arguments": arguments,
            })
        _write_sse_event(handler, "response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": index,
            "item": item,
        })
    _write_sse_event(handler, "response.completed", {
        "type": "response.completed",
        "response": response,
    })


def _redacted_error(exc):
    text = str(exc)
    for name, value in os.environ.items():
        if name.endswith("KEY") and value:
            text = text.replace(value, "<redacted>")
    return text


def _call_upstream(server, response_request):
    max_tokens = int(response_request.get("max_output_tokens") or server.max_tokens)
    payload = _chat_payload(response_request, max_tokens)
    target_url = _expand_env(server.target_url)
    headers = {"Content-Type": "application/json"}
    if server.api_key_env and os.environ.get(server.api_key_env) and "ak=" not in target_url:
        headers["Authorization"] = "Bearer %s" % os.environ[server.api_key_env]
    request = urllib.request.Request(
        target_url,
        data=_json_bytes(payload),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=server.timeout_seconds) as response:
        body = response.read()
    return json.loads(body.decode("utf-8"))


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "GTModelHubCrawlBridge/1.0"

    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        if self.path == "/health":
            self._send_json({"ok": True})
            return
        self.send_error(404)

    def do_POST(self):
        if not self.path.endswith("/responses"):
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("content-length") or "0")
            response_request = json.loads(self.rfile.read(length).decode("utf-8"))
            chat_response = _call_upstream(self.server, response_request)
            response = _responses_object(response_request, chat_response)
            if response_request.get("stream") is False:
                self._send_json(response)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            _stream_response(self, response)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            self._send_json({
                "error": {
                    "message": "upstream ModelHub crawl returned HTTP %s: %s" % (
                        exc.code,
                        _redacted_error(body),
                    ),
                    "type": "upstream_error",
                }
            }, status=502)
        except Exception as exc:
            if self.server.log_file:
                with open(self.server.log_file, "a", encoding="utf-8") as handle:
                    handle.write(_redacted_error(traceback.format_exc()) + "\n")
            self._send_json({
                "error": {
                    "message": _redacted_error(exc),
                    "type": "bridge_error",
                }
            }, status=500)

    def _send_json(self, value, status=200):
        raw = _json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--port-file")
    parser.add_argument("--target-url", required=True)
    parser.add_argument("--api-key-env", default="")
    parser.add_argument("--max-tokens", type=int, default=16384)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--log-file", default="")
    args = parser.parse_args(argv)

    server = ThreadingHTTPServer((args.host, args.port), BridgeHandler)
    server.target_url = args.target_url
    server.api_key_env = args.api_key_env
    server.max_tokens = args.max_tokens
    server.timeout_seconds = args.timeout_seconds
    server.log_file = args.log_file

    host, port = server.server_address[:2]
    if args.port_file:
        with open(args.port_file, "w", encoding="utf-8") as handle:
            handle.write("%s\n" % port)
    print("modelhub crawl bridge listening on %s:%s" % (host, port), file=sys.stderr)
    server.serve_forever()


if __name__ == "__main__":
    main()
