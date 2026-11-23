import pytest

from logscribe.config import load_config


def test_paths_are_relative_to_the_config_and_expand_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SVC_LOGS", str(tmp_path / "elsewhere"))
    monkeypatch.delenv("UNSET_DIR", raising=False)
    cfg_path = tmp_path / "conf" / "app.toml"
    cfg_path.parent.mkdir()
    cfg_path.write_text('''
name = "ironclad"
collection = "ironclad_errors"
persist_dir = "chroma"
top_k = 3
min_similarity = 0.3
seeds = "seeds.json"

[[sources]]
service = "bank-service"
path = "${SVC_LOGS}/bank.log"

[[sources]]
service = "agent-service"
path = "${UNSET_DIR:-../logs}/agent.log"
''')
    cfg = load_config(cfg_path)
    assert cfg.name == "ironclad" and cfg.collection == "ironclad_errors" and cfg.top_k == 3
    assert [(s.service, s.path) for s in cfg.sources] == [
        ("bank-service", tmp_path / "elsewhere" / "bank.log"),
        ("agent-service", (tmp_path / "logs" / "agent.log").resolve())]
    assert cfg.persist_dir == (tmp_path / "conf" / "chroma").resolve()
    assert cfg.seeds == (tmp_path / "conf" / "seeds.json").resolve()
    assert cfg.min_similarity == 0.3


@pytest.mark.parametrize("body, message", [
    ('name = "x"', "at least one"),
    ('[[sources]]\nservice = "a"', "needs a path"),
    ('colection = "typo"\n[[sources]]\npath = "a.log"', "unknown setting"),
])
def test_bad_configs_are_rejected(tmp_path, body, message):
    path = tmp_path / "bad.toml"
    path.write_text(body)
    with pytest.raises(ValueError, match=message):
        load_config(path)
