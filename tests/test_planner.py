"""Planner routing and the late-binding resolvers."""

import pytest

from app.planner import heuristic_plan, make_plan, resolve_args, _validate


def tools_for(question: str) -> list[str]:
    _, actions, _ = make_plan(question)
    return [a["tool"] for a in actions]


def test_a_policy_question_only_searches():
    assert tools_for("Can a support ticket page on-call?") == ["web_search"]


def test_a_metrics_question_adds_a_sql_step():
    assert tools_for("What is the average first response time by plan?") == [
        "web_search", "query_tickets",
    ]


def test_a_comparison_question_adds_a_calculator_step():
    assert tools_for("How does average first response compare across plans?") == [
        "web_search", "query_tickets", "calculator",
    ]


def test_an_explicit_save_request_adds_the_write_step():
    assert tools_for("What is our CSAT by plan and save a report about it") == [
        "web_search", "query_tickets", "save_report",
    ]


def test_a_subject_word_alone_does_not_trigger_a_metrics_query():
    """"ticket" used to be enough, which ran pointless SQL for policy questions."""
    assert "query_tickets" not in tools_for("What is the escalation ticket procedure?")


def test_csat_intent_selects_the_closed_only_query():
    _, actions, _ = make_plan("What is average CSAT by plan?")
    sql = actions[1]["args"]["sql"]
    assert "status = 'closed'" in sql and "csat IS NOT NULL" in sql


def test_plan_step_ids_are_sequential_and_match_their_actions():
    steps, actions = heuristic_plan("Compare average csat by plan and save a report")
    assert [s.id for s in steps] == [f"s{i}" for i in range(1, len(steps) + 1)]
    assert [a["step_id"] for a in actions] == [s.id for s in steps]


def test_the_planner_used_is_reported():
    assert make_plan("anything")[2] == "heuristic"


# --------------------------------------------------------------------------- #
# late binding
# --------------------------------------------------------------------------- #
TABLE = (
    "plan | n | avg_first_response_mins\n"
    "enterprise | 14 | 12.7\n"
    "business | 14 | 64.7\n"
    "self_serve | 7 | 497.0\n"
    "(3 rows)"
)


def observation(result, tool="query_tickets", ok=True):
    return {
        "step_id": "s2", "tool": tool, "ok": ok, "result": result,
        "error": None, "duration_ms": 1, "attempts": 1,
    }


def test_ratio_resolver_builds_an_expression_from_the_widest_gap():
    args = resolve_args(
        {"step_id": "s3", "tool": "calculator", "args": {}, "derive": "ratio_from_last_table"},
        "compare",
        [observation(TABLE)],
    )
    assert args == {"expression": "497.0 / 12.7"}


def test_ratio_resolver_fails_clearly_with_no_query_result():
    with pytest.raises(ValueError, match="no successful query_tickets"):
        resolve_args(
            {"step_id": "s3", "tool": "calculator", "args": {}, "derive": "ratio_from_last_table"},
            "compare",
            [observation("boom", ok=False)],
        )


def test_ratio_resolver_fails_clearly_with_one_row():
    with pytest.raises(ValueError, match="two data rows"):
        resolve_args(
            {"step_id": "s3", "tool": "calculator", "args": {}, "derive": "ratio_from_last_table"},
            "compare",
            [observation("plan | n\nenterprise | 14\n(1 rows)")],
        )


def test_report_resolver_produces_a_title_and_a_body():
    args = resolve_args(
        {"step_id": "s3", "tool": "save_report", "args": {}, "derive": "report_from_observations"},
        "What is our CSAT by plan?",
        [observation(TABLE)],
    )
    assert args["title"] == "What is our CSAT by plan"
    assert "query_tickets" in args["body"]


def test_an_unknown_resolver_is_rejected():
    with pytest.raises(ValueError, match="unknown resolver"):
        resolve_args(
            {"step_id": "s1", "tool": "calculator", "args": {}, "derive": "nope"}, "q", []
        )


def test_actions_without_a_derive_pass_their_args_through():
    args = resolve_args({"step_id": "s1", "tool": "calculator", "args": {"expression": "1+1"}}, "q", [])
    assert args == {"expression": "1+1"}


# --------------------------------------------------------------------------- #
# the planning guardrail
# --------------------------------------------------------------------------- #
def test_a_hallucinated_tool_name_is_rejected_at_planning_time():
    with pytest.raises(ValueError, match="unknown tool"):
        _validate([{"label": "x", "tool": "rm_rf", "args": {}}])


def test_arguments_violating_a_tool_schema_are_rejected_at_planning_time():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _validate([{"label": "x", "tool": "calculator", "args": {"wrong_key": 1}}])


def test_an_empty_plan_is_rejected():
    with pytest.raises(ValueError, match="no steps"):
        _validate([])


def test_a_valid_llm_plan_is_accepted_and_normalised():
    steps, actions = _validate(
        [{"label": "Add", "tool": "calculator", "args": {"expression": "1+1"}}]
    )
    assert steps[0].id == "s1"
    assert actions[0] == {"step_id": "s1", "tool": "calculator", "args": {"expression": "1+1"}}
