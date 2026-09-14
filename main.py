"""FastAPI backend for the composite-key job-scoped SMS demo."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("app")

import llm
import twilio_helpers as tw

STORE_ID = os.environ["MEMORY_STORE_ID"]
CONFIG_ID = os.environ["ORCHESTRATOR_CONFIG_ID"]
V1_SERVICE_SID = os.environ["CONVERSATIONS_V1_SERVICE_SID"]
AI_NUMBER = os.environ["TWILIO_AI_NUMBER"]
TECH_PHONE = os.environ["TECHNICIAN_PHONE"]
TECH_NAME = os.environ.get("TECHNICIAN_NAME", "Field Tech")
ADMIN_PERSONA = os.environ.get("ADMIN_PERSONA", "Dispatcher").strip() or "Dispatcher"

AI_AGENT_PROFILE_ID = os.environ.get("AI_AGENT_PROFILE_ID", "").strip()

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Composite-Key Job SMS Demo")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------- helpers ----------------

async def _ai_agent_profile_id() -> str:
    """Return the AI agent profile id. Set AI_AGENT_PROFILE_ID env, or first-run bootstrap."""
    global AI_AGENT_PROFILE_ID
    if AI_AGENT_PROFILE_ID:
        return AI_AGENT_PROFILE_ID
    # Look up by phone (assumes only one profile at the AI number).
    candidates = await tw.lookup_profiles_by_phone(STORE_ID, AI_NUMBER)
    if candidates:
        AI_AGENT_PROFILE_ID = candidates[0]
        return AI_AGENT_PROFILE_ID
    raise HTTPException(
        500,
        "AI agent profile not found. Run `python bootstrap.py` and set AI_AGENT_PROFILE_ID in .env.",
    )


ADMIN_PREFIX = f"[{ADMIN_PERSONA}]"


def _build_role_map(conv: dict) -> dict[str, str]:
    """participant.id -> 'tech' | 'ai', based on participant type."""
    m: dict[str, str] = {}
    for p in conv.get("participants") or []:
        pid = p.get("id")
        if not pid:
            continue
        t = p.get("type")
        if t == "CUSTOMER":
            m[pid] = "tech"
        elif t == "AI_AGENT":
            m[pid] = "ai"
    return m


def _classify_author(comm: dict, role_map: dict[str, str]) -> str:
    """Body prefix wins over participant type: dispatcher messages are relayed
    through the AI participant but flagged in the body with the persona prefix."""
    text = ((comm.get("content") or {}).get("text")) or ""
    if text.lstrip().startswith(ADMIN_PREFIX):
        return "admin"
    pid = ((comm.get("author") or {}).get("participantId")) or ""
    return role_map.get(pid, "unknown")


def _strip_admin_prefix(text: str) -> str:
    stripped = text.lstrip()
    if stripped.startswith(ADMIN_PREFIX):
        return stripped[len(ADMIN_PREFIX):].lstrip()
    return text


def _traits(profile: dict) -> dict:
    return (profile.get("traits") or {})


def _job_traits(profile: dict) -> dict:
    return _traits(profile).get("Job") or {}


def _profile_job_id(profile: dict) -> str | None:
    contact = _traits(profile).get("Contact") or {}
    return contact.get("jobID")


async def _profile_summary(profile: dict) -> dict:
    """Serialize a profile for the UI. `status` = Memory Job status (business logic);
    `conversationStatus` = live Twilio Conversation state (ACTIVE/INACTIVE/CLOSED) — what the UI shows.
    """
    job = _job_traits(profile)
    contact = _traits(profile).get("Contact") or {}
    conv_id = job.get("conversationId")
    conversation_status: str | None = None
    if conv_id:
        try:
            conv = await tw.get_conversation(conv_id)
            conversation_status = conv.get("status")
        except tw.TwilioError as e:
            log.warning("get_conversation failed for %s: %s", conv_id, e)
    return {
        "id": profile.get("id"),
        "jobID": contact.get("jobID"),
        "phone": contact.get("phone"),
        "status": job.get("status"),
        "conversationStatus": conversation_status,
        "startedAt": job.get("startedAt"),
        "lastActiveAt": job.get("lastActiveAt"),
        "completedAt": job.get("completedAt"),
        "conversationId": conv_id,
        "channelId": job.get("channelId"),
    }


async def _conversation_view(conversation_id: str) -> dict:
    conv = await tw.get_conversation(conversation_id)
    role_map = _build_role_map(conv)
    comms = await tw.list_communications(conversation_id)
    # Communications may come back oldest-first or newest-first; sort by occurredAt asc.
    def _ts(c: dict) -> str:
        return c.get("occurredAt") or c.get("createdAt") or ""
    comms_sorted = sorted(comms, key=_ts)
    messages = []
    for c in comms_sorted:
        content = c.get("content") or {}
        messages.append(
            {
                "id": c.get("id"),
                "author": _classify_author(c, role_map),
                "authorAddress": ((c.get("author") or {}).get("address")),
                "text": content.get("text"),
                "occurredAt": c.get("occurredAt") or c.get("createdAt"),
            }
        )
    return {
        "id": conv.get("id"),
        "status": conv.get("status"),
        "participants": conv.get("participants"),
        "messages": messages,
    }


async def _find_tech_participant(conversation: dict) -> dict:
    p = tw.find_participant(conversation, address=TECH_PHONE, ptype="CUSTOMER")
    if not p:
        raise HTTPException(500, "Tech participant not found in conversation.")
    return p


async def _find_ai_participant(conversation: dict) -> dict:
    p = tw.find_participant(conversation, address=AI_NUMBER, ptype="AI_AGENT")
    if not p:
        raise HTTPException(500, "AI participant not found in conversation.")
    return p


# Admin is not a distinct participant — dispatcher messages are relayed through
# the AI participant with a persona prefix. See _classify_author.


def _channel_id_for(participant: dict, address: str) -> str | None:
    for addr in (participant.get("addresses") or []):
        if addr.get("address") == address:
            return addr.get("channelId")
    return None


# ---------------- routes: UI ----------------

@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# ---------------- routes: state ----------------

@app.get("/api/state")
async def get_state(viewedProfileId: str | None = None) -> dict:
    profiles_raw = await tw.list_profiles_for_phone(STORE_ID, TECH_PHONE)
    profiles = await asyncio.gather(*[_profile_summary(p) for p in profiles_raw])
    # sort: active first, then paused, then completed; by startedAt desc within group.
    # Stable sort: apply the secondary key first, then the primary.
    profiles.sort(key=lambda p: p.get("startedAt") or "", reverse=True)
    order = {"active": 0, "paused": 1, "completed": 2}
    profiles.sort(key=lambda p: order.get(p.get("status"), 3))

    active = next((p for p in profiles if p.get("status") == "active"), None)
    active_id = active.get("id") if active else None

    view_id = viewedProfileId or active_id
    viewed_conv = None
    viewed_profile_id = None
    if view_id:
        viewed_profile_id = view_id
        conv_id = next((p.get("conversationId") for p in profiles if p.get("id") == view_id), None)
        if conv_id:
            try:
                viewed_conv = await _conversation_view(conv_id)
                viewed_conv["profileId"] = view_id
            except tw.TwilioError as e:
                viewed_conv = {"error": str(e), "profileId": view_id}

    return {
        "techPhone": TECH_PHONE,
        "techName": TECH_NAME,
        "aiNumber": AI_NUMBER,
        "adminPersona": ADMIN_PERSONA,
        "profiles": profiles,
        "activeProfileId": active_id,
        "viewedProfileId": viewed_profile_id,
        "viewedConversation": viewed_conv,
    }


# ---------------- routes: mutations ----------------

class AssignJobBody(BaseModel):
    jobID: str
    firstName: str | None = None


@app.post("/api/assign-job")
async def assign_job(body: AssignJobBody) -> dict:
    if not body.jobID.strip():
        raise HTTPException(400, "jobID is required.")
    job_id = body.jobID.strip()

    # 1. Refuse if this tech already has an active job.
    active_pid = await tw.find_active_profile(STORE_ID, TECH_PHONE)
    if active_pid:
        active_prof = await tw.get_profile(STORE_ID, active_pid)
        active_job_id = _profile_job_id(active_prof) or "?"
        raise HTTPException(
            409,
            f"Technician {TECH_PHONE} already has an active job (jobID={active_job_id}). "
            "Complete or pause it before assigning a new one.",
        )

    # 2. Create/resolve the (phone, jobID) profile with Job.status=active.
    create_resp = await tw.create_or_resolve_profile(
        STORE_ID, TECH_PHONE, job_id, first_name=body.firstName or TECH_NAME
    )
    profile_id = create_resp["id"]
    await tw.patch_profile_traits(
        STORE_ID,
        profile_id,
        {"Job": {"status": "active", "lastActiveAt": _now_iso()}},
    )

    # 3. Reuse an existing open conversation if the profile pre-existed (resume case).
    prof = await tw.get_profile(STORE_ID, profile_id)
    existing_conv_id = _job_traits(prof).get("conversationId")
    conv = None
    conv_id = None
    channel_id = _job_traits(prof).get("channelId")
    if existing_conv_id:
        try:
            existing_conv = await tw.get_conversation(existing_conv_id)
            if existing_conv.get("status") in ("ACTIVE", "INACTIVE"):
                conv = existing_conv
                conv_id = existing_conv_id
        except tw.TwilioError:
            pass

    # 4. Otherwise mint a channelId and create a fresh conversation.
    if conv is None:
        channel_id = await tw.mint_channel_id(V1_SERVICE_SID)
        ai_profile = await _ai_agent_profile_id()
        conv = await tw.create_orchestrator_conversation(
            config_id=CONFIG_ID,
            customer_profile_id=profile_id,
            ai_agent_profile_id=ai_profile,
            tech_phone=TECH_PHONE,
            ai_number=AI_NUMBER,
            channel_id=channel_id,
        )
        conv_id = conv["id"]
        await tw.patch_profile_traits(
            STORE_ID,
            profile_id,
            {"Job": {"conversationId": conv_id, "channelId": channel_id}},
        )

    # 5. Send a welcome SMS from the dispatcher persona (real SMS + record it).
    tech = await _find_tech_participant(conv)
    ai = await _find_ai_participant(conv)
    channel_id = _channel_id_for(tech, TECH_PHONE) or channel_id
    welcome = (
        f"{ADMIN_PREFIX} Hi {TECH_NAME}, you've been assigned a new job: {job_id}. "
        "Reply here with updates when you're on site."
    )
    log.info("assign_job welcome message conversationId=%s jobId=%s", conv_id, job_id)
    try:
        await tw.send_message_action(
            conversation_id=conv_id,
            sender_participant_id=ai["id"],
            recipient_participant_id=tech["id"],
            from_address=AI_NUMBER,
            to_address=TECH_PHONE,
            body=welcome,
        )
        await tw.record_communication(
            conversation_id=conv_id,
            author_participant_id=ai["id"],
            author_address=AI_NUMBER,
            recipient_participant_id=tech["id"],
            recipient_address=TECH_PHONE,
            text=welcome,
            channel_id=channel_id,
        )
    except tw.TwilioError as e:
        log.exception("assign_job welcome send failed: %s", e)
        await tw.record_communication(
            conversation_id=conv_id,
            author_participant_id=ai["id"],
            author_address=AI_NUMBER,
            recipient_participant_id=tech["id"],
            recipient_address=TECH_PHONE,
            text=f"[send failed: {e.status_code}] {welcome}",
            channel_id=channel_id,
        )

    return {"profileId": profile_id, "conversationId": conv_id, "resumed": existing_conv_id is not None}


class SendBody(BaseModel):
    conversationId: str
    text: str


@app.post("/api/admin/send")
async def admin_send(body: SendBody) -> dict:
    conv = await tw.get_conversation(body.conversationId)
    tech = await _find_tech_participant(conv)
    ai = await _find_ai_participant(conv)
    # Dispatcher messages are relayed through the AI participant; the persona
    # prefix on the body is the only marker distinguishing them from AI replies.
    prefixed = f"{ADMIN_PREFIX} {body.text}"
    channel_id = _channel_id_for(tech, TECH_PHONE)
    log.info("admin_send conversationId=%s bodyLen=%d", body.conversationId, len(prefixed))
    result = await tw.send_message_action(
        conversation_id=body.conversationId,
        sender_participant_id=ai["id"],
        recipient_participant_id=tech["id"],
        from_address=AI_NUMBER,
        to_address=TECH_PHONE,
        body=prefixed,
    )
    # Actions dispatches the SMS but doesn't auto-create a Communication.
    # Bookkeep the outbound message so it shows in the UI + Console thread.
    await tw.record_communication(
        conversation_id=body.conversationId,
        author_participant_id=ai["id"],
        author_address=AI_NUMBER,
        recipient_participant_id=tech["id"],
        recipient_address=TECH_PHONE,
        text=prefixed,
        channel_id=channel_id,
    )
    return {"ok": True, "action": result}


@app.post("/api/tech/simulate")
async def tech_simulate(body: SendBody) -> dict:
    """Simulate an inbound SMS from the tech to the AI number, and run the full inbound pipeline."""
    return await _handle_inbound(body.conversationId, body.text, simulated=True)


class ProfileBody(BaseModel):
    profileId: str


@app.post("/api/complete-job")
async def complete_job(body: ProfileBody) -> dict:
    await tw.patch_profile_traits(
        STORE_ID,
        body.profileId,
        {"Job": {"status": "completed", "completedAt": _now_iso()}},
    )
    prof = await tw.get_profile(STORE_ID, body.profileId)
    conv_id = _job_traits(prof).get("conversationId")
    if conv_id:
        try:
            await tw.close_conversation(conv_id)
        except tw.TwilioError:
            pass
    return {"ok": True}


@app.delete("/api/profile/{profile_id}")
async def delete_profile(profile_id: str) -> dict:
    """Close the profile's conversation (if any) and delete the profile from Memory."""
    log.info("delete_profile profileId=%s", profile_id)
    try:
        prof = await tw.get_profile(STORE_ID, profile_id)
    except tw.TwilioError as e:
        if e.status_code == 404:
            raise HTTPException(404, "Profile not found.")
        raise
    conv_id = _job_traits(prof).get("conversationId")
    if conv_id:
        try:
            await tw.close_conversation(conv_id)
        except tw.TwilioError as e:
            log.warning("close_conversation on delete failed (continuing): %s", e)
    await tw.delete_profile(STORE_ID, profile_id)
    return {"ok": True, "deletedProfileId": profile_id, "closedConversationId": conv_id}


@app.post("/api/reactivate-job")
async def reactivate_job(body: ProfileBody) -> dict:
    # Pause any current active for this tech, and inactivate its Twilio conversation
    # so Orchestrator doesn't have two ACTIVE threads on the same phone number.
    active_pid = await tw.find_active_profile(STORE_ID, TECH_PHONE)
    if active_pid and active_pid != body.profileId:
        active_prof = await tw.get_profile(STORE_ID, active_pid)
        active_conv_id = _job_traits(active_prof).get("conversationId")
        await tw.patch_profile_traits(
            STORE_ID,
            active_pid,
            {"Job": {"status": "paused", "lastActiveAt": _now_iso()}},
        )
        if active_conv_id:
            try:
                await tw.set_conversation_state(active_conv_id, "INACTIVE")
                log.info("reactivate paused prior conversation %s", active_conv_id)
            except tw.TwilioError as e:
                log.warning("could not inactivate prior conversation %s: %s", active_conv_id, e)
    # Flip target to active.
    await tw.patch_profile_traits(
        STORE_ID,
        body.profileId,
        {"Job": {"status": "active", "lastActiveAt": _now_iso(), "completedAt": None}},
    )
    # If its conversation is CLOSED, create a new one; otherwise reuse.
    prof = await tw.get_profile(STORE_ID, body.profileId)
    conv_id = _job_traits(prof).get("conversationId")
    job_id = _profile_job_id(prof) or ""
    need_new = True
    if conv_id:
        try:
            conv = await tw.get_conversation(conv_id)
            if conv.get("status") in ("ACTIVE", "INACTIVE"):
                need_new = False
        except tw.TwilioError:
            pass
    if need_new:
        channel_id = await tw.mint_channel_id(V1_SERVICE_SID)
        ai_profile = await _ai_agent_profile_id()
        conv = await tw.create_orchestrator_conversation(
            config_id=CONFIG_ID,
            customer_profile_id=body.profileId,
            ai_agent_profile_id=ai_profile,
            tech_phone=TECH_PHONE,
            ai_number=AI_NUMBER,
            channel_id=channel_id,
        )
        await tw.patch_profile_traits(
            STORE_ID,
            body.profileId,
            {"Job": {"conversationId": conv["id"], "channelId": channel_id}},
        )
        conv_id = conv["id"]
    return {"ok": True, "conversationId": conv_id}


# ---------------- routes: webhook ----------------

async def _route_inbound_sms(from_: str, to: str, body: str, message_sid: str | None) -> PlainTextResponse:
    """Route an inbound SMS (from either the number-level webhook or the generic /webhook)."""
    log.info("Inbound SMS: MessageSid=%s From=%s To=%s Body=%r", message_sid, from_, to, body[:200])
    active_pid = await tw.find_active_profile(STORE_ID, from_)
    if not active_pid:
        log.warning("No active profile found for phone=%s — replying with 'not assigned' TwiML.", from_)
        twiml = (
            "<?xml version='1.0' encoding='UTF-8'?>"
            "<Response><Message>You're not currently assigned to a job. "
            "Please contact dispatch.</Message></Response>"
        )
        return PlainTextResponse(twiml, media_type="application/xml")
    prof = await tw.get_profile(STORE_ID, active_pid)
    conv_id = _job_traits(prof).get("conversationId")
    log.info("Routed to active profile=%s conversationId=%s", active_pid, conv_id)
    if not conv_id:
        log.warning("Active profile=%s has no conversationId trait — replying with fallback TwiML.", active_pid)
        twiml = (
            "<?xml version='1.0' encoding='UTF-8'?>"
            "<Response><Message>No active conversation for your job. Contact dispatch.</Message></Response>"
        )
        return PlainTextResponse(twiml, media_type="application/xml")
    await _handle_inbound(conv_id, body, simulated=False)
    return PlainTextResponse(
        "<?xml version='1.0' encoding='UTF-8'?><Response/>",
        media_type="application/xml",
    )


@app.post("/webhooks/sms/ai")
async def sms_ai_webhook(
    From: str = Form(...),
    To: str = Form(...),
    Body: str = Form(""),
    MessageSid: str | None = Form(None),
) -> PlainTextResponse:
    """Inbound SMS from tech to AI number. Route to active job's conversation, generate AI reply."""
    log.info("POST /webhooks/sms/ai")
    return await _route_inbound_sms(From, To, Body, MessageSid)


@app.post("/webhook")
async def generic_webhook(request: Request) -> PlainTextResponse:
    """Catch-all webhook: logs everything, and routes SMS-shaped payloads through the same
    inbound handler as /webhooks/sms/ai. Useful for:
      - Twilio Messaging inbound webhooks pointed here (form-encoded: From, To, Body, ...)
      - Orchestrator statusCallbacks (JSON body: conversation lifecycle events)
      - Any other Twilio callback you want to inspect during the workshop.
    """
    content_type = (request.headers.get("content-type") or "").lower()
    log.info("POST /webhook content-type=%r", content_type)

    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        form = await request.form()
        form_dict = {k: form.get(k) for k in form.keys()}
        log.info("POST /webhook form payload: %s", form_dict)
        from_ = form.get("From")
        to = form.get("To")
        body = form.get("Body", "") or ""
        message_sid = form.get("MessageSid")
        if from_ and to:
            return await _route_inbound_sms(from_, to, body, message_sid)
        log.warning("POST /webhook form had no From/To — not an SMS inbound; returning 204.")
        return PlainTextResponse("", status_code=204)

    # Non-form: probably a JSON status callback (e.g. Orchestrator lifecycle event).
    try:
        payload = await request.json()
        log.info("POST /webhook json payload: %s", payload)
    except Exception:
        raw = await request.body()
        log.info("POST /webhook non-json body (%d bytes): %r", len(raw), raw[:2000])
    return PlainTextResponse("", status_code=204)


@app.get("/webhook")
async def webhook_probe() -> PlainTextResponse:
    """GET on /webhook so it's easy to sanity-check reachability from a browser or curl."""
    log.info("GET /webhook (probe)")
    return PlainTextResponse("ok")


@app.post("/webhook/cintel")
async def cintel_webhook(request: Request) -> PlainTextResponse:
    """CINTEL post-conversation callback receiver. Observation-only for now —
    logs the payload so we can inspect its shape before wiring up profile writes."""
    content_type = (request.headers.get("content-type") or "").lower()
    log.info(
        "POST /webhook/cintel content-type=%r headers=%s",
        content_type,
        dict(request.headers),
    )
    try:
        payload = await request.json()
        log.info("POST /webhook/cintel json payload: %s", payload)
    except Exception:
        raw = await request.body()
        log.info("POST /webhook/cintel non-json body (%d bytes): %r", len(raw), raw[:4000])
    return PlainTextResponse("", status_code=204)


@app.get("/webhook/cintel")
async def cintel_webhook_probe() -> PlainTextResponse:
    log.info("GET /webhook/cintel (probe)")
    return PlainTextResponse("ok")


async def _handle_inbound(conversation_id: str, text: str, simulated: bool) -> dict:
    """Shared: record inbound tech message, generate AI reply, send it back."""
    log.info("_handle_inbound conversationId=%s simulated=%s text=%r", conversation_id, simulated, text[:200])
    conv = await tw.get_conversation(conversation_id)
    tech = await _find_tech_participant(conv)
    ai = await _find_ai_participant(conv)
    channel_id = None
    for addr in (tech.get("addresses") or []):
        if addr.get("address") == TECH_PHONE:
            channel_id = addr.get("channelId")
            break

    # 1. Record the inbound tech message.
    await tw.record_communication(
        conversation_id=conversation_id,
        author_participant_id=tech["id"],
        author_address=TECH_PHONE,
        recipient_participant_id=ai["id"],
        recipient_address=AI_NUMBER,
        text=text,
        channel_id=channel_id,
    )

    # 2. Recall context.
    profile_id = tech.get("profileId")
    prof = await tw.get_profile(STORE_ID, profile_id) if profile_id else {}
    job_id = _profile_job_id(prof) or "unknown"
    recall_data = {}
    if profile_id:
        try:
            recall_data = await tw.recall(
                STORE_ID, profile_id, conversation_id=conversation_id
            )
        except tw.TwilioError:
            recall_data = {}

    # 3. Build minimal history from recent communications.
    role_map = _build_role_map(conv)
    comms = await tw.list_communications(conversation_id)
    def _ts(c: dict) -> str:
        return c.get("occurredAt") or c.get("createdAt") or ""
    comms_sorted = sorted(comms, key=_ts)
    history: list[dict[str, str]] = []
    for c in comms_sorted:
        who = _classify_author(c, role_map)
        body = ((c.get("content") or {}).get("text")) or ""
        if not body:
            continue
        if who == "admin":
            # Dispatcher lines were sent through the AI participant with a persona
            # prefix. Strip the prefix and tag them inline so the LLM sees them
            # as third-party context and doesn't attribute them to itself.
            stripped = _strip_admin_prefix(body)
            history.append({
                "role": "user",
                "content": f"[Message from human dispatcher to technician]: {stripped}",
            })
        elif who == "ai":
            history.append({"role": "assistant", "content": body})
        else:
            history.append({"role": "user", "content": body})

    # 4. Generate the AI reply and send it as a real SMS via /Actions.
    reply = await llm.generate_reply(
        tech_name=TECH_NAME,
        job_id=job_id,
        recall=recall_data,
        conversation_history=history,
        latest_message=text,
        admin_persona=ADMIN_PERSONA,
    )
    log.info("LLM reply generated (%d chars): %r", len(reply or ""), (reply or "")[:200])
    if reply:
        try:
            await tw.send_message_action(
                conversation_id=conversation_id,
                sender_participant_id=ai["id"],
                recipient_participant_id=tech["id"],
                from_address=AI_NUMBER,
                to_address=TECH_PHONE,
                body=reply,
            )
            log.info("AI reply SMS emitted OK via /Actions.")
            # Actions dispatches but doesn't auto-create a Communication — record it.
            await tw.record_communication(
                conversation_id=conversation_id,
                author_participant_id=ai["id"],
                author_address=AI_NUMBER,
                recipient_participant_id=tech["id"],
                recipient_address=TECH_PHONE,
                text=reply,
                channel_id=channel_id,
            )
        except tw.TwilioError as e:
            log.exception("AI reply /Actions call failed: %s", e)
            await tw.record_communication(
                conversation_id=conversation_id,
                author_participant_id=ai["id"],
                author_address=AI_NUMBER,
                recipient_participant_id=tech["id"],
                recipient_address=TECH_PHONE,
                text=f"[send failed: {e.status_code}] {reply}",
                channel_id=channel_id,
            )

    return {"ok": True, "reply": reply, "simulated": simulated}
