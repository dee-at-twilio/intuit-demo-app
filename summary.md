# Composite-Key Job SMS Demo — API & Runtime Summary

A FastAPI three-column UI that exercises Twilio Memory's **gated composite matching-rule feature** (`phone AND jobID`) so that one field technician's phone number can resolve to many distinct Memory profiles — one per job. Twilio holds job identity; the application holds no database.

---

## Environment / prerequisites

- **Memory Store** `mem_store_01m11g22dffbaawr3d2vx99mfb` with:
  - `enforceUnique: false` on both `phone` and `jobID`
  - `matchingRules` includes `"phone AND jobID"`
  - `jobID` identifier config defined
- **Orchestrator Configuration** `conv_configuration_01m28q62mwfe398a6pz3w1hp2b`:
  - `GROUP_BY_PROFILE`
  - `memoryStoreId` → the store above
  - **SMS `captureRules` cleared** — the phone number's own webhook is the router; passive capture can't disambiguate multiple profiles on the same phone.
- **Conversations classic Service** `IS37231a3da5824da58229cd7760041bce` — used **only** to mint fresh `CH-` SIDs for use as unique `channelId` values on v2 conversations.
- One SMS-capable Twilio number (`AI_NUMBER`) — used by both AI and dispatcher personas.
- `AI_AGENT_PROFILE_ID` (from `bootstrap.py`) — the AI's own Memory profile at `AI_NUMBER`.

---

## Twilio HTTP surface used

All calls live in `twilio_helpers.py` and hit three base URLs:

| Base | Purpose |
| --- | --- |
| `https://memory.twilio.com/v1` | Trait groups, profiles, identifiers, lookup, recall, observations, summaries |
| `https://conversations.twilio.com/v1` | Classic Conversations — only for minting `channelId` |
| `https://conversations.twilio.com/v2` | Orchestrator conversations, actions, communications |

### Memory (v1)

| Method | Path | Used for |
| --- | --- | --- |
| `POST` | `/ControlPlane/Stores/{store}/TraitGroups` | Register the `Job` trait group (bootstrap) |
| `GET`  | `/ControlPlane/Stores/{store}/TraitGroups/{name}?includeTraits=true` | Idempotency check for `ensure_trait_group` |
| `PATCH`| `/ControlPlane/Stores/{store}/TraitGroups/{name}` | Merge missing traits into an existing group |
| `POST` | `/Stores/{store}/Profiles` | **Create-or-resolve composite profile.** Body: `traits.Contact:{phone,jobID,firstName}` + `traits.Job:{status,startedAt,lastActiveAt}`. With `matchingRules: "phone AND jobID"` and both identifiers non-unique, posting the same pair returns the existing profile. |
| `GET`  | `/Stores/{store}/Profiles/{id}` | Read traits (esp. `Job.status`, `Job.conversationId`, `Job.channelId`) |
| `PATCH`| `/Stores/{store}/Profiles/{id}` | Merge `Job` state (`status`, `conversationId`, `channelId`, timestamps) |
| `DELETE`| `/Stores/{store}/Profiles/{id}` | Hard delete — Memory does not soft-delete |
| `POST` | `/Stores/{store}/Profiles/Lookup` | `{idType:"phone", value}` → list of every profile id sharing that phone |
| `GET`  | `/Stores/{store}/Profiles/{id}/Identifiers/{idType}` | Pull `jobID` values off a profile to complete composite resolution |
| `POST` | `/Stores/{store}/Profiles/{id}/Recall` | Fetch observations + summaries. Optional `conversationId` scoping; app uses **unscoped** recall so LLM sees full job history across every conversation the profile ever had. |
| `POST` | `/Stores/{store}/Profiles/{id}/Observations` | CINTEL `CLASSIFICATION` results land here (e.g. sentiment labels) |
| `POST` | `/Stores/{store}/Profiles/{id}/ConversationSummaries` | CINTEL `TEXT` results land here (e.g. per-conversation summary) |

### Conversations classic (v1) — channelId mint only

| Method | Path | Used for |
| --- | --- | --- |
| `POST` | `/v1/Services/{svc}/Conversations` | Create a classic Conversation solely to harvest its `CH-` SID → reused as `channelId` on the v2 conversation |

### Conversations v2 (Orchestrator)

| Method | Path | Used for |
| --- | --- | --- |
| `POST` | `/v2/Conversations` | Create a two-participant conversation: `CUSTOMER` (tech, carries `(phone,jobID)` `profileId`) and `AI_AGENT` (carries AI profile id). Both share the same `channelId`. |
| `GET`  | `/v2/Conversations/{id}` | Read status + participants |
| `PUT`  | `/v2/Conversations/{id}` `{status}` | Set state to `ACTIVE` / `INACTIVE` / `CLOSED`. Closing a conversation triggers CINTEL/Memory extraction per config. |
| `GET`  | `/v2/Conversations/{id}/Communications?PageSize=100` | Read transcript |
| `POST` | `/v2/Conversations/{id}/Actions` | `type: SEND_MESSAGE` — actually dispatches an outbound SMS via Twilio |
| `POST` | `/v2/Conversations/{id}/Communications` | Bookkeep a message record. Used for outbound (Actions doesn't auto-record) and for inbound tech SMS. |

---

## Participant model — a design constraint worth naming

- Only **two** participants per conversation: `CUSTOMER` (tech) and `AI_AGENT` (AI).
- Twilio enforces address uniqueness across participants regardless of `channelId`, so a distinct "admin" participant at the AI number is **not allowed**.
- Dispatcher (human) messages are **relayed through the AI participant** with a `[<ADMIN_PERSONA>]` prefix in the body. The prefix is the only provenance marker; `_classify_author` in `main.py` checks the body prefix before the participant type.
- The tech's phone sees one SMS thread from the AI number; SMS groups by peer number on the handset, so mixing AI + dispatcher on one number is a physical constraint the design leans into.

---

## Application flows — what each route does

### `POST /api/assign-job`  (the flow the sequence diagram covers)

1. **Refuse if the tech already has an active job.** `find_active_profile` runs `POST /Profiles/Lookup` and then GETs each returned profile, stopping on the first `Job.status == "active"`. It returns the **full profile** — the 409 error message pulls `jobID` off that object without a second GET.
2. **Create/resolve the composite profile.** `POST /Profiles` with `Contact.{phone,jobID}` + `Job.status="active"`. Composite matching returns the same profile if the pair already exists. The response body is the resolved profile with traits — including any prior `Job.conversationId` / `Job.channelId` from a resumed profile.
3. **Reuse an open conversation if possible.** Read `existing_conv_id` and `channel_id` off the POST response (no separate GET). If `existing_conv_id` points at an `ACTIVE|INACTIVE` conversation, use it.
4. **Otherwise create a fresh conversation.** `mint_channel_id` (v1 `POST`) → `create_orchestrator_conversation` (v2 `POST`) with both participants.
5. **One PATCH writes every trait mutation:** `{status:"active", lastActiveAt, conversationId, channelId}` in a single `PATCH /Profiles/{id}` call — no intermediate status-flip.
6. Send welcome via `/Actions` (real SMS from AI number to tech, prefixed with `[<ADMIN_PERSONA>]`) → `POST /Communications` to bookkeep it.
7. Respond `{profileId, conversationId, resumed}`.

Result: on a first-time assignment the whole path is `Lookup + N GETs + POST /Profiles + v1 mint + v2 create + one PATCH + Actions + Communications`. On resume with a still-open conversation it collapses further — no v1 mint, no v2 create.

### `POST /api/admin/send`

- Prefix body with `[<ADMIN_PERSONA>]`, send via `/Actions` from AI participant to tech participant, then `POST /Communications` to record it.

### `POST /api/tech/simulate` and `POST /webhooks/sms/ai`

- Router (`_route_inbound_sms`): `find_active_profile(phone)` returns the full profile; `Job.conversationId` is read directly off it (**no re-GET**). Simulated and real inbound tech SMS share `_handle_inbound`:
  1. `POST /Communications` to record the inbound.
  2. `POST /Recall` (unscoped) to hydrate LLM context.
  3. Build history from `list_communications`, tagging `[Message from human dispatcher…]` when the body carries the admin prefix.
  4. `llm.generate_reply` → send via `/Actions` → `POST /Communications` to record the reply.
- Real webhook returns empty TwiML; if no active profile, returns `<Message>You're not currently assigned to a job…</Message>`.

### `POST /api/complete-job`

- `PATCH` profile: `Job.status="completed", completedAt`. Close the v2 conversation. CINTEL fires on close.

### `POST /api/reactivate-job`

- `find_active_profile` returns the full prior-active profile (if any); it's demoted with `PATCH Job.status="paused"` and its conversation is `PUT` to `INACTIVE` so Orchestrator doesn't hold two ACTIVE threads on the same phone.
- `GET` the target profile once to read its current `Job.conversationId` / `channelId`. If the referenced conversation is CLOSED, mint a new channelId and create a fresh v2 conversation on the **same profile**.
- **One combined `PATCH`** writes the target's new state: `{status:"active", lastActiveAt, completedAt:null, conversationId, channelId}`. Prior summaries are already attached to the profile, so the LLM sees the full history.

### `DELETE /api/profile/{id}`

- Close the profile's conversation, then hard-delete the profile. Job memory is a discrete unit.

### `POST /webhook/cintel`

- Iterates `operatorResults`. `profileId` is pulled from the `CUSTOMER` participant in `executionDetails.participants` — the composite key travels with the participant, so no attribution logic is needed.
- `outputFormat=TEXT` → `POST /ConversationSummaries` on that profile.
- `outputFormat=CLASSIFICATION` → `POST /Observations` on that profile.

### `POST /webhook` (catch-all)

- Form-encoded with `From`/`To`/`Body` → routes through `_route_inbound_sms` (same handler as `/webhooks/sms/ai`).
- JSON body → logged only (Orchestrator status callbacks etc.).

---

## Why the composite-key path is different

- **Router = one Twilio call.** `find_active_profile` + `Job.conversationId` gives you the conversation to route the inbound SMS to. No application DB.
- **Truth lives in Twilio.** `Job.status`, `Job.conversationId`, `Job.channelId`, `Contact.jobID` are all traits on the composite profile.
- **CINTEL attribution is free.** Operator results carry the CUSTOMER participant's `profileId`, which is the `(phone,jobID)` profile — summaries and observations land in the right bucket automatically.
- **Job history survives conversation churn.** Reactivating creates a fresh conversation on the same profile; prior summaries stay attached; unscoped `Recall` returns the whole timeline.
- **Deletion is one call.** `DELETE /Profiles/{id}` drops the job's memory as a discrete unit.

Without composite keys, all of the above collapses back into: two lookups per inbound message, a `jobs` / `active_job_for_phone` / `job_conversation_map` schema in the app DB, and a CINTEL callback that has to reverse-lookup `job_id` before it can save anything. Memory ends up unused because per-job writes would pool into one profile that `Recall` can't disentangle.
