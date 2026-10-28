import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from spiritboard.input_extensions import events, opencode_plugin, pi_extension


class ExtensionEventTests(unittest.TestCase):
    def test_opencode_request_and_matching_resolution(self):
        for kind in ("question", "permission"):
            asked = events("profile", f"{kind}.asked", {"sessionID": "ses_one", "id": "r1"})
            self.assertEqual(len(asked), 1)
            self.assertEqual(asked[0].action, "request")
            self.assertEqual(asked[0].kind, "approval" if kind == "permission" else "question")
            self.assertEqual(asked[0].request_id, "r1")
            reply = events(
                "profile", f"{kind}.replied", {"sessionID": "ses_one", "requestID": "r1"}
            )[0]
            self.assertEqual((reply.action, reply.native_id), ("resolve", "ses_one"))
        rejected = events(
            "profile", "question.rejected", {"sessionID": "ses_one", "requestID": "r1"}
        )[0]
        self.assertEqual(rejected.action, "resolve")

    def test_official_opencode_envelope_and_non_waiting_events(self):
        payload = {"properties": {"sessionID": "ses_one", "id": "r1"}}
        self.assertEqual(events("p", "question.asked", payload)[0].request_id, "r1")
        for status in ("busy", "retry"):
            self.assertEqual(
                events("p", "session.status", {"sessionID": "ses_one", "status": {"type": status}}),
                [],
            )
        idle = events("p", "session.status", {"sessionID": "ses_one", "status": {"type": "idle"}})
        self.assertEqual(idle[0].action, "idle")

    def test_pi_prompt_pairs_and_runtime_lifecycle(self):
        payload = {"session_id": "pi_one", "request_id": "dialog_one"}
        self.assertEqual(events("p", "ui_prompt_start", payload)[0].action, "request")
        self.assertEqual(events("p", "ui_prompt_end", payload)[0].action, "resolve")
        self.assertEqual(events("p", "agent_start", payload)[0].action, "resume")
        self.assertEqual(events("p", "session_start", payload)[0].action, "resume")
        self.assertEqual(events("p", "agent_settled", payload)[0].action, "idle")
        self.assertEqual(events("p", "session_shutdown", payload)[0].action, "close")

    def test_missing_and_invalid_metadata_is_not_a_signal(self):
        for payload in (
            {},
            {"sessionID": "ses_one"},
            {"sessionID": "../private", "id": "r1"},
            {"sessionID": ":invalid", "id": "r1"},
        ):
            self.assertEqual(events("p", "question.asked", payload), [])
        self.assertEqual(events("p", "unknown", {"sessionID": "ses_one"}), [])


@unittest.skipUnless(shutil.which("node"), "Node is needed to verify harness extensions")
class GeneratedExtensionTests(unittest.TestCase):
    def execute(self, source, driver):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            output = base / "events.jsonl"
            collector = (
                "let s='';process.stdin.on('data',d=>s+=d);"
                "process.stdin.on('end',()=>require('node:fs').appendFileSync(process.argv[1],s+'\\n'))"
            )
            command = [shutil.which("node"), "-e", collector, str(output)]
            module = base / "extension.mjs"
            module.write_text(source(command))
            script = base / "driver.mjs"
            script.write_text(
                f"import * as extension from {json.dumps(module.as_uri())};\n" + driver
            )
            result = subprocess.run(
                [shutil.which("node"), str(script)], capture_output=True, text=True, timeout=10
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return (
                [json.loads(line) for line in output.read_text().splitlines()]
                if output.exists()
                else []
            )

    def test_opencode_hydration_pending_precedence_and_content_minimization(self):
        emitted = self.execute(
            opencode_plugin,
            """
const hooks = await extension.SpiritboardPlugin({client: {
  question: {list: async () => ({data: [{sessionID:'ses_one',id:'q1',questions:['secret']}]})},
  permission: {list: async () => ({data: []})}
}});
const idle={type:'session.status',properties:{sessionID:'ses_one',status:{type:'idle'}}};
await hooks.event({event:idle});
await hooks.event({event:{type:'question.replied',
  properties:{sessionID:'ses_one',requestID:'q1',answers:['private']}}});
await hooks.event({event:idle});
""",
        )
        self.assertEqual(
            [event["hook_event_name"] for event in emitted],
            ["question.asked", "question.replied", "session.status"],
        )
        self.assertNotIn("secret", json.dumps(emitted))

    def test_pi_restart_resets_an_unrestored_dialog_before_new_requests(self):
        from spiritboard.input_events import InputSignals

        emitted = self.execute(
            pi_extension,
            """
const ctx={sessionManager:{getSessionId:()=> 'pi_one'}};
const before=new Map();
extension.default({on:(name,fn)=>before.set(name,fn)});
await before.get('session_start')({},ctx);
await before.get('ui_prompt_start')({},ctx);
// An abrupt process exit does not deliver session_shutdown.
const after=new Map();
extension.default({on:(name,fn)=>after.set(name,fn)});
await after.get('session_start')({},ctx);
""",
        )
        signals = InputSignals()
        try:
            for event in emitted[:-1]:
                for signal in events("p", event["hook_event_name"], event):
                    signals.record(signal)
            self.assertEqual(signals.snapshot()["p:pi_one"].pending, 1)
            for signal in events("p", emitted[-1]["hook_event_name"], emitted[-1]):
                signals.record(signal)
            self.assertEqual(signals.snapshot()["p:pi_one"].pending, 0)
        finally:
            signals.close()
        self.assertNotIn("private", json.dumps(emitted))

    def test_pi_prompt_identity_and_session_replacement(self):
        emitted = self.execute(
            pi_extension,
            """
const handlers = new Map();
extension.default({on:(name,fn)=>handlers.set(name,fn)});
let sid='pi_one';
const ctx={sessionManager:{getSessionId:()=>sid}};
await handlers.get('session_start')({},ctx);
await handlers.get('ui_prompt_start')({title:'private choice'},ctx);
await handlers.get('ui_prompt_end')({},ctx);
await handlers.get('session_shutdown')({},ctx);
sid='pi_two';
await handlers.get('session_start')({},ctx);
await handlers.get('ui_prompt_start')({title:'other secret'},ctx);
await handlers.get('session_shutdown')({},ctx);
""",
        )
        asked = [e for e in emitted if e["hook_event_name"] == "ui_prompt_start"]
        resolved = next(e for e in emitted if e["hook_event_name"] == "ui_prompt_end")
        self.assertEqual(asked[0]["request_id"], resolved["request_id"])
        self.assertNotEqual(asked[0]["request_id"], asked[1]["request_id"])
        self.assertEqual(asked[1]["session_id"], "pi_two")
        self.assertEqual(emitted[-1]["hook_event_name"], "session_shutdown")
        self.assertNotIn("private", json.dumps(emitted))
        self.assertNotIn("secret", json.dumps(emitted))

    def test_unsupported_opencode_hydration_does_not_fabricate_idle(self):
        emitted = self.execute(
            opencode_plugin,
            """
const hooks=await extension.SpiritboardPlugin({client:{}});
await hooks.event({event:{type:'session.status',
  properties:{sessionID:'ses_one',status:{type:'idle'}}}});
await hooks.event({event:{type:'permission.asked',
  properties:{sessionID:'ses_one',id:'p1',metadata:{secret:'private'}}}});
""",
        )
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0]["hook_event_name"], "permission.asked")

    def test_opencode_dispose_closes_only_its_tracked_pending_sessions(self):
        emitted = self.execute(
            opencode_plugin,
            """
const hooks=await extension.SpiritboardPlugin({client:{}});
await hooks.event({event:{type:'question.asked',properties:{sessionID:'ses_one',id:'q1'}}});
await hooks.event({event:{type:'question.asked',properties:{sessionID:'ses_two',id:'q2'}}});
await hooks.event({event:{type:'question.replied',
  properties:{sessionID:'ses_two',requestID:'q2'}}});
await hooks.dispose();
""",
        )
        closed = [event for event in emitted if event["hook_event_name"] == "session_shutdown"]
        self.assertEqual(closed, [{"hook_event_name": "session_shutdown", "session_id": "ses_one"}])
        self.assertEqual(events("p", "session_shutdown", closed[0])[0].action, "close")

    def test_opencode_existing_client_transport_hydrates_current_directory(self):
        emitted = self.execute(
            opencode_plugin,
            """
const calls=[];
let factoryReturned=false;
const client={_client:{get:async options=>{
  calls.push(options);
  if(!factoryReturned) throw new Error('instance bootstrap is still waiting for the plugin');
  if(!(options.signal instanceof AbortSignal)) throw new Error('missing hydration timeout');
  if(options.query?.directory!=='/invented/project') throw new Error('wrong directory');
  if(options.url==='/question') {
    return {data:[{sessionID:'ses_one',id:'q1',questions:['private']}]};
  }
  if(options.url==='/permission') return {data:[]};
  throw new Error('unexpected endpoint');
}}};
const hooks=await extension.SpiritboardPlugin({client,directory:'/invented/project'});
factoryReturned=true;
await hooks.event({event:{type:'synthetic.ready',properties:{sessionID:'ses_one'}}});
if(calls.length!==2) throw new Error('did not reuse the existing client transport');
""",
        )
        self.assertEqual(
            emitted,
            [{"hook_event_name": "question.asked", "sessionID": "ses_one", "id": "q1"}],
        )

    def test_partial_hydration_does_not_clear_unseen_waits(self):
        emitted = self.execute(
            opencode_plugin,
            """
const hooks=await extension.SpiritboardPlugin({client:{_client:{get:async options=>{
  if(options.url==='/question') return {data:[]};
  return {error:{message:'private error'}};
}}},directory:'/invented/project'});
await hooks.event({event:{type:'session.status',
  properties:{sessionID:'ses_one',status:{type:'idle'}}}});
""",
        )
        self.assertEqual(emitted, [])

    def test_opencode_resolution_during_hydration_is_replayed_after_snapshot(self):
        emitted = self.execute(
            opencode_plugin,
            """
let release;
const gate=new Promise(resolve=>release=resolve);
const hooks=await extension.SpiritboardPlugin({client:{
  question:{list:async()=>{
    await gate;
    return {data:[{sessionID:'ses_one',id:'q1'}]};
  }},
  permission:{list:async()=>({data:[]})}
}});
const resolved=hooks.event({event:{type:'question.replied',
  properties:{sessionID:'ses_one',requestID:'q1'}}});
release();
await resolved;
""",
        )
        self.assertEqual(
            [event["hook_event_name"] for event in emitted],
            ["question.asked", "question.replied"],
        )
