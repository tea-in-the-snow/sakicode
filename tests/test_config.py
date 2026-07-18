"""Tests for sakicode.config. No network access involved."""

from sakicode.config import DEFAULT_BASE_URL, load_config

_ENV_VARS = ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_BASE_URL")


def _clear_env(monkeypatch):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_env_file_provides_key(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-from-file\n")
    monkeypatch.chdir(tmp_path)
    config = load_config()
    assert config.api_key == "sk-from-file"
    assert config.base_url == DEFAULT_BASE_URL


def test_real_env_var_wins_over_env_file(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-from-file\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    config = load_config()
    assert config.api_key == "sk-from-env"


def test_no_env_anywhere_returns_none_key(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.chdir(tmp_path)  # no .env here
    config = load_config()
    assert config.api_key is None
