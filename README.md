# Composite-Key Job SMS Demo

A three-column FastAPI app that exercises the pattern from your notebooks: one field technician, one phone number, many jobs — each job gets its own Twilio Memory profile via a composite `(phone, jobID)` matching rule, and its own Orchestrator conversation.

## What each column does

- **Left — Technician view.** SMS thread from the tech's POV. Messages from either the AI number or the admin number show inline. Includes a "Send as tech (simulated inbound)" input that runs the same routing path as a real inbound SMS.
- **Middle — Admin/AI view.** Same conversation, admin/operator perspective. Contains "Assign new job" (pauses the current active job, creates a new profile + conversation) and "Send as dispatcher" — the human's message emits from the *same* AI Twilio number, prefixed with `[<ADMIN_PERSONA>]`. The conversation has only two participants (tech CUSTOMER + AI AI_AGENT); dispatcher messages are relayed through the AI participant with the persona prefix as the provenance marker. Twilio validates address uniqueness across participants regardless of channelId, so a distinct "admin" participant at the AI number isn't permitted.
- **Right — Memory profiles.** Every profile that phone-lookup returns for the tech. Badges show `active` / `paused` / `completed`. Click a card to view its conversation in the left+middle columns. Buttons: `Complete job` (active → completed, closes conversation), `Resume` (paused → active), `Reactivate` (completed → active, creates a fresh conversation).

## Prerequisites

1. **Twilio account with composite `matchingRules` enabled** (the gated feature your notebook already uses).
2. **A Memory Store already configured** with:
   - `enforceUnique: false` on both `phone` and `jobID`
   - `matchingRules` including `"phone AND jobID"`
   - The `jobID` identifier config defined
   You already have this: `mem_store_01m11g22dffbaawr3d2vx99mfb`.
3. **An Orchestrator Configuration** with `GROUP_BY_PROFILE`, `memoryStoreId` pointing at that store, and — importantly — **no passive SMS `captureRules`** (see the setup step below). You already have `conv_configuration_01m28q62mwfe398a6pz3w1hp2b`.
4. **A Conversations classic Service** — used only to mint fresh `channelId` values. You already have `IS37231a3da5824da58229cd7760041bce`.
5. **One SMS-capable Twilio number** — used by both the AI agent and the human dispatcher. Its inbound webhook must point at this app. Dispatcher messages emit from the same number, prefixed with `[<ADMIN_PERSONA>]` so the technician sees a single SMS thread on their phone (SMS groups by peer number on the device — this is a physical constraint, not a Twilio one).
6. **OpenAI API key** for the AI replies.
7. **Python 3.10+** and **ngrok** (or any tunnel) for the inbound webhook.

## Setup

```bash
cd demo-app
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Fill in .env: Twilio SID/token, both numbers, tech phone, OpenAI key.
```

### One-time bootstrap: register the `Job` trait group + create the AI agent Memory profile

Twilio Memory rejects writes to any trait field that hasn't been declared on the store first — `Contact` works out of the box, but the custom `Job` group needs to be registered. The bootstrap script does that (idempotently) and then creates the AI agent's Memory profile:

```bash
python bootstrap.py
```

It prints something like:
```
Registering Trait Group 'Job' on store=mem_store_... ...
  OK: {...}

Looking up AI profile at phone=+44... ...
  None found. Creating AI agent profile ...

AI_AGENT_PROFILE_ID=mem_profile_01m2de4b06egxbwteesrx912nr
```

Paste the `AI_AGENT_PROFILE_ID=...` line into `.env`. The script is safe to re-run — the trait-group step no-ops if the group already exists (and merges in any newly-added fields).

### Turn off passive SMS captureRules on the Orchestrator config

Your existing config has SMS `captureRules` that auto-attach every inbound SMS to a conversation via `GROUP_BY_PROFILE`. With `enforceUnique: false` on phone (multiple profiles share the tech's number), passive capture can't pick the right profile from an inbound SMS alone — this app's webhook needs to do that decision using `Job.status`. Remove the SMS captureRules on the config so the phone number's own webhook is the routing path.

Two ways:
- **Console:** Conversations → Configurations → your config → Channel Settings → SMS → clear the capture rules and save.
- **API:** `GET` the config, set `channelSettings.SMS.captureRules = []`, `PUT` it back.

### Point the AI Twilio number's SMS webhook at this app

In the Console (Phone Numbers → Manage → Active numbers → your AI number → Messaging), set the "A MESSAGE COMES IN" webhook to `POST` `<your-ngrok-url>/webhooks/sms/ai`.

## Run

```bash
# terminal 1
uvicorn main:app --reload --port 8000

# terminal 2
ngrok http 8000
# copy the https URL into the AI number's inbound webhook in Console (once, or if the URL changes)
```

Then open `http://localhost:8000` in a browser.

## Try it

1. In the middle column, type a jobID (e.g. `4521`) and click **Assign new job**. The right column shows a new profile with `active` status. The middle/left columns show an empty conversation ready to go.
2. In the left column, type a simulated tech message and click **Send as tech**. It routes through the same code path as a real inbound SMS. The AI generates a reply (via OpenAI, informed by Recall of the profile's context) and Twilio sends it as a real SMS to your tech phone.
3. In the middle column, type a message and click **Send as dispatcher**. A real SMS goes to the tech's phone from the AI number, prefixed `[<ADMIN_PERSONA>] your message`. It appears in the same SMS thread the AI uses.
4. Repeat step 1 with a different jobID (e.g. `4522`). The previously-active profile flips to `paused`. Inbound SMSes from the tech now land on the new job's conversation.
5. Click a `paused` profile card and hit **Resume** — the current active flips to paused, this one flips to active. Its conversation resumes (or a new one is created if the old one was closed).
6. Click **Complete job** on an active profile to close its conversation and mark the profile `completed`. Memory extraction fires on close (per your config).


## Files

- `main.py` — FastAPI app, routes, and inbound-SMS routing logic.
- `twilio_helpers.py` — thin async HTTP helpers for Memory v1 + Conversations v2 (+ v1 for channelId minting).
- `llm.py` — OpenAI reply generator with Recall-injected system prompt.
- `bootstrap.py` — one-off: creates the AI agent Memory profile.
- `static/index.html`, `static/app.js`, `static/style.css` — the UI.
