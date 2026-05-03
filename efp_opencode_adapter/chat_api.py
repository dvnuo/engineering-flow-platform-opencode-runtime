from __future__ import annotations
import json,re
from typing import Any
from uuid import uuid4
from aiohttp import web
from .opencode_client import OpenCodeClientError
from .session_store import SessionRecord
from .thinking_events import assistant_delta_event, chat_complete_event, chat_completed_compat_event, chat_failed_event, chat_started_event, llm_thinking_event, safe_preview, utc_now_iso

def _bad_request(error:str): return web.HTTPBadRequest(text=json.dumps({"error":error}),content_type="application/json")
def _metadata_from_payload(payload):
    m=payload.get("metadata")
    if m is None:return {}
    if not isinstance(m,dict): raise _bad_request("metadata_must_be_object")
    return m
def _runtime_profile_from_metadata(m):
    rp=m.get("runtime_profile")
    if rp is None:return {}
    if not isinstance(rp,dict): raise _bad_request("runtime_profile_must_be_object")
    return rp
def _optional_str(v): return v if isinstance(v,str) else None
def _normalize_title(raw: Any) -> str: return re.sub(r"\s+"," ",raw if isinstance(raw,str) else "").strip() or "Chat"
def _extract_session_id(payload):
    if isinstance(payload,dict):
      for k in ("id","session_id","uuid"):
        if payload.get(k): return str(payload[k])
    return ""
def _optional_nonempty_string_from_payload(payload,key,*,generated,error):
    if key not in payload or payload.get(key) is None:return generated
    v=payload.get(key)
    if not isinstance(v,str): raise _bad_request(error)
    return v.strip() or generated

def _portal_session_id_from_payload(payload): return _optional_nonempty_string_from_payload(payload,"session_id",generated=str(uuid4()),error="session_id_must_be_string")
def _request_id_from_payload(payload): return _optional_nonempty_string_from_payload(payload,"request_id",generated=f"chat-{uuid4()}",error="request_id_must_be_string")

def extract_assistant_text(payload: Any) -> str:
    if isinstance(payload,dict):
        msg=payload.get("message")
        if isinstance(msg,dict):
            parts=msg.get("parts")
            if isinstance(parts,list):
                return "\n".join(str(p.get("text") or p.get("content") or "") for p in parts if isinstance(p,dict)).strip()
        for k in ("response","text","content"):
            if isinstance(payload.get(k),str) and payload.get(k): return payload[k]
    return ""

async def handle_chat_payload(request: web.Request, payload: dict[str, Any]) -> dict[str, Any]:
    message=payload.get("message")
    if not isinstance(message,str) or not message.strip(): raise _bad_request("message_required")
    metadata=_metadata_from_payload(payload); runtime_profile=_runtime_profile_from_metadata(metadata)
    portal_session_id=_portal_session_id_from_payload(payload); request_id=_request_id_from_payload(payload)
    title=_normalize_title(_optional_str(metadata.get("title")) or message[:60]); model=_optional_str(metadata.get("model")) or _optional_str(runtime_profile.get("model")); agent=_optional_str(metadata.get("agent")); system=_optional_str(metadata.get("system"))
    store=request.app["session_store"]; bus=request.app["event_bus"]; client=request.app["opencode_client"]; chatlog_store=request.app["chatlog_store"]; usage_tracker=request.app["usage_tracker"]; portal_metadata_client=request.app["portal_metadata_client"]
    partial_recovery=False; runtime_events=[]
    context_state={"objective":message[:300],"summary":"OpenCode request accepted","current_state":"running","next_step":"Waiting for OpenCode assistant response","constraints":[],"decisions":[],"open_loops":[],"budget":{"usage_percent":0}}
    try:
      record=store.get(portal_session_id)
      if record is None:
        created=await client.create_session(title=title); sid=_extract_session_id(created)
        record=SessionRecord(portal_session_id,sid,title,agent,model,utc_now_iso(),utc_now_iso(),"",0); store.upsert(record)
      start=chat_started_event(session_id=portal_session_id,request_id=request_id,opencode_session_id=record.opencode_session_id); runtime_events.append(start); await bus.publish(start)
      chatlog_store.start_entry(portal_session_id,request_id=request_id,message=message,runtime_events=runtime_events,context_state=context_state,llm_debug={"engine":"opencode","opencode_session_id":record.opencode_session_id})
      await portal_metadata_client.publish_session_metadata(session_id=portal_session_id,latest_event_type="chat.started",latest_event_state="running",request_id=request_id,summary="Chat started",runtime_events=runtime_events,metadata={"opencode_session_id":record.opencode_session_id})
      think=llm_thinking_event(session_id=portal_session_id,request_id=request_id,opencode_session_id=record.opencode_session_id); runtime_events.append(think); await bus.publish(think)
      response_payload=await client.send_message(record.opencode_session_id,parts=[{"type":"text","text":message}],model=model,agent=agent,system=system)
      assistant_text=extract_assistant_text(response_payload) or "[no assistant response]"
      for e in [assistant_delta_event(session_id=portal_session_id,request_id=request_id,opencode_session_id=record.opencode_session_id,text=assistant_text),chat_complete_event(session_id=portal_session_id,request_id=request_id,opencode_session_id=record.opencode_session_id,text=assistant_text),chat_completed_compat_event(session_id=portal_session_id,request_id=request_id,opencode_session_id=record.opencode_session_id,text=assistant_text)]: runtime_events.append(e); await bus.publish(e)
      updated=store.update_after_chat(portal_session_id,message,assistant_text,model,agent)
      provider=_optional_str(runtime_profile.get("provider")) or _optional_str(metadata.get("provider")) or (response_payload.get("provider") if isinstance(response_payload,dict) else None) or "unknown"
      usage_record=usage_tracker.record_chat(session_id=portal_session_id,request_id=request_id,model=model,provider=provider,response_payload=response_payload,input_text=message,output_text=assistant_text)
      final_context={**context_state,"summary":assistant_text[:500],"current_state":"completed","next_step":""}
      chatlog_store.finish_entry(portal_session_id,request_id=request_id,status="success",response=assistant_text,runtime_events=runtime_events,events=runtime_events,context_state=final_context,llm_debug={"engine":"opencode","opencode_session_id":updated.opencode_session_id,"usage":usage_record,"response_payload_preview":safe_preview(response_payload,2000)})
      await portal_metadata_client.publish_session_metadata(session_id=portal_session_id,latest_event_type="chat.completed",latest_event_state="success",request_id=request_id,summary=assistant_text[:300],runtime_events=runtime_events,metadata={"engine":"opencode","opencode_session_id":updated.opencode_session_id,"model":model,"provider":provider,"context_state":final_context,"usage":usage_record})
    except OpenCodeClientError as exc:
      ev=chat_failed_event(session_id=portal_session_id,request_id=request_id,error=str(exc)); runtime_events.append(ev); await bus.publish(ev)
      chatlog_store.fail_entry(portal_session_id,request_id=request_id,error=str(exc),runtime_events=runtime_events,context_state=context_state,llm_debug={"engine":"opencode"})
      await portal_metadata_client.publish_session_metadata(session_id=portal_session_id,latest_event_type="chat.failed",latest_event_state="error",request_id=request_id,summary=str(exc),runtime_events=runtime_events,metadata={"engine":"opencode"})
      raise web.HTTPBadGateway(text=json.dumps({"error":"opencode_error","detail":str(exc)}),content_type="application/json")
    out={"session_id":portal_session_id,"request_id":request_id,"response":assistant_text,"events":runtime_events,"runtime_events":runtime_events,"usage":usage_record,"context_state":final_context,"_llm_debug":{"engine":"opencode","opencode_session_id":updated.opencode_session_id,"usage":usage_record,"thinking_events":runtime_events}}
    if partial_recovery or getattr(updated,"partial_recovery",False): out["_llm_debug"]["partial_recovery"]=True
    return out

async def chat_handler(request):
    payload=await request.json()
    if not isinstance(payload,dict): raise _bad_request("invalid_json")
    return web.json_response(await handle_chat_payload(request,payload))

async def chat_stream_handler(request):
    try: payload=await request.json()
    except Exception: payload=None
    resp=web.StreamResponse(status=200,headers={"Content-Type":"text/event-stream; charset=utf-8","Cache-Control":"no-cache","Connection":"keep-alive"}); await resp.prepare(request)
    if not isinstance(payload,dict):
      await resp.write(b'event: error\ndata: {"error":"invalid_json"}\n\n'); await resp.write_eof(); return resp
    payload["session_id"]=_portal_session_id_from_payload(payload); payload["request_id"]=_request_id_from_payload(payload)
    pre=chat_started_event(session_id=payload["session_id"],request_id=payload["request_id"])
    await resp.write(f"event: runtime_event\ndata: {json.dumps(pre,ensure_ascii=False)}\n\n".encode())
    try:
      result=await handle_chat_payload(request,payload)
      for e in result.get("runtime_events",[]):
        if e.get("type")==pre.get("type") and e.get("session_id")==pre.get("session_id") and e.get("request_id")==pre.get("request_id"): continue
        await resp.write(f"event: runtime_event\ndata: {json.dumps(e,ensure_ascii=False)}\n\n".encode())
      await resp.write(f"event: final\ndata: {json.dumps(result,ensure_ascii=False)}\n\n".encode()); await resp.write(b"event: done\ndata: {\"ok\":true}\n\n")
    except web.HTTPException as exc:
      await resp.write(f"event: error\ndata: {json.dumps({'error':'chat_failed','detail':exc.text},ensure_ascii=False)}\n\n".encode())
    await resp.write_eof(); return resp
