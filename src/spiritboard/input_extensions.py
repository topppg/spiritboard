"""Terminal-independent adapters for OpenCode and Pi interaction lifecycle events."""

from __future__ import annotations

import json
import re

from .input_events import InputEvent


def _id(value: object) -> str:
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", value)
        else ""
    )


def events(profile_id: str, event_name: str, payload: dict[str, object]) -> list[InputEvent]:
    nested = payload.get("properties")
    data = nested if isinstance(nested, dict) else payload
    native = _id(data.get("sessionID")) or _id(data.get("session_id"))
    if not native:
        return []
    request = ""
    kind = "approval" if event_name.startswith("permission.") else "question"
    if event_name in {"question.asked", "permission.asked", "ui_prompt_start"}:
        action = "request"
        request = _id(data.get("id")) or _id(data.get("request_id"))
    elif event_name in {
        "question.replied",
        "question.rejected",
        "permission.replied",
        "ui_prompt_end",
    }:
        action = "resolve"
        request = _id(data.get("requestID")) or _id(data.get("request_id"))
    elif event_name == "session.status":
        status = data.get("status")
        if not isinstance(status, dict) or status.get("type") != "idle":
            return []
        action = "idle"
    elif event_name in {"session.idle", "agent_settled"}:
        action = "idle"
    elif event_name in {"agent_start", "session_start"}:
        action = "resume"
    elif event_name in {"session_shutdown", "session.deleted"}:
        action = "close"
    else:
        return []
    if action in {"request", "resolve"} and not request:
        return []
    return [InputEvent(profile_id, native, action, request_id=request, kind=kind)]


def _bridge(command: list[str]) -> str:
    if not command or not all(isinstance(part, str) and part for part in command):
        raise ValueError("An extension requires a local collector command")
    return (
        'import { spawn } from "node:child_process";\n'
        f"const collector = {json.dumps(command)};\n"
        + """
const validId = value => typeof value === 'string' &&
  /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/.test(value);
let delivery = Promise.resolve();
function emit(hook_event_name, payload) {
  const record = JSON.stringify({hook_event_name, ...payload});
  delivery = delivery.then(() => new Promise(resolve => {
    try {
      const child = spawn(collector[0], collector.slice(1), {stdio:['pipe','ignore','ignore']});
      const timeout = setTimeout(() => child.kill(), 10000);
      timeout.unref();
      const done = () => { clearTimeout(timeout); resolve(); };
      child.on('error', done);
      child.on('close', done);
      child.stdin.on('error', () => {});
      child.stdin.end(record);
    } catch { resolve(); }
  }));
  return delivery;
}
"""
    )


def opencode_plugin(command: list[str]) -> str:
    return (
        _bridge(command)
        + """
export const SpiritboardPlugin = async ({client,directory}) => {
  const pending = new Map();
  let hydrated = true;
  const requests = session => {
    if (!pending.has(session)) pending.set(session, new Set());
    return pending.get(session);
  };
  async function receive(type, properties) {
    if (!properties || !validId(properties.sessionID)) return;
    const sessionID = properties.sessionID;
    if (type === 'question.asked' || type === 'permission.asked') {
      if (!validId(properties.id)) return;
      requests(sessionID).add(properties.id);
      await emit(type, {sessionID, id:properties.id});
    } else if (['question.replied','question.rejected','permission.replied'].includes(type)) {
      if (!validId(properties.requestID)) return;
      requests(sessionID).delete(properties.requestID);
      await emit(type, {sessionID, requestID:properties.requestID});
    } else if (type === 'session.status' || type === 'session.idle') {
      if (type === 'session.status' && properties.status?.type !== 'idle') return;
      if (hydrated && !pending.get(sessionID)?.size) {
        await emit('session.status', {sessionID,status:{type:'idle'}});
      }
    } else if (type === 'session.deleted') {
      pending.delete(sessionID);
      await emit(type, {sessionID});
    }
  }
  // Instance HTTP handlers wait for plugin initialization, so query after this factory returns.
  const ready = new Promise(resolve => setTimeout(resolve, 0)).then(async () => {
    for (const kind of ['question','permission']) {
      try {
        const signal = AbortSignal.timeout(3000);
        let result;
        if (typeof client?.[kind]?.list === 'function') {
          result = await client[kind].list(undefined, {signal});
        } else if (typeof client?._client?.get === 'function' && typeof directory === 'string') {
          result = await client._client.get({url:'/' + kind,query:{directory},signal});
        }
        if (!Array.isArray(result?.data)) {
          hydrated = false;
          continue;
        }
        for (const request of result.data) await receive(kind + '.asked', request);
      } catch { hydrated = false; }
    }
  });
  return {
    event: async ({event}) => {
      await ready;
      if (event && typeof event.type === 'string') await receive(event.type, event.properties);
    },
    dispose: async () => {
      await ready;
      for (const [sessionID, ids] of pending) {
        if (ids.size) await emit('session_shutdown', {session_id:sessionID});
      }
      pending.clear();
    }
  };
};
"""
    )


def pi_extension(command: list[str]) -> str:
    return (
        _bridge(command)
        + """
import { randomUUID } from 'node:crypto';
export default function (pi) {
  let session = '';
  let request = '';
  const native = ctx => {
    const id = ctx?.sessionManager?.getSessionId();
    return validId(id) ? id : '';
  };
  pi.on('session_start', async (_event, ctx) => {
    session = native(ctx);
    request = '';
    if (session) await emit('session_start', {session_id:session});
  });
  pi.on('ui_prompt_start', async (_event, ctx) => {
    session = native(ctx);
    if (!session || request) return;
    request = randomUUID();
    await emit('ui_prompt_start', {session_id:session, request_id:request});
  });
  pi.on('ui_prompt_end', async (_event, ctx) => {
    if (!request || session !== native(ctx)) return;
    const id = request;
    request = '';
    await emit('ui_prompt_end', {session_id:session, request_id:id});
  });
  pi.on('agent_start', async (_event, ctx) => {
    const id = native(ctx);
    if (id && !request) await emit('agent_start', {session_id:id});
  });
  pi.on('agent_settled', async (_event, ctx) => {
    const id = native(ctx);
    if (id && !request) await emit('agent_settled', {session_id:id});
  });
  pi.on('session_shutdown', async (_event, ctx) => {
    const id = session || native(ctx);
    session = '';
    request = '';
    if (id) await emit('session_shutdown', {session_id:id});
  });
}
"""
    )
