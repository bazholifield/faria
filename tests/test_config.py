from pathlib import Path

from faria.config import Settings, load_settings


def test_code_defaults_stand_alone():
    s = Settings()
    assert s.models.conversation == "claude-sonnet-5"
    assert s.correction.max_inline_per_turn == 1
    assert s.db_path == Path("data") / "faria.db"


def test_toml_overrides(tmp_path, monkeypatch):
    monkeypatch.delenv("FARIA_DATA_DIR", raising=False)
    cfg = tmp_path / "override.toml"
    cfg.write_text(
        """
[models]
conversation = "claude-opus-4-8"

[correction]
max_inline_per_turn = 3
"""
    )
    s = load_settings(config_path=cfg)
    assert s.models.conversation == "claude-opus-4-8"
    assert s.correction.max_inline_per_turn == 3
    # untouched sections keep code defaults
    assert s.models.summarizer == "claude-haiku-4-5"


def test_env_data_dir_override(tmp_path, monkeypatch):
    monkeypatch.setenv("FARIA_DATA_DIR", str(tmp_path / "elsewhere"))
    s = load_settings(config_path=tmp_path / "does-not-exist.toml")
    assert s.data_dir == tmp_path / "elsewhere"
