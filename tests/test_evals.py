import json
from pathlib import Path

from evals.run import grade

CASES = {
    case["name"]: case
    for case in json.loads((Path(__file__).parents[1] / "evals" / "cases.json").read_text())[
        "cases"
    ]
}

CASE = {
    "must_include": ["500", ["once", "one time"]],
    "must_cite": ["msg_e6b"],
    "must_call": ["get_event"],
    "must_not_include": ["all clear"],
}


def outcome(answer, *tools):
    return {"answer": answer, "steps": [{"tool": tool} for tool in tools]}


def test_grade_good_answer():
    answer = "msg_e6b got a 500 three times and was delivered One Time after the fix."
    assert grade(CASE, outcome(answer, "get_event")) == []


def test_grade_reports_each_failure():
    answer = "MSG_E6B is fine, all clear."
    assert grade(CASE, outcome(answer, "search_events")) == [
        "missing '500'",
        "missing 'once / one time'",
        "didn't cite msg_e6b",
        "didn't call get_event",
        "said 'all clear'",
    ]


# For each case: a right answer, then the opposite in words the string checks used to accept.
FLIPS = {
    "worth-retrying": (
        "Yes. Code 91 means the issuer couldn't be reached, which is temporary, so retry pay_e4.",
        "Not worth retrying: code 91 on pay_e4 won't clear by itself.",
    ),
    "declined-then-paid": (
        "Yes. The first try was declined with 05, then msg_e2b succeeded.",
        "No, it never went through; msg_e2b was declined as well.",
    ),
    "duplicate-not-double-delivered": (
        "No. Two copies of msg_e11a arrived, but your app got it only once.",
        "Yes, your app received it twice: msg_e11a went out once at 14:01 and once at 14:02.",
    ),
    "refund-after-the-fix": (
        "Yes, msg_e8b was delivered on the first attempt with a 200.",
        "No, your app never received it.",
    ),
}


def test_opposite_answers_fail():
    for name, (right, wrong) in FLIPS.items():
        case = CASES[name]
        tools = case.get("must_call", [])
        assert grade(case, outcome(right, *tools)) == [], name
        assert grade(case, outcome(wrong, *tools)) != [], name
