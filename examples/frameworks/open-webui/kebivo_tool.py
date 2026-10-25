"""
title: Kebivo knowledge gaps
description: Report questions the model could not answer reliably to a self-hosted Kebivo inbox.
version: 0.1.0
license: Apache-2.0
"""

import asyncio
import json
import urllib.error
import urllib.request
import uuid

from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        kebivo_url: str = Field(
            "http://kebivo:8000", description="Kebivo origin as seen from the Open WebUI server"
        )
        project_token: str = Field("", description="Kebivo project token with feedback:gap")
        tags: str = Field("open-webui", description="Comma-separated tags added to every report")

    def __init__(self) -> None:
        self.valves = self.Valves()

    async def report_knowledge_gap(self, question: str, context: str = "") -> str:
        """
        Report a question you could not answer reliably from the available information,
        so the team that maintains the knowledge base can fill the gap. Use it when the
        documentation is missing, out of date, contradictory or too vague. Do not use it
        for questions you answered, small talk, or to send personal data.
        :param question: The user's question, rewritten without personal data.
        :param context: Optional: what you searched and why it was not enough.
        """
        if not self.valves.project_token:
            return "Kebivo is not configured: set the project token in the tool's valves."
        body: dict[str, object] = {"event_type": "knowledge_gap", "question": question}
        if context.strip():
            body["context"] = context
        tags = [tag.strip() for tag in self.valves.tags.split(",") if tag.strip()]
        if tags:
            body["tags"] = tags
        return await asyncio.to_thread(self._submit, body)

    def _submit(self, body: dict[str, object]) -> str:
        request = urllib.request.Request(
            self.valves.kebivo_url.rstrip("/") + "/api/v1/feedback",
            data=json.dumps(body).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.valves.project_token}",
                "Content-Type": "application/json",
                "Idempotency-Key": str(uuid.uuid4()),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            return f"The report was not recorded (Kebivo answered HTTP {error.code})."
        except (urllib.error.URLError, TimeoutError):
            return "The report was not recorded (Kebivo is unreachable)."
        return f"The knowledge gap was reported (event {result['event_id']})."
