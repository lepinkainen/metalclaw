import json

import httpx
import pytest

import config
import wilma
from frontends import common
from tools import mail
from tools import wilma as wilma_tools

LOGIN_PAGE = """<form><input type="text" name="Login"><input type="password" name="Password">
<input type="hidden" name="SESSIONID" value=""><input type="hidden" name="returnpath" value="">
<input type="submit" name="submit" value="Kirjaudu sisään"></form>"""

ROLES = {"payload": [
    {"type": "passwd", "name": "Parent", "slug": ""},
    {"type": "guardian", "name": "Sanni Lindblad", "slug": "/!1001"},
    {"type": "guardian", "name": "Otto Lindblad", "slug": "/!1002"},
]}

MESSAGES = {"Messages": [
    {"Id": 5, "Subject": "Pidä huolta! -koulupoliisikonsertit Järvenpäässä", "TimeStamp": "2026-10-19 16:08",
     "Sender": "Rehtori Meikäläinen", "Status": 1, "Replies": 0},
    {"Id": 4, "Subject": "Retki", "TimeStamp": "2026-10-10 09:00", "Sender": "Opettaja", "Status": 0},
]}

DETAIL = {"messages": [{
    "Id": 5, "Subject": "Pidä huolta! -koulupoliisikonsertit Järvenpäässä", "TimeStamp": "2026-10-19 16:08",
    "Sender": "Rehtori Meikäläinen", "Recipients": ["Huoltajat"],
    "ContentHtml": "<p>Hei!</p><p>Konsertti on <b>torstaina</b>.</p>",
    "ReplyList": [{"Id": 9, "TimeStamp": "2026-10-19 17:00", "Sender": "Toinen", "ContentHtml": "<p>Kiitos</p>"}],
}]}


class FakeWilma:
    """Minimal Wilma: form login, roles, message list, message detail."""

    def __init__(self, password="pw", mfa=False):
        self.password = password
        self.mfa = mfa
        self.valid_sids: set[str] = set()
        self.logins = 0
        self.calls: list[str] = []
        self.detail_json = True

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(f"{request.method} {path}")
        if path == "/login" and request.method == "GET":
            return httpx.Response(200, html=LOGIN_PAGE)
        if path == "/token":
            return httpx.Response(200, json={"Wilma2LoginID": "tok"})
        if path == "/login":
            form = dict(httpx.QueryParams(request.content.decode()))
            assert form["SESSIONID"] == "tok"
            if form["Password"] != self.password:
                return httpx.Response(303, headers={"location": "/?loginfailed"})
            self.logins += 1
            sid = f"sid{self.logins}"
            self.valid_sids = {sid}
            loc = "/mfa" if self.mfa else "/?checkcookie"
            return httpx.Response(303, headers={"location": loc, "set-cookie": f"Wilma2SID={sid}; Path=/"})
        if path == "/mfa":
            return httpx.Response(200, html='<input id="mfa-formkey" value="fk1">')
        if path == "/api/v1/accounts/me/mfa/otp/check":
            form = dict(httpx.QueryParams(request.content.decode()))
            ok = form["formkey"] == "fk1" and json.loads(form["payload"])["otp"] == wilma.totp("JBSWY3DPEHPK3PXP")
            return httpx.Response(200, json={"payload": {"success": ok}})
        if request.headers.get("cookie", "").removeprefix("Wilma2SID=") not in self.valid_sids:
            return httpx.Response(302, headers={"location": "/login"})
        if path == "/forbidden":
            return httpx.Response(403)
        if path == "/api/v1/accounts/me/roles":
            return httpx.Response(200, json=ROLES)
        if path.endswith("/messages/list"):
            return httpx.Response(200, json=MESSAGES)
        if path.endswith("/messages/5"):
            if request.url.params.get("format") == "json" and self.detail_json:
                return httpx.Response(200, json=DETAIL)
            if request.url.params.get("format") == "json":
                return httpx.Response(404)
            return httpx.Response(200, html=(
                '<div id="page-content-area"><h1>Vanha viesti</h1><table class="proptable">'
                "<tr><th>Lähettäjä</th><td>Opettaja X</td></tr><tr><th>Lähetetty</th><td>19.10.2026 16:08</td></tr>"
                '</table><div class="ckeditor hidden"><p>Vanhan Wilman sisältö</p></div></div>'
            ))
        return httpx.Response(404)


@pytest.fixture
def fake():
    return FakeWilma()


@pytest.fixture
def client(fake):
    return wilma.WilmaClient("https://w.example", "u", "pw", transport=httpx.MockTransport(fake))


@pytest.fixture
def patched(monkeypatch, client):
    monkeypatch.setattr(wilma, "get_client", lambda: client)
    return client


def test_totp_rfc6238_vector():
    # RFC 6238 appendix B, SHA1 secret "12345678901234567890", T=59 → 94287082 (last 6 digits).
    assert wilma.totp("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", at=59) == "287082"


def test_login_lazy_and_session_reused(client, fake):
    assert [s["name"] for s in client.students()] == ["Sanni Lindblad", "Otto Lindblad"]
    client.list_messages("1001")
    client.list_messages("1002")
    assert fake.logins == 1
    assert "GET /!1001/messages/list" in fake.calls


def test_relogin_when_session_cancelled(client, fake):
    client.list_messages("1001")
    fake.valid_sids = set()  # e.g. the parent logged in from a browser
    msgs = client.list_messages("1001")
    assert fake.logins == 2
    assert msgs[0]["id"] == 5


def test_bad_password_disables_further_logins(fake):
    c = wilma.WilmaClient("https://w.example", "u", "wrong", transport=httpx.MockTransport(fake))
    with pytest.raises(wilma.WilmaAuthError):
        c.list_messages(None)
    n = len(fake.calls)
    with pytest.raises(wilma.WilmaAuthError):
        c.list_messages(None)
    assert len(fake.calls) == n


def test_mfa_with_totp_secret():
    fake = FakeWilma(mfa=True)
    c = wilma.WilmaClient("https://w.example", "u", "pw", "JBSWY3DPEHPK3PXP", transport=httpx.MockTransport(fake))
    assert c.list_messages("1001")[0]["unread"] is True
    assert "POST /api/v1/accounts/me/mfa/otp/check" in fake.calls


def test_mfa_without_secret_is_actionable():
    fake = FakeWilma(mfa=True)
    c = wilma.WilmaClient("https://w.example", "u", "pw", transport=httpx.MockTransport(fake))
    with pytest.raises(wilma.WilmaAuthError, match="wilma_totp_secret"):
        c.list_messages(None)


def test_message_detail_json(client):
    msg = client.get_message("1001", 5)
    assert msg["sent_at"] == "2026-10-19T16:08+03:00"
    assert "**torstaina**" in msg["content"]
    assert msg["recipients"] == ["Huoltajat"]
    assert msg["replies"][0]["content"] == "Kiitos"


def test_message_detail_html_fallback(client, fake):
    fake.detail_json = False
    msg = client.get_message("1001", 5)
    assert msg["subject"] == "Vanha viesti"
    assert msg["sender"] == "Opettaja X"
    assert "Vanhan Wilman sisältö" in msg["content"]


def test_match_messages_truncated_subject_and_time():
    msgs = wilma.parse_message_list(MESSAGES)
    subj = "Viesti Wilmasta: Pidä huolta! -koulupoliisikonsertit Jä"
    assert [m["id"] for m in wilma.match_messages(msgs, subj, "19.10.2026 16:08")] == [5]
    assert wilma.match_messages(msgs, subj, "19.10.2026 18:00") == []
    assert wilma.match_messages(msgs, "Viesti Wilmasta: Jotain muuta", None) == []


def test_find_wilma_message_dedups_across_children(patched):
    out = wilma_tools.find_wilma_message(
        "Viesti Wilmasta: Pidä huolta! -koulupoliisikonsertit Jä", "19.10.2026 16:08"
    )
    assert out["status"] == "found"
    assert out["students"] == ["Otto Lindblad", "Sanni Lindblad"]
    assert "Konsertti" in out["content"]


def test_find_wilma_message_not_found(patched):
    assert wilma_tools.find_wilma_message("Viesti Wilmasta: Ei ole")["status"] == "not_found"


def test_list_and_read_tools(patched):
    out = wilma_tools.list_wilma_messages(student="sanni", unread_only=True)
    assert len(out["students"]) == 1
    assert [m["id"] for m in out["students"][0]["messages"]] == [5]
    msg = wilma_tools.read_wilma_message(5, student="1001")
    assert msg["student_name"] == "Sanni Lindblad"
    with pytest.raises(ValueError, match="no Wilma student"):
        wilma_tools.list_wilma_messages(student="nobody")


def test_slash_wilma_parse_and_format(patched):
    kwargs = common.parse_wilma_args("--student sanni --unread --count 3")
    assert kwargs == {"student": "sanni", "folder": "inbox", "unread_only": True, "limit": 3}
    text = common.format_wilma_result(wilma_tools.list_wilma_messages(**kwargs))
    assert "**Sanni Lindblad**" in text
    assert "`5`" in text
    assert "[UNREAD]" in text


def test_get_client_requires_config(cfg_file, write_config):
    write_config(cfg_file)
    wilma.reset_client()
    with pytest.raises(ValueError, match="WILMA_USERNAME"):
        wilma.get_client()


def test_wilma_env_overrides(cfg_file, write_config, monkeypatch):
    write_config(cfg_file, wilma_url="https://a.example")
    monkeypatch.setenv("WILMA_USERNAME", "envuser")
    monkeypatch.setenv("WILMA_PASSWORD", "envpw")
    config.reset_cache()
    cfg = config.get_config()
    assert (cfg.wilma_url, cfg.wilma_username, cfg.wilma_password) == ("https://a.example", "envuser", "envpw")


def test_403_on_live_session_does_not_relogin(client, fake):
    client.list_messages("1001")
    with pytest.raises(wilma.WilmaError, match="HTTP 403"):
        client.get("/forbidden")
    assert fake.logins == 1


def test_wilma_notification_detection():
    notif = {"from": "jarvenpaa@inschool.fi", "subject": "Viesti Wilmasta: Retki"}
    assert mail._is_wilma_notification(notif)
    assert not mail._is_wilma_notification({"from": "jarvenpaa@inschool.fi", "subject": "Tiedote"})
    assert not mail._is_wilma_notification({"from": "x@example.com", "subject": "Viesti Wilmasta: Retki"})
