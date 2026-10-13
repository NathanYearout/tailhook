import json
from datetime import UTC, datetime

import pytest
from botocore.exceptions import ClientError

from app import agent, store
from app.simulator import event_body

T0 = datetime(2026, 10, 12, 14, 0, tzinfo=UTC)


def text(value):
    return {"text": value}


def tool_use(name, args, id="tooluse_1"):
    return {"toolUse": {"toolUseId": id, "name": name, "input": args}}


def reply(stop_reason, *content):
    return {
        "stopReason": stop_reason,
        "output": {"message": {"role": "assistant", "content": list(content)}},
        "usage": {"inputTokens": 1000, "outputTokens": 100},
    }


class FakeClient:
    """Stands in for the bedrock-runtime client: hands back scripted replies, keeps the requests."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def converse(self, **request):
        self.requests.append({**request, "messages": list(request["messages"])})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture(autouse=True)
def declined_payment():
    body = event_body("payment.failed", "pay_1", T0, code="51")
    store.record_receipt("msg_1", body, "r1", now=T0)


def test_tool_call_then_answer():
    client = FakeClient(
        reply("tool_use", tool_use("payment_timeline", {"payment_id": "pay_1"})),
        reply("end_turn", text("Declined with code 51 (msg_1).")),
    )

    result = agent.ask("Why did pay_1 fail?", client=client)

    assert result["answer"] == "Declined with code 51 (msg_1)."
    assert [step["tool"] for step in result["steps"]] == ["payment_timeline"]
    tool_result = client.requests[1]["messages"][-1]["content"][0]["toolResult"]
    assert tool_result["toolUseId"] == "tooluse_1"
    assert json.loads(tool_result["content"][0]["text"])[0]["decline_code"] == "51"


def test_requests_carry_system_and_tools():
    client = FakeClient(reply("end_turn", text("ok")))

    agent.ask("hi", client=client)

    request = client.requests[0]
    assert request["system"] == [{"text": agent.SYSTEM}]
    assert request["inferenceConfig"] == {"maxTokens": agent.MAX_TOKENS}
    assert {tool["toolSpec"]["name"] for tool in request["toolConfig"]["tools"]} == {
        "payment_timeline",
        "get_event",
        "search_events",
        "explain_decline_code",
    }


def test_loop_stops_after_six_rounds():
    looping = reply("tool_use", tool_use("payment_timeline", {"payment_id": "pay_1"}))
    client = FakeClient(*[looping] * agent.MAX_ROUNDS)

    result = agent.ask("Why?", client=client)

    assert "ran out of lookups" in result["answer"]
    assert len(client.requests) == agent.MAX_ROUNDS


def test_filtered_reply_ends_loop():
    client = FakeClient(reply("content_filtered", tool_use("get_event", {"event_id": "msg_1"})))

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
    assert result["cost_usd"] == pytest.approx((2000 * 0.15 + 200 * 0.60) / 1_000_000)


def test_bedrock_errors_mean_model_unavailable():
    denied = ClientError({"Error": {"Code": "AccessDeniedException"}}, "Converse")

    with pytest.raises(agent.ModelUnavailable):
        agent.ask("?", client=FakeClient(denied))


def test_tools_return_errors():
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
