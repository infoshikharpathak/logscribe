import json

from logscribe.sampler import LogSampler, MultiFileSampler, is_error_line, scan_lines
import re

PATTERNS = [re.compile(r"\bERROR\b"), re.compile(r"\bException\b")]


def jline(level, message, **extra):
    return json.dumps({"timestamp": "2026-11-23T00:00:00+00:00", "level": level, "message": message, **extra})


def test_json_lines_trigger_on_their_level_not_on_words():
    assert is_error_line(jline("ERROR", "OTP_MAX_ATTEMPTS: too many"), PATTERNS)
    assert is_error_line(jline("critical", "down"), PATTERNS)
    assert not is_error_line(jline("INFO", "retrying after Exception in worker"), PATTERNS)
    assert not is_error_line(jline("WARNING", "ERROR budget at 80%"), PATTERNS)
    # Plain text still goes by the patterns.
    assert is_error_line("2026-11-23 ERROR db down", PATTERNS)
    assert not is_error_line("2026-11-23 INFO fine", PATTERNS)


def test_scan_lines_uses_json_levels():
    chunks = scan_lines([jline("INFO", "Exception handled"), jline("ERROR", "boom")])
    assert [json.loads(c.trigger_line)["message"] for c in chunks] == ["boom"]


def write(path, *lines):
    with path.open("a") as f:
        for line in lines:
            f.write(line + "\n")


def test_multi_file_keeps_service_names_and_separate_context(tmp_path):
    bank, agent = tmp_path / "bank.log", tmp_path / "agent.log"
    write(bank, jline("INFO", "bank started"))
    write(agent, jline("INFO", "agent started"))
    sampler = MultiFileSampler([LogSampler(bank, service_name="bank-service"),
                                LogSampler(agent, service_name="agent-service")])
    sampler.open(from_start=False)  # existing lines are skipped
    assert sampler.read_available() == []
    write(bank, jline("INFO", "bank request"), jline("ERROR", "bank failed"))
    write(agent, jline("ERROR", "agent failed"))
    chunks = sampler.read_available()
    assert [(c.service_name, json.loads(c.trigger_line)["message"]) for c in chunks] == [
        ("bank-service", "bank failed"), ("agent-service", "agent failed")]
    bank_chunk, agent_chunk = chunks
    assert any("bank request" in line for line in bank_chunk.context_lines)
    assert not any("bank" in line for line in agent_chunk.context_lines)
    assert bank_chunk.source_file == str(bank)


def test_waits_for_a_file_that_doesnt_exist_yet(tmp_path):
    path = tmp_path / "later.log"
    sampler = LogSampler(path, service_name="svc")
    sampler.open()
    assert sampler.read_available() == []
    write(path, jline("ERROR", "first line"))
    (chunk,) = sampler.read_available()
    assert chunk.line_number == 1 and chunk.service_name == "svc"


def test_partial_line_is_read_once_complete(tmp_path):
    path = tmp_path / "a.log"
    path.write_text("")
    sampler = LogSampler(path)
    sampler.open()
    line = jline("ERROR", "half written")
    with path.open("a") as f:
        f.write(line[:20])
    assert sampler.read_available() == []
    with path.open("a") as f:
        f.write(line[20:] + "\n")
    (chunk,) = sampler.read_available()
    assert chunk.trigger_line == line


def test_truncated_file_is_reread_from_the_start(tmp_path):
    path = tmp_path / "a.log"
    write(path, *[jline("INFO", f"line {i}") for i in range(20)])
    sampler = LogSampler(path)
    sampler.open()
    path.write_text(jline("ERROR", "after rotation") + "\n")
    (chunk,) = sampler.read_available()
    assert json.loads(chunk.trigger_line)["message"] == "after rotation"


def test_traceback_after_a_text_trigger_is_absorbed(tmp_path):
    path = tmp_path / "a.log"
    path.write_text("")
    sampler = LogSampler(path)
    sampler.open()
    write(path, "ERROR request failed", "Traceback (most recent call last):", '  File "x.py", line 1',
          "ValueError: bad")
    (chunk,) = sampler.read_available()
    assert chunk.context_lines[-1] == "ValueError: bad"
