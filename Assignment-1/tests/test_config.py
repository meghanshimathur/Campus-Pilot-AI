from campuspilot import config


def _clean(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)
    for k in ("LLM_PROVIDER", "GEMINI_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)


def test_provider_autodetect(monkeypatch):
    _clean(monkeypatch)
    assert config.load_settings().llm_provider is None
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    assert config.load_settings().llm_provider == "openai"
    monkeypatch.setenv("GEMINI_API_KEY", "dummy")
    assert config.load_settings().llm_provider == "gemini"


def test_keys_not_in_repr(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "super-secret-value")
    assert "super-secret-value" not in repr(config.load_settings())
