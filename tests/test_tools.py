"""Tool behaviour, and specifically the sandboxes. These are the security tests."""

import pytest

from app.tools import TOOLS_BY_NAME, WRITE_TOOLS, is_write_tool, tool_schemas
from app.tools.calculator import calculator
from app.tools.csv_query import query_tickets
from app.tools.save_report import save_report
from app.tools.web_search import web_search


def test_every_tool_exposes_a_schema_and_a_description():
    for schema in tool_schemas():
        assert schema["description"].strip()
        assert schema["args_schema"].get("properties")


def test_only_save_report_is_marked_as_a_write():
    assert WRITE_TOOLS == {"save_report"}
    assert is_write_tool("save_report")
    assert not is_write_tool("query_tickets")


def test_schema_flags_which_tools_need_approval():
    by_name = {s["name"]: s for s in tool_schemas()}
    assert by_name["save_report"]["requires_approval"] is True
    assert by_name["calculator"]["requires_approval"] is False


# --------------------------------------------------------------------------- #
# calculator sandbox
# --------------------------------------------------------------------------- #
def test_calculator_evaluates_arithmetic():
    assert calculator.invoke({"expression": "(1240 - 980) / 980 * 100"}).endswith(
        str((1240 - 980) / 980 * 100)
    )


def test_calculator_renders_whole_floats_as_integers():
    assert calculator.invoke({"expression": "10 / 2"}) == "10 / 2 = 5"


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('ls')",   # the reason eval() is not used
        "open('/etc/passwd').read()",
        "(1).__class__.__bases__",
        "[x for x in range(3)]",
        "lambda: 1",
    ],
)
def test_calculator_rejects_everything_that_is_not_arithmetic(expression):
    with pytest.raises(ValueError):
        calculator.invoke({"expression": expression})


def test_calculator_rejects_a_denial_of_service_exponent():
    with pytest.raises(ValueError, match="exponent"):
        calculator.invoke({"expression": "2 ** 10 ** 9"})


def test_calculator_rejects_division_by_zero_with_a_clear_message():
    with pytest.raises(ValueError, match="zero"):
        calculator.invoke({"expression": "1 / 0"})


# --------------------------------------------------------------------------- #
# sql sandbox
# --------------------------------------------------------------------------- #
def test_query_returns_rows_with_a_header_and_a_count():
    out = query_tickets.invoke({"sql": "SELECT plan, COUNT(*) n FROM tickets GROUP BY plan"})
    assert out.splitlines()[0] == "plan | n"
    assert out.strip().endswith("rows)")


def test_query_reports_no_rows_rather_than_an_empty_string():
    assert query_tickets.invoke({"sql": "SELECT * FROM tickets WHERE plan = 'nope'"}) == "0 rows"


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE tickets",
        "UPDATE tickets SET csat = 5",
        "INSERT INTO tickets VALUES ('x')",
        "DELETE FROM tickets",
        "SELECT 1; DROP TABLE tickets",     # statement smuggling
        "ATTACH DATABASE '/etc/passwd' AS leak",
    ],
)
def test_query_rejects_every_write_and_smuggling_attempt(sql):
    with pytest.raises(ValueError):
        query_tickets.invoke({"sql": sql})


def test_query_appends_a_limit_when_none_is_given():
    # An unbounded SELECT is a memory incident waiting for a bigger table.
    out = query_tickets.invoke({"sql": "SELECT ticket_id FROM tickets"})
    assert "(35 rows)" in out or "(50 rows)" in out


def test_query_respects_an_explicit_limit():
    out = query_tickets.invoke({"sql": "SELECT ticket_id FROM tickets LIMIT 3"})
    assert "(3 rows)" in out


def test_query_rejects_a_bad_column_with_a_legible_message():
    # The message is what the model reads to correct itself instead of looping.
    with pytest.raises(ValueError, match="rejected"):
        query_tickets.invoke({"sql": "SELECT nonexistent FROM tickets"})


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #
def test_search_finds_the_right_document_for_an_oncall_question():
    hits = web_search.invoke({"query": "can a support ticket page on-call?", "limit": 2})
    assert hits[0]["source_id"] == "oncall-policy"


def test_search_results_carry_a_parseable_url_for_the_citation_chip():
    for hit in web_search.invoke({"query": "enterprise first response", "limit": 3}):
        assert hit["url"].startswith("file://")
        assert hit["snippet"]


def test_search_rejects_an_empty_query():
    with pytest.raises(ValueError):
        web_search.invoke({"query": "   "})


def test_search_honours_the_limit():
    assert len(web_search.invoke({"query": "policy", "limit": 2})) <= 2


# --------------------------------------------------------------------------- #
# write tool
# --------------------------------------------------------------------------- #
def test_save_report_writes_a_file_and_reports_its_size():
    out = save_report.invoke({"title": "Quarterly CSAT", "body": "# Body\n\nnumbers"})
    assert out["bytes"] > 0
    assert out["path"].endswith(".md")


def test_save_report_refuses_to_escape_its_directory():
    # A model-chosen filename is untrusted input.
    out = save_report.invoke({"title": "../../.ssh/authorized_keys", "body": "x"})
    assert ".ssh" not in out["path"]
    assert "/.." not in out["path"]
