"""Synthetic Claude hooks report input across interfaces without reading their screens."""

import json
import unittest
from dataclasses import asdict

from spiritboard.input_claude import events, hooks
from spiritboard.input_events import InputSignals

PROFILE = "claude-demo"
SESSION = "demo-session"


def payload(tool: str = "AskUserQuestion", tool_id: str = "toolu_demo") -> dict[str, object]:
    return {
        "session_id": SESSION,
        "tool_name": tool,
        "tool_use_id": tool_id,
        "tool_input": {
            "questions": [
                {
                    "question": "Which synthetic source?",
                    "header": "Source",
                    "options": [
                        {"label": "Example", "description": "First invented choice"},
                        {"label": "Sample", "description": "Second invented choice"},
                    ],
                    "multiSelect": False,
                }
            ]
        },
    }


class ClaudeInputTests(unittest.TestCase):
    def test_unpersisted_question_needs_no_terminal_or_transcript(self):
        event = events(PROFILE, "PreToolUse", payload())[0]
        self.assertEqual(
            (event.profile_id, event.native_id, event.action, event.kind),
            (PROFILE, SESSION, "request", "question"),
        )
        self.assertEqual(event.request_id, "tool:toolu_demo")

    def test_same_event_for_every_interface(self):
        expected = asdict(events(PROFILE, "PreToolUse", payload())[0])
        del expected["occurred_at"]
        for interface in ("terminal", "cmux", "vscode", "desktop", "custom"):
            with self.subTest(interface=interface):
                value = {**payload(), "entrypoint": interface}
                event = asdict(events(PROFILE, "PreToolUse", value)[0])
                del event["occurred_at"]
                self.assertEqual(event, expected)

    def test_tool_result_resolves_matching_question(self):
        request = events(PROFILE, "PreToolUse", payload())[0]
        result = events(PROFILE, "PostToolUse", payload())
        self.assertTrue(
            any(x.action == "resolve" and x.request_id == request.request_id for x in result)
        )
        other = events(PROFILE, "PostToolUse", payload("Read", "toolu_unrelated"))
        self.assertFalse(any(x.request_id == request.request_id for x in other))

    def test_tool_failure_resolves_same_question(self):
        request = events(PROFILE, "PreToolUse", payload())[0]
        result = events(PROFILE, "PostToolUseFailure", payload())
        self.assertTrue(
            any(x.action == "resolve" and x.request_id == request.request_id for x in result)
        )

    def test_denied_permission_resolves_when_its_tool_batch_returns(self):
        signals = InputSignals()
        try:
            denied = {**payload("Bash"), "tool_input": {"command": "echo invented"}}
            other = payload("AskUserQuestion", "toolu_other_question")
            for value in events(PROFILE, "PermissionRequest", denied):
                signals.record(value)
            for value in events(PROFILE, "PreToolUse", other):
                signals.record(value)
            batch = {
                "session_id": SESSION,
                "tool_calls": [{**denied, "tool_response": "Permission denied by user"}],
            }
            for value in events(PROFILE, "PostToolBatch", batch):
                signals.record(value)
            self.assertEqual(signals.snapshot()[PROFILE + ":" + SESSION].pending, 1)
            for value in events(
                PROFILE, "PostToolBatch", {"session_id": SESSION, "tool_calls": [other]}
            ):
                signals.record(value)
            self.assertEqual(signals.snapshot()[PROFILE + ":" + SESSION].pending, 0)
        finally:
            signals.close()

    def test_batch_resolution_keeps_outer_session_and_agent_scope(self):
        question = {**payload(), "agent_id": "worker-123"}
        request = events(PROFILE, "PreToolUse", question)[0]
        batch = {
            "session_id": SESSION,
            "agent_id": "worker-123",
            "tool_calls": [{**payload(), "session_id": "unrelated-session", "agent_id": "wrong"}],
        }
        resolved = events(PROFILE, "PostToolBatch", batch)
        self.assertTrue(any(value.request_id == request.request_id for value in resolved))
        self.assertTrue(all(value.native_id == request.native_id for value in resolved))

    def test_batch_ignores_malformed_entries_and_response_content(self):
        value = {
            "session_id": SESSION,
            "tool_calls": [None, "invalid", {**payload(), "tool_response": "PRIVATE OUTPUT"}],
        }
        resolved = events(PROFILE, "PostToolBatch", value)
        self.assertTrue(resolved)
        self.assertNotIn("PRIVATE OUTPUT", json.dumps([asdict(event) for event in resolved]))
        self.assertEqual(events(PROFILE, "PostToolBatch", {"session_id": SESSION}), [])
        self.assertEqual(
            events(PROFILE, "PostToolBatch", {"session_id": SESSION, "tool_calls": {}}), []
        )

    def test_plan_approval_is_a_request(self):
        event = events(PROFILE, "PreToolUse", payload("ExitPlanMode"))[0]
        self.assertEqual((event.action, event.kind), ("request", "approval"))

    def test_ordinary_tool_start_does_not_imply_waiting(self):
        self.assertEqual(events(PROFILE, "PreToolUse", payload("Read")), [])

    def test_permission_without_tool_id_matches_result_by_input(self):
        value = payload("Bash")
        del value["tool_use_id"]
        value["tool_input"] = {"command": "echo synthetic", "description": "Invented command"}
        request = events(PROFILE, "PermissionRequest", value)[0]
        result = events(PROFILE, "PostToolUse", {**value, "tool_use_id": "toolu_permission"})
        self.assertEqual((request.action, request.kind), ("request", "approval"))
        self.assertTrue(
            any(x.request_id == request.request_id and x.action == "resolve" for x in result)
        )
        unrelated = {**value, "tool_input": {"command": "echo different"}}
        self.assertFalse(
            any(
                x.request_id == request.request_id
                for x in events(PROFILE, "PostToolUse", unrelated)
            )
        )

    def test_permission_hash_is_independent_of_input_key_order(self):
        first = {**payload("Bash"), "tool_input": {"command": "echo synthetic", "timeout": 10}}
        second = {**first, "tool_input": {"timeout": 10, "command": "echo synthetic"}}
        self.assertEqual(
            events(PROFILE, "PermissionRequest", first)[0].request_id,
            events(PROFILE, "PermissionRequest", second)[0].request_id,
        )

    def test_question_permission_matches_answered_input(self):
        value = payload()
        request = events(PROFILE, "PermissionRequest", value)[0]
        answered = {
            **value,
            "tool_input": {
                **value["tool_input"],
                "answers": {"Which synthetic source?": "Example"},
                "response": "Synthetic response",
            },
        }
        resolved = events(PROFILE, "PostToolUse", answered)
        self.assertEqual(request.kind, "question")
        self.assertTrue(any(x.request_id == request.request_id for x in resolved))

    def test_subagent_does_not_change_parent_scope(self):
        event = events(PROFILE, "PreToolUse", {**payload(), "agent_id": "worker-123"})[0]
        self.assertEqual(event.native_id, SESSION + ":agent:worker-123")
        parent = events(PROFILE, "PreToolUse", payload())[0]
        self.assertNotEqual(event.native_id, parent.native_id)

    def test_lifecycle_clears_native_session_scope(self):
        for name, action in (
            ("UserPromptSubmit", "resume"),
            ("Stop", "idle"),
            ("SessionEnd", "close"),
        ):
            with self.subTest(name=name):
                result = events(PROFILE, name, {"session_id": SESSION})
                self.assertEqual(len(result), 1)
                self.assertEqual((result[0].action, result[0].request_id), (action, ""))

    def test_elicitation_requires_exact_request_id(self):
        value = {
            "session_id": SESSION,
            "mcp_server_name": "synthetic-server",
            "elicitation_id": "elicitation-demo",
            "message": "Do not retain this synthetic prompt",
        }
        request = events(PROFILE, "Elicitation", value)[0]
        result = events(PROFILE, "ElicitationResult", {**value, "action": "cancel"})[0]
        self.assertEqual((request.action, result.action), ("request", "resolve"))
        self.assertEqual(request.request_id, result.request_id)
        del value["elicitation_id"]
        self.assertEqual(events(PROFILE, "Elicitation", value), [])
        self.assertEqual(events(PROFILE, "ElicitationResult", value), [])

    def test_elicitation_ids_are_scoped_to_server(self):
        value = {"session_id": SESSION, "elicitation_id": "shared-id", "mcp_server_name": "one"}
        one = events(PROFILE, "Elicitation", value)[0]
        two = events(PROFILE, "Elicitation", {**value, "mcp_server_name": "two"})[0]
        self.assertNotEqual(one.request_id, two.request_id)

    def test_events_export_no_prompt_arguments_or_paths(self):
        value = {
            **payload(),
            "cwd": "/tmp/synthetic-private-directory",
            "transcript_path": "/tmp/synthetic-private-transcript.jsonl",
        }
        for event_name in ("PreToolUse", "PermissionRequest", "PostToolUse"):
            encoded = json.dumps([asdict(event) for event in events(PROFILE, event_name, value)])
            self.assertNotIn("synthetic-private", encoded)
            self.assertNotIn("Which synthetic source", encoded)
            self.assertNotIn("First invented choice", encoded)

    def test_invalid_or_unknown_events_are_ignored(self):
        for value in ({}, {"session_id": "../../session"}, {"session_id": 42}):
            self.assertEqual(events(PROFILE, "UserPromptSubmit", value), [])
        self.assertEqual(events(PROFILE, "Notification", payload()), [])
        self.assertEqual(events(PROFILE, "PermissionDenied", payload()), [])
        self.assertEqual(events(PROFILE, "PreToolUse", {**payload(), "agent_id": "../worker"}), [])
        self.assertEqual(events(PROFILE, "PreToolUse", {**payload(), "tool_use_id": "../tool"}), [])
        self.assertEqual(events(PROFILE, "PermissionRequest", {**payload(), "tool_input": []}), [])
        self.assertEqual(events(PROFILE, "PreToolUse", {**payload(), "tool_name": []}), [])

    def test_hook_configuration_installs_only_correlated_lifecycle_events(self):
        command = "'/tmp/synthetic path/spiritboard' input-event claude"
        configured = hooks(command)
        self.assertEqual(configured["PreToolUse"][0]["matcher"], "AskUserQuestion|ExitPlanMode")
        self.assertNotIn("Notification", configured)
        for name in (
            "PermissionRequest",
            "PostToolUse",
            "PostToolUseFailure",
            "PostToolBatch",
            "UserPromptSubmit",
            "Stop",
            "SessionEnd",
            "Elicitation",
            "ElicitationResult",
        ):
            self.assertIn(name, configured)
        for matchers in configured.values():
            for matcher in matchers:
                for hook in matcher["hooks"]:
                    self.assertEqual(hook["type"], "command")
                    self.assertEqual(hook["command"], command)
                    self.assertGreaterEqual(hook["timeout"], 1)
                    self.assertLessEqual(hook["timeout"], 3)
                    self.assertNotIn("async", hook)
