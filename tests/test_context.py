"""An event's context is its own request's lines, not the whole buffer."""

import json
from types import SimpleNamespace

from logscribe.analyzer import AgentForgeAnalyzer, OpenAIAnalyzer, build_analyzer
from logscribe.incident import OnCallCurator
from logscribe.processor import ErrorProcessor
from logscribe.sampler import RawErrorChunk


def j(**fields):
    return json.dumps({"timestamp": "2026-11-23T00:12:00+00:00", "service_name": "bank-service", **fields})


PG = j(level="ERROR", message="Database error: ConnectionRefusedError: [Errno 61] Connection refused",
       request_id="req-pg", endpoint="/accounts", error_type="ConnectionRefusedError",
       stack_trace="Traceback (most recent call last):\n  File \"db.py\"\nConnectionRefusedError: refused")
OTP1 = j(level="WARNING", message="OTP_INVALID: Incorrect OTP. 2 attempt(s) remaining.", request_id="req-1",
         error_type="OTP_INVALID")
OTP3 = j(level="ERROR", message="OTP_MAX_ATTEMPTS: Too many incorrect OTP attempts. Transfer cancelled.",
         request_id="req-3", endpoint="/transfers/{transfer_id}/confirm", error_type="OTP_MAX_ATTEMPTS", attempts=3)
OTP3_INFO = j(level="INFO", message="transfer cancelled", request_id="req-3")


def case3_event(buffer):
    return ErrorProcessor().process(RawErrorChunk(OTP3, buffer, "bank-service.log", len(buffer)))


def test_another_requests_stack_trace_isnt_taken_as_this_errors():
    ev = case3_event([PG, j(level="INFO", message="request completed", request_id="req-ok"), OTP1, OTP3_INFO, OTP3])
    assert ev.stack_trace == ""
    assert ev.related_lines == [OTP3_INFO, OTP3]
    assert "ConnectionRefused" not in ev.to_document() and "ConnectionRefused" not in ev.related_context()


def test_without_a_request_id_the_same_services_latest_lines_are_used():
    other = json.dumps({"service_name": "agent-service", "level": "INFO", "message": "agent line"})
    lines = [j(level="INFO", message=f"bank {i}") for i in range(8)] + [other]
    trigger = j(level="ERROR", message="no request id", error_type="X")
    ev = ErrorProcessor().process(RawErrorChunk(trigger, lines + [trigger], "a.log", 10))
    assert ev.related_lines == [*lines[4:8], trigger]


def test_curator_and_analyzer_prompts_carry_only_the_requests_lines():
    ev = case3_event([PG, OTP1, OTP3])
    sent = {}

    def create(**kwargs):
        sent["prompt"] = kwargs["messages"][-1]["content"]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="summary"))])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    OnCallCurator(client=client).curate(ev)
    assert "Log lines for this request" in sent["prompt"] and "OTP_MAX_ATTEMPTS" in sent["prompt"]
    assert "ConnectionRefused" not in sent["prompt"] and "OTP_INVALID" not in sent["prompt"]
    assert "ConnectionRefused" not in OpenAIAnalyzer._build_prompt(ev, [])


def test_config_repo_path_wins_over_the_env_default(monkeypatch):
    monkeypatch.setenv("LOGSCRIBE_ANALYZER", "agent_forge")
    monkeypatch.setenv("LOGSCRIBE_REPO_PATH", "/env/default")
    assert build_analyzer(repo_path="/srv/ironclad").repo_path == "/srv/ironclad"
    assert build_analyzer().repo_path == "/env/default"
    goal = AgentForgeAnalyzer._build_goal(case3_event([OTP3]), [], "/srv/ironclad")
    assert "Repository path for git correlation: /srv/ironclad\n" in goal
    assert "recent commits (git_log" in goal and "Cite the commit hash" in goal
    assert "Repository path" not in AgentForgeAnalyzer._build_goal(case3_event([OTP3]), [], None)
