"""`opencortyx doctor` on a new installation: the built-in models are judged as what they are."""
import pytest

from opencortyx import doctor


@pytest.mark.anyio
async def test_the_built_in_generation_model_needs_no_server_or_key(monkeypatch, capsys):
    """Qwen3-0.6B runs inside OpenCortyx: no address to reach, no key. It was reported as a hosted
    model with its key MISSING, so every fresh installation failed `doctor`."""
    from opencortyx.core.inference import hf_hub
    monkeypatch.setattr(hf_hub, "is_downloaded", lambda repo: True)
    gen = {"provider": "transformers", "model_id": "Qwen/Qwen3-0.6B", "base_url": None, "api_key": None}
    assert await doctor._probe_generation("workspace W", gen) == doctor.OK
    assert "in-process, downloaded" in capsys.readouterr().out

    monkeypatch.setattr(hf_hub, "is_downloaded", lambda repo: False)
    assert await doctor._probe_generation("workspace W", gen) == doctor.WARN, "downloads on first use"


@pytest.mark.anyio
async def test_a_hosted_model_without_a_key_still_fails(capsys):
    gen = {"provider": "openai", "model_id": "gpt-4o-mini", "base_url": None, "api_key": None}
    assert await doctor._probe_generation("workspace W", gen) == doctor.FAIL
    assert "key MISSING" in capsys.readouterr().out


@pytest.mark.anyio
async def test_before_the_admin_exists_the_embedder_is_not_a_failure(monkeypatch, capsys):
    async def no_rows(role):
        return []
    monkeypatch.setattr(doctor, "_default_rows", no_rows)
    assert await doctor.check_embedding() == doctor.WARN
    assert "no workspace yet" in capsys.readouterr().out
