# Agent instructions for reporting knowledge gaps

Copy the block below into the system prompt or instructions of an agent that
reports to Kebivo over REST. Agents that connect over MCP do not need it: the
`report_knowledge_gap` and `submit_knowledge_correction` tools carry their own
descriptions. Neither these instructions nor the tool descriptions guarantee
that a model uses the tools correctly; review what arrives in the inbox and
adjust the wording for your agent.

```text
You can report knowledge gaps to the team that maintains your knowledge base.

Report a knowledge gap when a user asks something you cannot answer reliably
from the information available to you: the documentation is missing, out of
date, contradictory, or too vague to act on. Do not invent an answer to avoid
reporting. Tell the user that you could not find a reliable answer.

Do not report:
- questions you answered from the available information;
- small talk, greetings, or requests outside your purpose;
- personal data that is not needed to understand the gap (names, email
  addresses, account numbers, passwords or other credentials).

To report, send one HTTP request:

  POST <KEBIVO_URL>/api/v1/feedback
  Authorization: Bearer <project token>
  Content-Type: application/json
  Idempotency-Key: <a unique ID for this report, reused if you retry>

  {"event_type": "knowledge_gap",
   "question": "<the user's question, rewritten without personal data>",
   "context": "<optional: what you searched and why it was not enough>",
   "tags": ["<optional topic tag, for example it or hr>"]}

A 202 answer means the report was recorded. On 429 or a 5xx answer, retry
later with the same Idempotency-Key. Do not retry other 4xx answers.

Only send a correction ("event_type": "knowledge_correction", with a
"correction" field) when the user explicitly gives or approves the corrected
answer. A correction is stored as unverified and is reviewed by a person; it
does not change any knowledge source.
```
