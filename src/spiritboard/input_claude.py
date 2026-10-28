"""Translate Claude lifecycle hooks into private, interface-independent input signals."""

from __future__ import annotations

import hashlib
import json
import re

from .input_events import InputEvent

INTERACTIVE = {"AskUserQuestion": "question", "ExitPlanMode": "approval"}
LIFECYCLE = {"UserPromptSubmit": "resume", "Stop": "idle", "SessionEnd": "close"}


def _identifier(value: object) -> str:
    return (
        value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value) else ""
    )


def _digest(value: object) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return ""
    return hashlib.sha256(encoded.encode()).hexdigest()


def _permission_id(payload: dict[str, object]) -> str:
    tool = _identifier(payload.get("tool_name"))
    arguments = payload.get("tool_input")
    if not tool or not isinstance(arguments, dict):
        return ""
    if tool == "AskUserQuestion":
        # Hosts append answers to the question input before the tool executes.
        arguments = {
            key: value for key, value in arguments.items() if key not in {"answers", "response"}
        }
    digest = _digest([tool, arguments])
    return "permission:" + digest if digest else ""


def events(profile_id: str, event_name: str, payload: dict[str, object]) -> list[InputEvent]:
    """Return only correlation metadata; never retain question text or tool arguments."""
    native = _identifier(payload.get("session_id"))
    if not native:
        return []
    if "agent_id" in payload:
        agent = _identifier(payload["agent_id"])
        if not agent:
            return []
        native += ":agent:" + agent
    if event_name in LIFECYCLE:
        return [InputEvent(profile_id, native, LIFECYCLE[event_name])]
    if event_name == "PostToolBatch":
        calls = payload.get("tool_calls")
        if not isinstance(calls, list):
            return []
        resolved: list[InputEvent] = []
        for call in calls:
            if not isinstance(call, dict):
                continue
            completion = {
                key: call[key] for key in ("tool_name", "tool_input", "tool_use_id") if key in call
            }
            completion["session_id"] = payload["session_id"]
            if "agent_id" in payload:
                completion["agent_id"] = payload["agent_id"]
            resolved.extend(events(profile_id, "PostToolUse", completion))
        return resolved

    tool = payload.get("tool_name")
    kind = INTERACTIVE.get(tool, "approval") if isinstance(tool, str) else "approval"
    tool_id = _identifier(payload.get("tool_use_id"))
    if event_name == "PreToolUse":
        if not isinstance(tool, str) or tool not in INTERACTIVE or not tool_id:
            return []
        return [InputEvent(profile_id, native, "request", "tool:" + tool_id, kind)]
    if event_name == "PermissionRequest":
        request_id = _permission_id(payload)
        return [InputEvent(profile_id, native, "request", request_id, kind)] if request_id else []
    if event_name in {"PostToolUse", "PostToolUseFailure"}:
        request_ids = ["tool:" + tool_id] if tool_id else []
        permission_id = _permission_id(payload)
        if permission_id:
            request_ids.append(permission_id)
        return [
            InputEvent(profile_id, native, "resolve", request_id, kind)
            for request_id in request_ids
        ]
    if event_name in {"Elicitation", "ElicitationResult"}:
        elicitation_id = _identifier(payload.get("elicitation_id"))
        server = payload.get("mcp_server_name")
        if not elicitation_id or not isinstance(server, str) or not server:
            return []
        request_id = "elicitation:" + _digest([server, elicitation_id])
        action = "request" if event_name == "Elicitation" else "resolve"
        return [InputEvent(profile_id, native, action, request_id)]
    return []


def hooks(command: str) -> dict[str, list[dict[str, object]]]:
    """Build additive settings entries for a caller-supplied, shell-quoted command."""
    names = (
        "PreToolUse",
        "PermissionRequest",
        "PostToolUse",
        "PostToolUseFailure",
        "PostToolBatch",
        "UserPromptSubmit",
        "Stop",
        "SessionEnd",
        "Elicitation",
        "ElicitationResult",
    )
    configured: dict[str, list[dict[str, object]]] = {}
    for name in names:
        entry: dict[str, object] = {
            "hooks": [{"type": "command", "command": command, "timeout": 3}]
        }
        if name == "PreToolUse":
            entry["matcher"] = "AskUserQuestion|ExitPlanMode"
        configured[name] = [entry]
    return configured
