"""Slack destination validation, payload and secret boundaries."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from kebivo.destinations.payload import test_payload as make_test_payload
from kebivo.destinations.validation import ConfigError, validate_config
from kebivo.settings import Settings, SettingsError
from kebivo.tests.test_projects import make_client, write_headers
from kebivo.worker.runner import send_destination

URL = "https://hooks.slack.com/services/T123/B456/secret-canary"


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.slack.com/services/T/B/S",
        "https://hooks.slack.com.evil.test/services/T/B/S",
        "https://hooks.slack.com:444/services/T/B/S",
        "https://hooks.slack.com/other/T/B/S",
        "https://hooks.slack.com/services/T/B/S?next=1",
        "https://user@hooks.slack.com/services/T/B/S",
        "https://hooks.slack.com/services/",
    ],
)
def test_slack_rejects_non_slack_urls(url: str) -> None:
    with pytest.raises(ConfigError) as exc:
        validate_config({"type": "slack", "url": url}, {})
    assert exc.value.field == "url"


def test_slack_url_is_secret_and_private_opt_in_rejected() -> None:
    public, hidden = validate_config({"type": "slack", "url": URL}, {})
    assert public == {"timeout_seconds": 10}
    assert hidden == {"url": URL}
    _, padded = validate_config({"type": "slack", "url": f" \n{URL}\t "}, {})
    assert padded == {"url": URL}
    with pytest.raises(ConfigError) as exc:
        validate_config({"type": "slack", "url": URL, "allow_private_network": True}, {})
    assert exc.value.field == "allow_private_network"


def test_slack_api_masks_url_and_allows_rotation(tmp_path: Path) -> None:
    client, app = make_client(tmp_path / "slack.db")
    headers = write_headers(client)
    created = client.post(
        "/api/v1/destinations",
        json={"name": "Slack", "type": "slack", "config": {"url": URL}},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    identifier = created.json()["id"]
    assert created.json()["config"] == {
        "timeout_seconds": 10,
        "url_origin": "https://hooks.slack.com",
        "url_has_path": True,
        "url": {"set": True},
    }
    for response in (
        created,
        client.get("/api/v1/destinations"),
        client.get(f"/api/v1/destinations/{identifier}"),
    ):
        assert "secret-canary" not in response.text
    replacement = URL.replace("secret-canary", "replacement-canary")
    changed = client.patch(
        f"/api/v1/destinations/{identifier}",
        json={"config": {"url": replacement}},
        headers=headers,
    )
    assert changed.status_code == 200, changed.text
    assert "replacement-canary" not in changed.text
    app.state.engine.dispose()


def test_slack_block_kit_and_checked_sender(monkeypatch: Any) -> None:
    import kebivo.destinations.base as base
    from kebivo.security.secretbox import SecretBox

    sent: list[tuple[str, dict[str, str], dict[str, Any], bool]] = []

    async def post(
        url: str, headers: dict[str, str], body: bytes, *, private: bool, timeout: int
    ) -> tuple[int, None]:
        sent.append((url, headers, json.loads(body), private))
        return 200, None

    monkeypatch.setattr(base, "post_pinned", post)
    settings = Settings("Kebivo", "test", None, ":memory:", public_url="https://kebivo.test")
    box = SecretBox.from_settings(settings)
    destination = {
        "id": "slack-one",
        "type": "slack",
        "config": json.dumps({"timeout_seconds": 10}),
        "ciphertext": box.encrypt("slack-one", {"url": URL}),
        "allow_private_network": False,
    }
    message = make_test_payload()
    message.update(
        {"group_id": "group-one", "occurrence_count": 3, "tags": ["docs", "urgent"], "test": False}
    )
    message["feedback"]["question"] = "How do I <reset> & sign in?"
    result = asyncio.run(send_destination(destination, message, settings))
    assert result.outcome == "success"
    url, headers, body, private = sent[0]
    assert url == URL and not private
    assert headers["X-Delivery-ID"] == message["delivery_id"]
    assert body["text"] == "New Kebivo feedback"
    assert body["blocks"][0]["text"] == {
        "type": "plain_text",
        "text": message["feedback"]["question"],
    }
    assert body["blocks"][1]["fields"] == [
        {"type": "plain_text", "text": "Project: Test project"},
        {"type": "plain_text", "text": "Tags: docs, urgent"},
        {"type": "plain_text", "text": "Occurrences: 3"},
    ]
    assert body["blocks"][2]["elements"][0]["url"] == "https://kebivo.test/inbox/groups/group-one"
    assert "blocks" in body and "feedback" not in body

    no_link = Settings("Kebivo", "test", None, ":memory:")
    assert asyncio.run(send_destination(destination, message, no_link)).outcome == "success"
    assert len(sent[-1][2]["blocks"]) == 2

    message["feedback"]["question"] = "q" * 10000
    assert asyncio.run(send_destination(destination, message, settings)).outcome == "success"
    assert sent[-1][2]["blocks"][0]["text"]["text"] == "q" * 2999 + "…"

    message["project"]["name"] = "p" * 2500
    message["tags"] = ["t" * 2500]
    assert asyncio.run(send_destination(destination, message, settings)).outcome == "success"
    fields = sent[-1][2]["blocks"][1]["fields"]
    assert fields[0]["text"] == "Project: " + "p" * 1990 + "…"
    assert fields[1]["text"] == "Tags: " + "t" * 1993 + "…"


def test_public_url_requires_https_origin() -> None:
    for value in ("http://example.org", "https://example.org/path", "https://u@example.org"):
        with pytest.raises(SettingsError, match="PUBLIC_URL"):
            Settings.from_env({"PUBLIC_URL": value})
    assert (
        Settings.from_env({"PUBLIC_URL": "https://example.org/"}).public_url
        == "https://example.org"
    )
