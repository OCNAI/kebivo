# Roadmap

> **Nothing on this page is committed.** These are candidates the maintainers are considering after 0.1.0. There are no dates. What gets built depends on what people running Kebivo ask for, so tell us: open a feature request, or add a 👍 to an existing one.

Kebivo stays small on purpose: one container, one SQLite file, no telemetry, and no outbound connection unless you configure one. A candidate that breaks those rules will not be built in that form.

## How to influence it

- **Vote:** add a 👍 reaction to the issue or discussion for the candidate you need. We read reactions, not comment counts.
- **Explain your case:** say what you would connect Kebivo to, how many reports a week you expect, and what you do today instead. A concrete case outweighs a vote.
- **Contribute:** comment on the issue before you start, so we can agree on the design first. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Candidates (not committed)

### Destinations

Today a [webhook destination](docs/destinations.md) plus a small relay reaches most tools. The repository ships relay [recipes](examples/recipes/) for Slack, Linear, Jira, Teams and GitHub Issues.

| Candidate | Today |
| --- | --- |
| Native Slack destination | Available in the upcoming 0.2.0 release; webhook + Slack relay recipe works today |
| Native Jira destination | Webhook + Jira relay recipe |
| Native Linear destination | Webhook + Linear relay recipe |
| GitHub Issues and GitLab Issues destinations | GitHub: webhook + [GitHub Issues relay recipe](examples/recipes/github-issues/); GitLab: webhook + your own relay |
| Microsoft Teams destination | Webhook + [Teams relay recipe](examples/recipes/teams/) |
| Google Sheets and SharePoint list destinations | Webhook + your own relay |
| PostgreSQL destination (write each report to a table) | Webhook or the JSON Lines [export](docs/export.md) |

### Ways to send feedback

Today: the REST API, the MCP server, the [Python `kebivo-client`](sdk/python/README.md) and [TypeScript](sdk/typescript/README.md) SDKs, and the Python SDK's optional [LangChain callback](docs/rest-api.md#langchain-and-langgraph) for empty retrievals. Copy-paste [framework examples](examples/frameworks/) cover Claude, the OpenAI Agents SDK, LangGraph, the Vercel AI SDK, Open WebUI and Dify.

- Deeper LangGraph integration beyond the retriever callback
- Framework middleware that reports a gap when an answer has no supporting sources

### Grouping and triage

Today: exact grouping, optional semantic grouping through an LLM provider you configure, and manual merge and split. See [deduplication](docs/deduplication.md).

- Similarity grouping with embeddings, including a local embedding model
- Priority suggestions based on how often and how recently a question repeats
- Summaries of a large group
- Trends: which gaps grow or shrink week by week
- Suggestions for who owns a gap, based on earlier routing

### Knowledge improvement

- Classifying the likely cause of a gap (missing document, outdated document, retrieval miss)
- Diagnostics for RAG retrieval misses
- Draft documentation changes for a reviewer to accept or reject
- Checking whether a knowledge source still exists or has changed
- Keeping a group in sync with the knowledge-base page that fixes it
- Re-asking the original question after a fix, to check that the gap is closed

Kebivo will not edit your knowledge base on its own. Any candidate here keeps a person in the loop.

### Administration

- Several administrator accounts and roles
- Single sign-on (OIDC or SAML)
- More retention policies (per tag, per destination)
- Several organizations in one instance
- Reporting for large installations

## Not planned

- Telemetry or any connection home
- Answering questions or changing the knowledge base without a person's review
