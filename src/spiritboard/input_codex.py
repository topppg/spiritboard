"""Translate Codex lifecycle hooks and app-server input signals into local events."""

from __future__ import annotations

import hashlib
import json

from .input_events import InputEvent

QUESTIONS = {"request_user_input", "request_user_input_async"}
REQUESTS = {
    "item/tool/requestUserInput": "question",
    "item/commandExecution/requestApproval": "approval",
    "item/fileChange/requestApproval": "approval",
    "item/permissions/requestApproval": "approval",
    "mcpServer/elicitation/request": "question",
}


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def _request_id(value: object) -> str:
    if isinstance(value, str):
        return "rpc:str:" + value
    return "rpc:int:" + str(value) if isinstance(value, int) and not isinstance(value, bool) else ""


def _approval_id(payload: dict[str, object]) -> str:
    tool = _string(payload.get("tool_name"))
    if not tool or "tool_input" not in payload:
        return ""
    data = json.dumps(
        [tool, payload["tool_input"]], sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    digest = hashlib.sha256(data.encode()).hexdigest()
    return f"approval:{_string(payload.get('turn_id'))}:{digest}"


def events(profile_id: str, event_name: str, payload: dict[str, object]) -> list[InputEvent]:
    """Read metadata only; never retain question text, tool inputs, or answers."""
    params = payload.get("params")
    body = params if isinstance(params, dict) else payload
    native = _string(body.get("threadId")) or _string(body.get("session_id"))
    if not native:
        return []
    occurred_at = _string(body.get("timestamp"))

    def signal(
        action: str, request_id: str = "", kind: str = "question", alias_id: str = ""
    ) -> InputEvent:
        return InputEvent(profile_id, native, action, request_id, kind, occurred_at, alias_id)

    if event_name in REQUESTS:
        request_id = _request_id(payload.get("id"))
        item = _string(body.get("itemId"))
        alias = "tool:" + item if item else ""
        return [signal("request", request_id, REQUESTS[event_name], alias)] if request_id else []
    if event_name == "serverRequest/resolved":
        request_id = _request_id(body.get("requestId"))
        return [signal("resolve", request_id)] if request_id else []
    if event_name == "thread/status/changed":
        status = body.get("status")
        if not isinstance(status, dict) or _string(status.get("type")) not in {"active", "idle"}:
            return []
        flags = status.get("activeFlags", [])
        if not isinstance(flags, list):
            return []
        return [
            signal("request" if flag in flags else "resolve", f"status:{kind}", kind)
            for flag, kind in (
                ("waitingOnApproval", "approval"),
                ("waitingOnUserInput", "question"),
            )
        ]
    if event_name in {"thread/closed", "SessionEnd"}:
        return [signal("close")]
    if event_name == "UserPromptSubmit":
        return [signal("resume")]
    tool = _string(body.get("tool_name"))
    raw_call = _string(body.get("tool_use_id"))
    call_id = "tool:" + raw_call if raw_call else ""
    if event_name == "PreToolUse":
        return [signal("request", call_id)] if tool in QUESTIONS and call_id else []
    if event_name == "PermissionRequest":
        request_id = call_id or _approval_id(body)
        return [signal("request", request_id, "approval")] if request_id else []
    if event_name == "PostToolUse":
        if tool == "request_user_input_async":
            return []
        resolved = [signal("resolve", call_id)] if call_id else []
        approval_id = _approval_id(body)
        if approval_id:
            resolved.append(signal("resolve", approval_id, "approval"))
        return resolved
    return []


def hooks(command: str) -> dict[str, list[dict[str, object]]]:
    """Return the event map to place under ``hooks`` in a Codex hooks.json file."""
    result: dict[str, list[dict[str, object]]] = {}
    for event_name in (
        "PreToolUse",
        "PermissionRequest",
        "PostToolUse",
        "UserPromptSubmit",
        "SessionEnd",
    ):
        group: dict[str, object] = {
            "hooks": [
                {"type": "command", "command": f"{command} --event {event_name}", "timeout": 3}
            ]
        }
        if event_name == "PreToolUse":
            group["matcher"] = "^request_user_input(?:_async)?$"
        result[event_name] = [group]
    return result
