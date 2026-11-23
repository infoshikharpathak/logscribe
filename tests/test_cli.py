import functools
import json
import re

from typer.testing import CliRunner

import cli
from logscribe.memory import ErrorMemory

from .conftest import BagOfWords
from .test_memory import SEED, event


def plain(output: str) -> str:
    """CLI output without ANSI colors (Rich colors it in CI, splitting words)."""
    return re.sub(r"\x1b\[[0-9;]*m", "", output)


def test_seed_command_loads_the_config_seeds(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ErrorMemory", functools.partial(ErrorMemory, embedding_function=BagOfWords()))
    (tmp_path / "seeds.json").write_text(json.dumps({"seeded_examples": True, "incidents": [SEED]}))
    (tmp_path / "app.toml").write_text(
        'collection = "app_errors"\npersist_dir = "chroma"\nseeds = "seeds.json"\n'
        '[[sources]]\nservice = "bank-service"\npath = "bank.log"\n')
    result = CliRunner().invoke(cli.app, ["seed", "--config", str(tmp_path / "app.toml")])
    assert result.exit_code == 0, result.output
    out = plain(result.output)
    assert "Loaded 1 seeded incidents into app_errors" in out and "seed-pg-down" in out
    memory = ErrorMemory(persist_dir=tmp_path / "chroma", collection_name="app_errors",
                         embedding_function=BagOfWords())
    assert memory.recent()[0]["metadata"]["seeded"] is True


def test_watch_needs_exactly_one_of_file_or_config(tmp_path):
    result = CliRunner().invoke(cli.app, ["watch"])
    out = plain(result.output)
    assert result.exit_code == 2 and "--file" in out and "not both" in out


def test_report_lines_carry_matches_and_scores(tmp_path):
    path = tmp_path / "report.jsonl"
    ev = event("OTP_MAX_ATTEMPTS", "too many", request_id="r1")
    match = {"id": "seed-x", "metadata": {"error_type": "OTP_MAX_ATTEMPTS", "message": "m", "seeded": True,
                                          "timestamp": "seeded example"}, "similarity": 0.81, "distance": 0.19}
    cli._append_report(path, ev, [match], "root cause", None, "OpenAIAnalyzer")
    entry = json.loads(path.read_text())
    assert entry["request_id"] == "r1" and entry["service_name"] == "bank-service"
    assert entry["matches"] == [{"id": "seed-x", "error_type": "OTP_MAX_ATTEMPTS", "message": "m", "seeded": True,
                                 "when": "seeded example", "similarity": 0.81, "distance": 0.19}]
