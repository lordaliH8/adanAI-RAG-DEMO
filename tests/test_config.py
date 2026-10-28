"""Provider selection is explicit; credentials never select another endpoint."""
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.retrieval import Retriever


@pytest.fixture(autouse=True)
def isolate_provider_environment(monkeypatch):
    for name in ("LLM_PROVIDER", "LLM_BASE_URL", "LLM_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL"):
        monkeypatch.delenv(name, raising=False)


def test_legacy_avalai_dotenv_keeps_endpoint_and_key(tmp_path):
    path = tmp_path / "legacy.env"
    path.write_text("LLM_BASE_URL=https://api.avalai.org/v1\nLLM_API_KEY=legacy-test-key\n")
    config = Settings(_env_file=path)
    assert config.llm_provider == "avalai"
    assert config.llm_base_url == "https://api.avalai.org/v1"
    assert config.llm_api_key.get_secret_value() == "legacy-test-key"


@pytest.mark.parametrize("provider,endpoint", [
    ("openai", "https://api.openai.com/v1"),
    ("avalai", "https://api.avalai.org/v1"),
])
def test_explicit_profiles_have_expected_defaults(provider, endpoint):
    assert Settings(_env_file=None, llm_provider=provider).llm_base_url == endpoint


def test_existing_avalai_ir_endpoint_remains_supported():
    config = Settings(_env_file=None, llm_base_url="https://api.avalai.ir/v1/")
    assert config.llm_provider == "avalai" and config.llm_base_url == "https://api.avalai.ir/v1"


def test_openai_key_alias_requires_official_profile(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "official-test-key")
    official = Settings(_env_file=None, llm_provider="openai")
    avalai = Settings(_env_file=None, llm_provider="avalai")
    custom = Settings(_env_file=None, llm_provider="custom", llm_base_url="http://localhost:1234/v1")
    assert official.llm_api_key.get_secret_value() == "official-test-key"
    assert avalai.llm_api_key.get_secret_value() == custom.llm_api_key.get_secret_value() == ""
    assert "official-test-key" not in repr(official)
    assert "openai_api_key" not in official.model_dump()


def test_conflicting_official_keys_fail_without_printing_either_key():
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, llm_provider="openai", llm_api_key="old-private-test-key", openai_api_key="new-private-test-key")
    assert "Conflicting" in str(caught.value)
    assert "old-private-test-key" not in str(caught.value)
    assert "new-private-test-key" not in str(caught.value)


@pytest.mark.parametrize("provider,url", [
    ("openai", "https://api.avalai.org/v1"),
    ("avalai", "https://api.openai.com/v1"),
    ("openai", "https://api.openai.com.attacker.invalid/v1"),
])
def test_named_provider_rejects_mismatched_endpoint_before_network(provider, url):
    with pytest.raises(ValidationError, match="LLM_PROVIDER"):
        Settings(_env_file=None, llm_provider=provider, llm_base_url=url)


@pytest.mark.parametrize("url", ["file:///tmp/api", "https://name:private-key@provider.invalid/v1", "https://provider.invalid/v1?key=private-key", "https://provider.invalid/v1#private-key"])
def test_custom_base_rejects_non_api_urls_and_embedded_credentials(url):
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, llm_provider="custom", llm_base_url=url)
    assert "private-key" not in str(caught.value)


def test_custom_profile_requires_explicit_base():
    with pytest.raises(ValidationError, match="requires an explicit LLM_BASE_URL"):
        Settings(_env_file=None, llm_provider="custom")


def test_openai_base_environment_cannot_redirect_selected_profile(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://unselected.invalid/v1")
    assert Settings(_env_file=None, llm_provider="openai").llm_base_url == "https://api.openai.com/v1"


@pytest.mark.parametrize("key", ["replace_with_your_api_token", "your_openai_api_key", "<API_KEY>", "sk-...", "aa-..."])
def test_placeholder_credentials_are_not_treated_as_configured(key):
    config = Settings(_env_file=None, llm_provider="openai", llm_api_key=key)
    assert config.llm_api_key.get_secret_value() == ""


def test_new_installation_example_selects_official_profile_with_no_real_key():
    config = Settings(_env_file=Path(__file__).resolve().parents[1] / ".env.example")
    assert config.llm_provider == "openai"
    assert config.llm_base_url == "https://api.openai.com/v1"
    assert config.llm_api_key.get_secret_value() == ""


def test_embedding_cache_identity_changes_across_endpoint_model_and_dimensions():
    service = object.__new__(Retriever)
    config = Settings(_env_file=None, llm_provider="openai")
    service.settings = config
    identities = [service._embedding_identity()]
    service.settings = Settings(_env_file=None, llm_provider="avalai")
    identities.append(service._embedding_identity())
    service.settings = Settings(_env_file=None, llm_provider="openai", embedding_model="text-embedding-3-large")
    identities.append(service._embedding_identity())
    service.settings = Settings(_env_file=None, llm_provider="openai", embedding_dimensions=768)
    identities.append(service._embedding_identity())
    assert len(set(identities)) == 4
