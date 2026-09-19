import time
import pytest
from biotech_jobs.engine import Engine, CompanyTimeoutError


class SlowAdapter:
    def fetch(self, company):
        time.sleep(2)
        return []


def test_hard_company_timeout(tmp_path):
    cfg = tmp_path / "companies.yml"
    cfg.write_text("companies: []\n")
    engine = Engine(cfg, tmp_path / "jobs.sqlite", company_timeout=1, request_timeout=1)
    if not hasattr(__import__('signal'), 'SIGALRM'):
        pytest.skip("SIGALRM unavailable on this platform")
    with pytest.raises(CompanyTimeoutError):
        engine._fetch_with_timeout(SlowAdapter(), {"name":"SlowCo"}, 1)


def test_active_target_profile_selects_only_matching_companies(tmp_path, monkeypatch):
    cfg = tmp_path / "companies.yml"
    cfg.write_text(
        "active_target_profile: user1\n"
        "companies:\n"
        "  - {name: user1Co, platform: empty, enabled: true, target_profile: user1}\n"
        "  - {name: OriginalCo, platform: empty, enabled: true}\n"
    )
    engine = Engine(cfg, tmp_path / "jobs.sqlite", company_timeout=1, request_timeout=1)
    messages = []
    monkeypatch.setattr(engine, "_log", messages.append)

    rows, errors, warnings, run = engine.run()

    assert rows == []
    assert errors == []
    assert warnings == []
    assert run["companies_attempted"] == 1
    assert any("target profile 'user1'" in message for message in messages)
