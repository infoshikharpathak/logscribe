import json
import uuid

import pytest

from logscribe import seeds
from logscribe.analyzer import OpenAIAnalyzer
from logscribe.processor import ErrorEvent, ErrorProcessor
from logscribe.sampler import RawErrorChunk

SEED = {"id": "pg-down", "service_name": "bank-service", "error_type": "ConnectionRefusedError",
        "message": "Database error: ConnectionRefusedError: Connect call failed 127.0.0.1 5432",
        "symptom": "Every bank request returns 500", "cause": "Postgres was stopped",
        "fix": "Start Postgres; the pool reconnects"}


def event(error_type, message, service="bank-service", **variables):
    return ErrorEvent(id=str(uuid.uuid4()), error_type=error_type, message=message, stack_trace="",
                      key_variables=variables, timestamp="2026-11-23T00:00:00+00:00", source_file="x.log",
                      line_number=1, raw_context="", service_name=service)


def test_seeds_are_upserted_marked_and_scored(memory):
    memory.add_seed(SEED)
    memory.add_seed(SEED)  # loading again updates, doesn't duplicate
    assert len(memory.recent()) == 1
    (match,) = memory.query_similar(event("ConnectionRefusedError", SEED["message"]).embedding_text())
    assert match["metadata"]["seeded"] is True and match["metadata"]["fix"] == SEED["fix"]
    assert 0.5 < match["similarity"] <= 1.0


def test_seeded_types_dont_count_as_seen_before(memory):
    memory.add_seed(SEED)
    assert not memory.has_error_type("ConnectionRefusedError")
    memory.add(event("ConnectionRefusedError", "Database error"))
    assert memory.has_error_type("ConnectionRefusedError")


def test_second_occurrence_matches_the_first_above_unrelated_ones(memory):
    first = event("OTP_MAX_ATTEMPTS", "OTP_MAX_ATTEMPTS: Too many incorrect OTP attempts. Transfer cancelled.",
                  endpoint="/transfers/{transfer_id}/confirm", request_id="aaa")
    memory.add(first)
    memory.add(event("MERCHANT_KEY_INVALID", "MERCHANT_KEY_INVALID: Unknown merchant key.", endpoint="/payments/requests"))
    second = event("OTP_MAX_ATTEMPTS", first.message, endpoint="/transfers/{transfer_id}/confirm", request_id="bbb")
    matches = memory.query_similar(second.embedding_text(), top_k=2)
    assert matches[0]["id"] == first.id
    assert matches[0]["similarity"] == pytest.approx(1.0, abs=1e-3)  # request_id isn't embedded
    assert matches[0]["similarity"] > matches[1]["similarity"]
    assert [m["id"] for m in memory.query_similar(second.embedding_text(), top_k=2, min_similarity=0.9)] == [first.id]


def test_prompt_shows_scores_and_seeded_cause_and_fix(memory):
    memory.add_seed(SEED)
    new = event("ConnectionRefusedError", SEED["message"])
    prompt = OpenAIAnalyzer._build_prompt(new, memory.query_similar(new.embedding_text()))
    assert "service: bank-service" in prompt
    assert "[seeded example, resolved] (similarity" in prompt
    assert f"Cause: {SEED['cause']}" in prompt and f"Fix: {SEED['fix']}" in prompt


def test_json_service_name_wins_over_the_source_label():
    line = json.dumps({"level": "ERROR", "message": "m", "service_name": "agent-service", "request_id": "r1"})
    ev = ErrorProcessor().process(RawErrorChunk(line, [line], "a.log", 1, service_name="configured"))
    assert ev.service_name == "agent-service" and ev.key_variables == {"request_id": "r1"}
    assert "r1" not in ev.to_document()
    ev = ErrorProcessor().process(RawErrorChunk("ERROR boom", ["ERROR boom"], "a.log", 1, service_name="svc"))
    assert ev.service_name == "svc"


def test_seed_file_validation(tmp_path):
    good = tmp_path / "seeds.json"
    good.write_text(json.dumps({"seeded_examples": True, "incidents": [SEED]}))
    assert seeds.load_seed_file(good) == [SEED]
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"incidents": [{**SEED, "fix": ""}]}))
    with pytest.raises(ValueError, match="fix"):
        seeds.load_seed_file(bad)


def test_embedding_text_is_the_errors_identity_not_its_boilerplate():
    ev = event("ConnectionRefusedError", "Database error: ConnectionRefusedError: refused",
               endpoint="/accounts", method="GET", status_code="500", request_id="r9")
    ev.stack_trace = 'Traceback (most recent call last):\n  File "/srv/app.py", line 3\nConnectionRefusedError: [Errno 61] refused'
    text = ev.embedding_text()
    assert "endpoint: /accounts" in text and "service: bank-service" in text
    assert "exception: ConnectionRefusedError: [Errno 61] refused" in text
    assert "method" not in text and "status_code" not in text and "/srv/app.py" not in text and "r9" not in text
    assert "stack_trace" in ev.to_document() and "method=GET" in ev.to_document()  # the prompt keeps it all


def test_unrelated_errors_from_one_service_dont_outrank_the_matching_seed(memory):
    merchant_seed = {"id": "key", "service_name": "bank-service", "error_type": "MERCHANT_KEY_INVALID",
                     "message": "MERCHANT_KEY_INVALID: Unknown merchant key.", "symptom": "s", "cause": "c", "fix": "f"}
    memory.add_seed(merchant_seed)
    memory.add(event("OTP_MAX_ATTEMPTS", "OTP_MAX_ATTEMPTS: Too many incorrect OTP attempts. Transfer cancelled.",
                     endpoint="/transfers/{transfer_id}/confirm", method="POST", status_code="422"))
    new = event("MERCHANT_KEY_INVALID", "MERCHANT_KEY_INVALID: Unknown merchant key.",
                endpoint="/payments/requests", method="POST", status_code="401")
    assert memory.query_similar(new.embedding_text())[0]["id"] == "seed-key"
