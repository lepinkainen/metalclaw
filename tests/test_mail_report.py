import pytest

from tools import mail

MBS = {
    "by_role": {"inbox": {"id": "mb1"}, "trash": {"id": "mb9"}},
    "by_name": {},
    "by_id": {"mb1": {"name": "Inbox"}},
}


def _raw(i):
    return {
        "id": i,
        "subject": f"s{i}",
        "from": [{"name": "A", "email": "a@x.io"}],
        "to": [{"email": "me@x.io"}],
        "receivedAt": "2026-01-01T00:00:00Z",
        "keywords": {"$flagged": True},
        "preview": "p",
        "mailboxIds": {"mb1": True},
    }


class _Resp:
    def __init__(self, ids):
        self.ids = ids

    def raise_for_status(self):
        pass

    def json(self):
        return {
            "methodResponses": [
                ["Email/get", {"list": [_raw(i) for i in self.ids]}, "g"]
            ]
        }


@pytest.fixture
def fm(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(
        mail, "_fm_session", lambda: {"api_url": "u", "account_id": "a", "token": "t"}
    )
    monkeypatch.setattr(mail, "_fm_mailboxes", lambda: MBS)
    monkeypatch.setattr(mail, "_last_candidates", set())
    state = {"ids": ["1", "2"], "bodies": []}

    def post(url, headers, json):
        state["bodies"].append(json)
        return _Resp(state["ids"])

    monkeypatch.setattr(mail.HTTP, "post", post)
    return state


def test_to_and_filter(fm):
    out = mail.check_new_mail()
    assert out["emails"][0]["to"] == "me@x.io"
    assert out["emails"][0]["flagged"] is True
    assert "to" in fm["bodies"][0]["methodCalls"][1][1]["properties"]
    assert "OR" in str(fm["bodies"][0]["methodCalls"][0][1]["filter"])


def test_ledger_excludes_reported_and_idempotent(fm):
    assert mail.check_new_mail()["count"] == 2
    assert mail.mark_emails_reported(["1", "1"])["recorded"] == ["1"]
    assert mail.mark_emails_reported(["1"])["recorded"] == ["1"]  # idempotent, no error
    out = mail.check_new_mail()
    assert [e["id"] for e in out["emails"]] == ["2"]


def test_mark_rejects_unknown(fm):
    mail.check_new_mail()
    res = mail.mark_emails_reported(["2", "zzz", ""])
    assert res == {"recorded": ["2"], "rejected": ["zzz"]}
    assert [e["id"] for e in mail.check_new_mail()["emails"]] == ["1"]


def test_wilma_notification_gets_hint(fm, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        mail, "get_config", lambda: SimpleNamespace(wilma_url="https://w.example")
    )

    def post(url, headers, json):
        raw = [_raw("1"), _raw("2")]
        raw[0].update(
            subject="Viesti Wilmasta: Hei",
            **{"from": [{"name": "W", "email": "x@inschool.fi"}]},
        )
        return type(
            "R",
            (),
            {
                "raise_for_status": lambda s: None,
                "json": lambda s: {
                    "methodResponses": [["Email/get", {"list": raw}, "g"]]
                },
            },
        )()

    monkeypatch.setattr(mail.HTTP, "post", post)
    by_id = {e["id"]: e for e in mail.check_new_mail()["emails"]}
    assert "find_wilma_message" in by_id["1"]["hint"]
    assert "hint" not in by_id["2"]
