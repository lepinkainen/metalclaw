import pytest

from tools import mail

RAW = [
    {"id": "i", "name": "Inbox", "role": "inbox"},
    {"id": "o", "name": "other"},
    {"id": "x", "name": "job-offer", "parentId": "o"},
]


class _Resp:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return {"methodResponses": [["r", self.data, "s"]]}


@pytest.fixture
def jm(monkeypatch):
    monkeypatch.setattr(mail, "_FM_MAILBOXES", None)
    monkeypatch.setattr(mail, "_fm_session", lambda: {"api_url": "u", "account_id": "a", "token": "t"})
    st = {"mbs": [dict(m) for m in RAW], "calls": [], "n": 0}

    def post(url, headers, json):
        call = json["methodCalls"][0]
        st["calls"].append(call)
        if call[0] == "Mailbox/get":
            return _Resp({"list": st["mbs"]})
        if call[0] == "Mailbox/set":
            c = call[1]["create"]["c"]
            st["n"] += 1
            new = {"id": f"n{st['n']}", "name": c["name"], "parentId": c["parentId"]}
            st["mbs"].append(new)
            return _Resp({"created": {"c": {"id": new["id"]}}})
        return _Resp({"notUpdated": {"bad": {"description": "nope"}}})

    monkeypatch.setattr(mail.HTTP, "post", post)
    return st


def _kinds(st):
    return [c[0] for c in st["calls"]]


@pytest.mark.parametrize("bad", ["Inbox", "bot", "bot/", "Bot/x", "botx/y", "bot//x", "bot/../x", "bot/ x", "bot/" + "a" * 200])
def test_validate_rejects(bad):
    with pytest.raises(ValueError):
        mail._validate_bot_label(bad)


def test_validate_ok():
    assert mail._validate_bot_label(" bot/job-offer ") == ["bot", "job-offer"]
    assert mail._validate_bot_label("bot/orders/tracking") == ["bot", "orders", "tracking"]


def test_add_creates_chain(jm):
    out = mail.add_email_label(["1", "1", "bad"], "bot/job-offer")
    sets = [c[1]["create"]["c"] for c in jm["calls"] if c[0] == "Mailbox/set"]
    assert sets == [{"name": "bot", "parentId": None}, {"name": "job-offer", "parentId": "n1"}]
    upd = jm["calls"][-1][1]["update"]
    assert upd == {"1": {"mailboxIds/n2": True}, "bad": {"mailboxIds/n2": True}}
    assert out == {"label": "bot/job-offer", "updated": ["1"], "failed": {"bad": "nope"}}


def test_add_reuses_existing(jm):
    jm["mbs"] += [{"id": "b", "name": "Bot"}, {"id": "j", "name": "job-offer", "parentId": "b"}]
    mail.add_email_label(["1"], "bot/job-offer")
    assert "Mailbox/set" not in _kinds(jm)
    assert jm["calls"][-1][1]["update"] == {"1": {"mailboxIds/j": True}}


def test_remove_patches_none(jm):
    jm["mbs"] += [{"id": "b", "name": "bot"}, {"id": "j", "name": "job-offer", "parentId": "b"}]
    mail.remove_email_label(["1"], "bot/job-offer")
    assert jm["calls"][-1][1]["update"] == {"1": {"mailboxIds/j": None}}


def test_remove_missing_errors(jm):
    with pytest.raises(ValueError, match="not found"):
        mail.remove_email_label(["1"], "bot/job-offer")
    assert "Mailbox/set" not in _kinds(jm) and "Email/set" not in _kinds(jm)


def test_other_parent_not_matched(jm):
    with pytest.raises(ValueError):
        mail.remove_email_label(["1"], "bot/job-offer")  # job-offer exists only under "other"


def test_list_paths(jm):
    assert [m["path"] for m in mail.list_email_labels()] == ["Inbox", "other", "other/job-offer"]


def test_method_error_raises(jm, monkeypatch):
    jm["mbs"] += [{"id": "b", "name": "bot"}, {"id": "j", "name": "job-offer", "parentId": "b"}]
    orig = mail.HTTP.post

    def post(url, headers, json):
        if json["methodCalls"][0][0] == "Email/set":
            return type("R", (), {"raise_for_status": lambda s: None,
                                  "json": lambda s: {"methodResponses": [["error", {"type": "accountReadOnly"}, "s"]]}})()
        return orig(url, headers, json)

    monkeypatch.setattr(mail.HTTP, "post", post)
    with pytest.raises(ValueError, match="accountReadOnly"):
        mail.add_email_label(["1"], "bot/job-offer")
