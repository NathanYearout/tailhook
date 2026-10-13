from evals.run import grade

CASE = {
    "must_include": ["500", ["once", "one time"]],
    "must_cite": ["msg_e6b"],
    "must_call": ["get_event"],
    "must_not_include": ["all clear"],
}


def outcome(answer, *tools):
    return {"answer": answer, "steps": [{"tool": tool} for tool in tools]}


def test_an_answer_that_meets_every_rule_passes():
    answer = "msg_e6b got a 500 three times and was delivered One Time after the fix."
    assert grade(CASE, outcome(answer, "get_event")) == []


def test_each_broken_rule_is_reported():
    answer = "MSG_E6B is fine, all clear."
    assert grade(CASE, outcome(answer, "search_events")) == [
        "missing '500'",
        "missing 'once / one time'",
        "didn't cite msg_e6b",
        "didn't call get_event",
        "said 'all clear'",
    ]
