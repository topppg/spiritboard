"""Synthetic Codex hooks and app-server events contain no real session data."""

from spiritboard.input_codex import events, hooks
from spiritboard.input_events import InputSignals


def hook(event: str, **fields: object) -> dict[str, object]:
    return {"hook_event_name": event, "session_id": "synthetic-session", **fields}


def test_question_hooks_correlate_only_blocking_tool_results() -> None:
    fields = {"tool_name": "request_user_input", "tool_use_id": "call-one"}
    pending = events("profile", "PreToolUse", hook("PreToolUse", **fields))
    assert len(pending) == 1
    assert (pending[0].action, pending[0].kind, pending[0].request_id) == (
        "request",
        "question",
        "tool:call-one",
    )
    resolved = events("profile", "PostToolUse", hook("PostToolUse", **fields))
    assert any(row.action == "resolve" and row.request_id == "tool:call-one" for row in resolved)
    fields["tool_name"] = "request_user_input_async"
    assert events("profile", "PreToolUse", hook("PreToolUse", **fields))[0].action == "request"
    assert events("profile", "PostToolUse", hook("PostToolUse", **fields)) == []


def test_approval_hook_fallback_preserves_parallel_same_tool_requests() -> None:
    first = {"tool_name": "Bash", "tool_input": {"command": "synthetic first"}}
    second = {"tool_name": "Bash", "tool_input": {"command": "synthetic second"}}
    requests = [
        events("profile", "PermissionRequest", hook("PermissionRequest", **fields))[0]
        for fields in (first, second)
    ]
    assert all(row.kind == "approval" for row in requests)
    assert requests[0].request_id != requests[1].request_id
    assert "synthetic first" not in requests[0].request_id
    resolved = events("profile", "PostToolUse", hook("PostToolUse", **first))
    assert any(row.request_id == requests[0].request_id for row in resolved)
    assert all(row.request_id != requests[1].request_id for row in resolved)


def test_approval_with_call_id_resolves_matching_tool() -> None:
    fields = {"tool_name": "Bash", "tool_use_id": "call-one", "tool_input": {}}
    request = events("profile", "PermissionRequest", hook("PermissionRequest", **fields))[0]
    resolved = events("profile", "PostToolUse", hook("PostToolUse", **fields))
    assert any(row.request_id == request.request_id and row.action == "resolve" for row in resolved)


def test_lifecycle_does_not_treat_stop_as_async_answer() -> None:
    assert events("profile", "UserPromptSubmit", hook("UserPromptSubmit"))[0].action == "resume"
    assert events("profile", "SessionEnd", hook("SessionEnd"))[0].action == "close"
    assert events("profile", "Stop", hook("Stop")) == []


def test_appserver_requests_and_resolutions_keep_protocol_identity() -> None:
    for method, kind in (
        ("item/tool/requestUserInput", "question"),
        ("item/commandExecution/requestApproval", "approval"),
        ("item/fileChange/requestApproval", "approval"),
        ("item/permissions/requestApproval", "approval"),
        ("mcpServer/elicitation/request", "question"),
    ):
        request = {"id": 42, "method": method, "params": {"threadId": "synthetic-thread"}}
        pending = events("profile", method, request)
        assert len(pending) == 1
        assert (pending[0].action, pending[0].kind, pending[0].request_id) == (
            "request",
            kind,
            "rpc:int:42",
        )
    resolved = events(
        "profile",
        "serverRequest/resolved",
        {"params": {"threadId": "synthetic-thread", "requestId": 42}},
    )
    assert (resolved[0].action, resolved[0].request_id) == ("resolve", "rpc:int:42")


def test_hook_and_appserver_report_one_async_question_and_answer_clears_both() -> None:
    signals = InputSignals()
    try:
        hook_input = hook(
            "PreToolUse", tool_name="request_user_input_async", tool_use_id="call-one"
        )
        asked = {"id": 42, "params": {"threadId": "synthetic-session", "itemId": "call-one"}}
        answered = {"params": {"threadId": "synthetic-session", "requestId": 42}}
        for name, raw in (("PreToolUse", hook_input), ("item/tool/requestUserInput", asked)):
            for event in events("profile", name, raw):
                signals.record(event)
        assert signals.snapshot()["profile:synthetic-session"].pending == 1
        for event in events("profile", "serverRequest/resolved", answered):
            signals.record(event)
        assert signals.snapshot()["profile:synthetic-session"].pending == 0
    finally:
        signals.close()


def test_rpc_numeric_and_string_request_ids_never_collide() -> None:
    signals = InputSignals()
    try:
        for identity in (42, "42", "status:question"):
            for event in events(
                "p",
                "item/tool/requestUserInput",
                {"id": identity, "params": {"threadId": "synthetic-session"}},
            ):
                signals.record(event)
        assert signals.snapshot()["p:synthetic-session"].pending == 3
        for event in events(
            "p",
            "serverRequest/resolved",
            {"params": {"threadId": "synthetic-session", "requestId": 42}},
        ):
            signals.record(event)
        assert signals.snapshot()["p:synthetic-session"].pending == 2
    finally:
        signals.close()


def test_status_flags_have_independent_reserved_identity() -> None:
    pending = events(
        "profile",
        "thread/status/changed",
        {
            "params": {
                "threadId": "synthetic-thread",
                "status": {"type": "active", "activeFlags": ["waitingOnUserInput"]},
            }
        },
    )
    question = next(row for row in pending if row.kind == "question")
    assert question.action == "request"
    active = events(
        "profile",
        "thread/status/changed",
        {
            "params": {
                "threadId": "synthetic-thread",
                "status": {"type": "active", "activeFlags": []},
            }
        },
    )
    assert any(row.action == "resolve" and row.request_id == question.request_id for row in active)
    assert all(row.action == "resolve" and row.request_id for row in active)


def test_invalid_shapes_and_unrelated_tools_do_not_fabricate_input() -> None:
    assert events("profile", "PreToolUse", hook("PreToolUse", tool_name="exec_command")) == []
    assert events("profile", "PreToolUse", {"session_id": False}) == []
    assert events("profile", "serverRequest/resolved", {"params": []}) == []
    assert events("profile", "item/tool/requestUserInput", {"id": True, "params": {}}) == []
    assert events("profile", "PreToolUse", hook("PreToolUse", tool_name={})) == []
    assert (
        events(
            "profile",
            "thread/status/changed",
            {"params": {"threadId": "synthetic-session", "status": {"type": []}}},
        )
        == []
    )


def test_hook_config_emits_bounded_command_hooks_without_policy_decisions() -> None:
    command = "spiritboard input-event --source codex --profile-id synthetic"
    configured = hooks(command)
    assert {"PreToolUse", "PostToolUse", "PermissionRequest", "UserPromptSubmit", "SessionEnd"} <= (
        configured.keys()
    )
    for event, groups in configured.items():
        handlers = groups[0]["hooks"]
        assert isinstance(handlers, list)
        assert handlers[0] == {
            "type": "command",
            "command": f"{command} --event {event}",
            "timeout": 3,
        }
