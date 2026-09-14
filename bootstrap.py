"""One-off bootstrap:

  1. Register the `Job` Trait Group on the Memory Store so the app can write
     Job.status / startedAt / lastActiveAt / completedAt / conversationId / channelId
     onto profiles. Twilio Memory rejects writes to unregistered trait fields.
  2. Ensure an AI agent Memory profile exists at TWILIO_AI_NUMBER and print its ID.

Run:  python bootstrap.py
"""
from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv

load_dotenv()

import twilio_helpers as tw

STORE_ID = os.environ["MEMORY_STORE_ID"]
AI_NUMBER = os.environ["TWILIO_AI_NUMBER"]


JOB_TRAITS: dict[str, dict] = {
    "status": {
        "dataType": "STRING",
        "description": "Current job state: active, paused, or completed.",
    },
    "startedAt": {
        "dataType": "STRING",
        "description": "ISO 8601 timestamp when this job was first assigned.",
    },
    "lastActiveAt": {
        "dataType": "STRING",
        "description": "ISO 8601 timestamp when this job was last in the active state.",
    },
    "completedAt": {
        "dataType": "STRING",
        "description": "ISO 8601 timestamp when this job was marked completed.",
    },
    "conversationId": {
        "dataType": "STRING",
        "description": "Orchestrator conversation ID currently attached to this job.",
    },
    "channelId": {
        "dataType": "STRING",
        "description": "Channel SID shared by the tech + AI participants in this conversation.",
    },
}


async def register_job_trait_group() -> None:
    print(f"Registering Trait Group 'Job' on store={STORE_ID} ...")
    try:
        result = await tw.ensure_trait_group(
            STORE_ID,
            display_name="Job",
            traits=JOB_TRAITS,
            description="Per-job state for the composite-key job-scoped SMS demo.",
        )
        print(f"  OK: {result}")
    except tw.TwilioError as e:
        print(f"  FAILED: {e}")
        raise


async def ensure_ai_profile() -> None:
    print(f"\nLooking up AI profile at phone={AI_NUMBER} ...")
    candidates = await tw.lookup_profiles_by_phone(STORE_ID, AI_NUMBER)
    if candidates:
        print(f"  Found existing profile(s): {candidates}")
        print(f"\nAI_AGENT_PROFILE_ID={candidates[0]}")
        return
    print("  None found. Creating AI agent profile ...")
    resp = await tw.create_or_resolve_profile(
        STORE_ID,
        tech_phone=AI_NUMBER,
        job_id="__AI__",
        first_name="AI Agent",
    )
    print(f"  Created: {resp}")
    print(f"\nAI_AGENT_PROFILE_ID={resp['id']}")
    print("\nAdd the line above to your .env file.")


async def main() -> None:
    await register_job_trait_group()
    await ensure_ai_profile()


if __name__ == "__main__":
    asyncio.run(main())
