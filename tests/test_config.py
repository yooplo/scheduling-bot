from app.config import _calendar_accounts


def test_two_fixed_calendar_accounts_are_mapped_by_telegram_id(monkeypatch):
    for name in (
        "ALLOWED_TELEGRAM_USER_ID", "GOOGLE_REFRESH_TOKEN", "GOOGLE_CALENDAR_ID",
        "TELEGRAM_USER_1_ID", "GOOGLE_USER_1_REFRESH_TOKEN", "GOOGLE_USER_1_CALENDAR_ID",
        "TELEGRAM_USER_2_ID", "GOOGLE_USER_2_REFRESH_TOKEN", "GOOGLE_USER_2_CALENDAR_ID",
        "TELEGRAM_USER_1_USERNAME", "TELEGRAM_USER_2_USERNAME",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TELEGRAM_USER_1_ID", "111")
    monkeypatch.setenv("GOOGLE_USER_1_REFRESH_TOKEN", "first-token")
    monkeypatch.setenv("TELEGRAM_USER_2_ID", "222")
    monkeypatch.setenv("GOOGLE_USER_2_REFRESH_TOKEN", "second-token")
    monkeypatch.setenv("GOOGLE_USER_2_CALENDAR_ID", "second-calendar")
    monkeypatch.setenv("TELEGRAM_USER_2_USERNAME", "@SecondUser")

    accounts = _calendar_accounts()

    assert [(account.telegram_user_id, account.google_calendar_id) for account in accounts] == [
        (111, "primary"),
        (222, "second-calendar"),
    ]
    assert accounts[1].telegram_username == "seconduser"
