"""OpenAI-backed AI reply generator, with Recall-injected context."""
from __future__ import annotations

import os
from typing import Any

from openai import AsyncOpenAI


_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
    return _client


SYSTEM_PROMPT_TEMPLATE = """You are an AI assistant for Intuit's field technician platform, speaking with technician {tech_name} over SMS.

The technician is currently working on job {job_id}. Keep replies concise (under 300 characters), practical, and SMS-appropriate. Acknowledge job updates, ask clarifying questions when useful, and note next steps.

A human dispatcher also participates in this thread. Their messages appear in the transcript prefixed with "[Message from human dispatcher to technician]" and, on the technician's phone, arrive in the SMS thread prefixed with "[{admin_persona}]". These are NOT your messages. Do not repeat, restate, or impersonate the dispatcher; when they've already answered or instructed the tech, defer to them and only add value if you can.

Context from prior interactions on this and related jobs:
{recall_context}
"""


def _format_recall(recall: dict[str, Any]) -> str:
    observations = recall.get("observations", []) or []
    summaries = recall.get("summaries", []) or []
    lines: list[str] = []
    if observations:
        lines.append("Observations:")
        for o in observations:
            content = o.get("content") if isinstance(o, dict) else None
            if content:
                lines.append(f"- {content}")
    if summaries:
        lines.append("")
        lines.append("Summaries:")
        for s in summaries:
            content = s.get("content") if isinstance(s, dict) else None
            if content:
                lines.append(f"- {content}")
    return "\n".join(lines) if lines else "(no prior context)"


async def generate_reply(
    tech_name: str,
    job_id: str,
    recall: dict[str, Any],
    conversation_history: list[dict[str, str]],
    latest_message: str,
    admin_persona: str = "Dispatcher",
) -> str:
    """conversation_history: list of {role: 'user'|'assistant', content: str} for recent turns."""
    system = SYSTEM_PROMPT_TEMPLATE.format(
        tech_name=tech_name or "the technician",
        job_id=job_id,
        admin_persona=admin_persona,
        recall_context=_format_recall(recall),
    )
    messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    messages.extend(conversation_history[-20:])
    messages.append({"role": "user", "content": latest_message})

    client = _get_client()
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    completion = await client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=200,
        temperature=0.4,
    )
    return (completion.choices[0].message.content or "").strip()
