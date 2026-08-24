from types import SimpleNamespace

import pytest

import app.main as main


def test_failed_dependency_initialization_does_not_publish_partial_state(monkeypatch):
    settings = SimpleNamespace(
        telegram_bot_token="telegram-token",
        calendar_accounts=(SimpleNamespace(telegram_user_id=123),),
        groq_api_key="groq-key",
        groq_model="model",
        cron_job_api_key=None,
        service_base_url=None,
        scheduler_secret="scheduler-secret",
        user_timezone="Asia/Singapore",
    )
    attempts = 0

    def calendar_factory(_settings, _account):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary Google initialization failure")
        return "calendar-client"

    for name in ("_settings", "_telegram", "_calendars", "_parser", "_cron_client"):
        monkeypatch.setattr(main, name, None)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "TelegramClient", lambda token: ("telegram-client", token))
    monkeypatch.setattr(main, "CalendarClient", calendar_factory)
    monkeypatch.setattr(main, "GroqParser", lambda key, model: ("parser", key, model))

    with pytest.raises(RuntimeError, match="temporary Google"):
        main.dependencies()

    assert main._settings is None
    assert main._telegram is None
    assert main._calendars is None
    assert main._parser is None

    resolved = main.dependencies()

    assert resolved[2] == {123: "calendar-client"}
    assert attempts == 2
