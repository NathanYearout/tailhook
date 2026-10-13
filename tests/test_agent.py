import json
from datetime import UTC, datetime
from types import SimpleNamespace as Fake

import pytest

from app import agent, store
from app.simulator import event_body

T0 = datetime(2026, 10, 12, 14, 0, tzinfo=UTC)


def text(value):
    return Fake(type="text", text=value)


def tool_use(name, args, id="toolu_1"):
    return Fake(type="tool_use", name=name, input=args, id=id)


def reply(stop_reason, *content):
    usage = Fake(input_tokens=1000, output_tokens=100)
    return Fake(stop_reason=stop_reason, content=list(content), usage=usage)


class FakeClient:
    """Stands in for the Anthropic client: hands back scripted replies, keeps the requests."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []
        self.messages = self  # so client.messages.create(...) lands on create() below

    def create(self, **request):
        self.requests.append({**request, "messages": list(request["messages"])})
        return self.replies.pop(0)


@pytest.fixture(autouse=True)
def declined_payment():
    body = event_body("payment.failed", "pay_1", T0, code="51")
    store.record_receipt("msg_1", body, "r1", now=T0)


def test_a_tool_call_then_an_answer():
    client = FakeClient(
        reply("tool_use", tool_use("payment_timeline", {"payment_id": "pay_1"})),
        reply("end_turn", text("Declined with code 51 (msg_1).")),
    )

    result = agent.ask("Why did pay_1 fail?", client=client)

    assert result["answer"] == "Declined with code 51 (msg_1)."
    assert [step["tool"] for step in result["steps"]] == ["payment_timeline"]
    tool_result = client.requests[1]["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == "toolu_1"
    assert json.loads(tool_result["content"])[0]["decline_code"] == "51"


def test_every_request_carries_the_system_prompt_and_tools():
    client = FakeClient(reply("end_turn", text("ok")))

    agent.ask("hi", client=client)

    request = client.requests[0]
    assert request["system"] == agent.SYSTEM
    assert request["max_tokens"] == agent.MAX_TOKENS
    assert {tool["name"] for tool in request["tools"]} == {
        "payment_timeline",
        "get_event",
        "search_events",
        "explain_decline_code",
    }


def test_the_loop_stops_after_six_rounds():
    looping = reply("tool_use", tool_use("payment_timeline", {"payment_id": "pay_1"}))
    client = FakeClient(*[looping] * agent.MAX_ROUNDS)

    result = agent.ask("Why?", client=client)

    assert "ran out of lookups" in result["answer"]
    assert len(client.requests) == agent.MAX_ROUNDS


def test_a_refusal_ends_the_loop_without_running_tools():
    client = FakeClient(reply("refusal", tool_use("get_event", {"event_id": "msg_1"})))

    result = agent.ask("?", client=client)

    assert "couldn't finish" in result["answer"]
    assert result["steps"] == []


def test_cost_adds_up_across_rounds():
    client = FakeClient(
        reply("tool_use", tool_use("get_event", {"event_id": "msg_1"})),
        reply("end_turn", text("ok")),
    )

    result = agent.ask("?", client=client)

    assert result["usage"] == {"input_tokens": 2000, "output_tokens": 200}
    assert result["cost_usd"] == pytest.approx((2000 * 0.10 + 200 * 0.50) / 1_000_000)


def test_tools_return_errors_instead_of_guessing():
    def run(name, args):
        return json.loads(agent.run_tool(name, args))

    assert "error" in run("payment_timeline", {"payment_id": "pay_none"})
    assert "error" in run("get_event", {"event_id": "config#merchant"})
    assert "error" in run("explain_decline_code", {"code": "N7"})
    assert run("explain_decline_code", {"code": "51"})["reason"] == "insufficient_funds"
    assert run("get_event", {"event_id": "msg_1"})["payload"]["data"]["decline"]["code"] == "51"


def test_search_caps_its_limit(monkeypatch):
    seen = {}

    def recent(limit, **filters):
        seen.update(limit=limit, **filters)
        return []

    monkeypatch.setattr(store, "recent", recent)

    agent.run_tool(
        "search_events", {"type": "payment.failed", "status": None, "since": None, "limit": 500}
    )

    assert seen == {"limit": 50, "event_type": "payment.failed", "status": None, "since": None}


def test_model_ids_follow_the_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "bedrock")
    assert agent.model_id("haiku") == "us.anthropic.claude-haiku-5-5"
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    assert agent.model_id("opus") == "claude-opus-5-5"


def test_the_claude_api_key_always_comes_from_ssm(monkeypatch):
    # A key lying around in someone's shell must never be the one that gets billed.
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-the-shell")
    monkeypatch.setattr(agent, "_api_key", lambda: "sk-from-ssm")
    agent.make_client.cache_clear()

    assert agent.make_client().api_key == "sk-from-ssm"
    agent.make_client.cache_clear()
