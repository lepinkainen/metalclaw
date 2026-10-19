"""Unofficial client for Visma Wilma (Finnish school information system).

Wilma has no public API; this speaks the same session-cookie + JSON endpoints
the web UI uses. Protocol mirrors github.com/aikarjal/wilmai (MIT) — check its
changelog first when something here breaks.

Wilma allows one live session per account: every login cancels the previous
one (including a browser session), so the client logs in lazily, reuses the
session, and only logs in again when Wilma says the session is gone.
"""

import base64
import hashlib
import hmac
import json
import logging
import re
import struct
import threading
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup
from markdownify import markdownify

from config import get_config

log = logging.getLogger(__name__)

_TZ = ZoneInfo("Europe/Helsinki")
_BODY_CHAR_LIMIT = 20_000
_LOGIN_FAILED_RE = re.compile(r"loginfailed", re.IGNORECASE)
_MFA_FORMKEY_RE = re.compile(r'id="mfa-formkey"\s+value="([^"]+)"')
_STUDENT_SLUG_RE = re.compile(r"/?!(\d+)")
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36"
)

FOLDERS = {
    "inbox": "/messages/list",
    "archive": "/messages/list/archive",
    "outbox": "/messages/list/outbox",
}


class WilmaError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class WilmaAuthError(WilmaError):
    pass


def totp(secret: str, at: float | None = None) -> str:
    """RFC 6238 TOTP (SHA1, 30 s, 6 digits) — what Wilma's authenticator setup key produces."""
    key = base64.b32decode(secret.replace(" ", "").upper() + "=" * (-len(secret.replace(" ", "")) % 8))
    counter = int((time.time() if at is None else at) // 30)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


def parse_timestamp(value: Any) -> datetime | None:
    """Wilma timestamps → aware datetime in Europe/Helsinki. Accepts 'YYYY-MM-DD HH:MM', 'D.M.YYYY HH:MM', ISO."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        secs = value / 1000 if abs(value) >= 1e12 else value
        return datetime.fromtimestamp(secs, _TZ)
    raw = str(value).strip().replace("klo", " ")
    raw = re.sub(r"\s+", " ", raw)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y %H.%M", "%d.%m.%Y"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=_TZ)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt.astimezone(_TZ) if dt.tzinfo else dt.replace(tzinfo=_TZ)


def _iso(value: Any) -> str | None:
    dt = parse_timestamp(value)
    return dt.isoformat(timespec="minutes") if dt else None


def _compact(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _html_to_markdown(html: str | None) -> str:
    if not html:
        return ""
    text = markdownify(html, heading_style="ATX").strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > _BODY_CHAR_LIMIT:
        text = text[:_BODY_CHAR_LIMIT] + "\n\n…(truncated)"
    return text


def parse_students(data: Any) -> list[dict[str, str]]:
    """``GET /api/v1/accounts/me/roles`` → ``[{number, name}]``. Skips the account's own ``passwd`` role."""
    roles = data.get("payload") or data.get("Payload") if isinstance(data, dict) else data
    out: dict[str, dict[str, str]] = {}
    for rec in roles if isinstance(roles, list) else []:
        if not isinstance(rec, dict):
            continue
        if str(rec.get("type", rec.get("Type", ""))) in ("passwd", "7"):
            continue
        m = _STUDENT_SLUG_RE.search(str(rec.get("slug") or rec.get("Slug") or ""))
        if not m:
            continue
        name = _compact(rec.get("name") or rec.get("Name") or m.group(1))
        out.setdefault(m.group(1), {"number": m.group(1), "name": name})
    return list(out.values())


def parse_students_from_home(html: str) -> list[dict[str, str]]:
    """Older Wilmas without the roles API: guardian home page links each child as ``/!<number>/``."""
    out: dict[str, dict[str, str]] = {}
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        m = re.match(r"/!(\d+)/?$", str(a["href"]))
        if m:
            out.setdefault(m.group(1), {"number": m.group(1), "name": _compact(a.get_text(" ")) or m.group(1)})
    return list(out.values())


def parse_message_list(data: Any) -> list[dict[str, Any]]:
    items = data.get("Messages") or data.get("messages") or [] if isinstance(data, dict) else data
    out = []
    for item in items if isinstance(items, list) else []:
        try:
            mid = int(item.get("Id") or item.get("id"))
        except (TypeError, ValueError):
            continue
        sender = item.get("Sender") or item.get("sender")
        out.append({
            "id": mid,
            "subject": _compact(item.get("Subject") or item.get("subject")),
            "sender": _compact(sender) or None,
            "sent_at": _iso(item.get("TimeStamp") or item.get("Timestamp") or item.get("Time")),
            # Wilma marks unopened messages with a truthy Status (bold in the UI).
            "unread": bool(item.get("Status")),
            "replies": int(item.get("Replies") or 0),
        })
    return out


def parse_message_json(data: Any, message_id: int) -> dict[str, Any] | None:
    """``/messages/<id>?format=json`` → message dict, or None if this Wilma doesn't speak it."""
    msgs = data.get("messages") if isinstance(data, dict) else None
    m = msgs[0] if isinstance(msgs, list) and msgs and isinstance(msgs[0], dict) else None
    if m is None:
        return None
    recipients = m.get("Recipients")
    if not isinstance(recipients, list):
        recipients = [m["Recipient"]] if m.get("Recipient") else []
    replies = [
        {
            "sender": _compact(r.get("Sender")) or None,
            "sent_at": _iso(r.get("TimeStamp")),
            "content": _html_to_markdown(r.get("ContentHtml")),
        }
        for r in m.get("ReplyList") or []
        if isinstance(r, dict)
    ]
    replies.sort(key=lambda r: r["sent_at"] or "")
    return {
        "id": int(m.get("Id") or message_id),
        "subject": _compact(m.get("Subject")),
        "sender": _compact(m.get("Sender")) or None,
        "sent_at": _iso(m.get("TimeStamp")),
        "recipients": [_compact(r) for r in recipients if _compact(r)],
        "content": _html_to_markdown(m.get("ContentHtml")),
        "replies": replies,
    }


def parse_message_html(html: str, message_id: int) -> dict[str, Any]:
    """Fallback for older Wilmas that only serve the HTML message page."""
    soup = BeautifulSoup(html, "html.parser")
    area = soup.select_one("div#page-content-area") or soup
    h1 = area.find("h1")
    sender = sent_at = None
    for row in soup.select("table.proptable tr"):
        th, td = row.find("th"), row.find("td")
        if not th or not td:
            continue
        header = th.get_text(" ", strip=True).lower()
        if "lähettäjä" in header or "sender" in header:
            sender = td.get_text(" ", strip=True)
        elif "lähetetty" in header or "sent" in header:
            sent_at = _iso(td.get_text(" ", strip=True))
    body = soup.select_one("div.ckeditor")
    if body is None:
        for sel in ("table.proptable", "h1", "script", "style", "iframe"):
            for el in area.select(sel):
                el.decompose()
        body = area
    return {
        "id": message_id,
        "subject": _compact(h1.get_text(" ", strip=True)) if h1 else "",
        "sender": sender,
        "sent_at": sent_at,
        "recipients": [],
        "content": _html_to_markdown(str(body)),
        "replies": [],
    }


class WilmaClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        totp_secret: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._totp_secret = totp_secret
        self._http = httpx.Client(
            base_url=self.base_url,
            headers={"User-Agent": _USER_AGENT, "Referer": f"{self.base_url}/"},
            timeout=30.0,
            transport=transport,
        )
        self._lock = threading.RLock()
        self._logged_in = False
        # Set once Wilma rejects the credentials: further attempts could lock the account.
        self._failed: WilmaAuthError | None = None
        self._students: list[dict[str, str]] | None = None

    # --- auth ---

    def _login_token(self) -> str:
        resp = self._http.get("/token")
        if resp.status_code != 200:
            raise WilmaError(f"Wilma answered HTTP {resp.status_code} for its login token")
        try:
            return resp.json()["Wilma2LoginID"]
        except (ValueError, KeyError) as e:
            raise WilmaError("Wilma's login token wasn't in the expected format") from e

    def _login_form_fields(self) -> dict[str, str]:
        resp = self._http.get("/login")
        if resp.status_code >= 400:
            return {}
        fields: dict[str, str] = {}
        for inp in BeautifulSoup(resp.text, "html.parser").find_all("input"):
            name = str(inp.get("name") or "")
            if name and name not in ("Login", "Password") and str(inp.get("type", "text")).lower() in ("hidden", "submit"):
                fields[name] = str(inp.get("value", ""))
        return fields

    def login(self) -> None:
        with self._lock:
            if self._failed:
                raise self._failed
            self._http.cookies.clear()
            self._logged_in = False
            self._students = None
            t0 = time.monotonic()
            fields = self._login_form_fields()
            fields["SESSIONID"] = fields.get("SESSIONID") or self._login_token()
            resp = self._http.post(
                "/login",
                data={**fields, "Login": self._username, "Password": self._password},
            )
            location = resp.headers.get("location", "")
            if resp.status_code >= 500:
                raise WilmaError(f"Wilma answered HTTP {resp.status_code} while logging in")
            has_sid = "Wilma2SID" in self._http.cookies
            if _LOGIN_FAILED_RE.search(location) or (resp.status_code < 300 and _LOGIN_FAILED_RE.search(resp.text)):
                self._failed = WilmaAuthError(
                    "Wilma rejected the username/password. Fix wilma_username/wilma_password and restart."
                )
                log.warning("wilma: login rejected by Wilma; further attempts disabled until restart")
                raise self._failed
            if resp.is_redirect and has_sid and location:
                follow = self._http.get(location)
                m = _MFA_FORMKEY_RE.search(follow.text)
                if m:
                    self._answer_mfa(m.group(1))
            elif not (has_sid or resp.status_code < 300):
                raise WilmaError(f"Wilma didn't accept the login right now (HTTP {resp.status_code})")
            self._logged_in = True
            log.info("wilma: logged in latency=%.2fs", time.monotonic() - t0)

    def _answer_mfa(self, formkey: str) -> None:
        if not self._totp_secret:
            self._failed = WilmaAuthError(
                "Wilma asks for a two-step verification code. Set wilma_totp_secret "
                "(the authenticator setup key) in config.yaml or WILMA_TOTP_SECRET and restart."
            )
            raise self._failed
        resp = self._http.post(
            "/api/v1/accounts/me/mfa/otp/check",
            data={"formkey": formkey, "payload": json.dumps({"otp": totp(self._totp_secret), "action": "login"})},
        )
        if resp.status_code >= 500:
            raise WilmaError(f"Wilma answered HTTP {resp.status_code} while checking the code")
        try:
            data = resp.json()
        except ValueError as e:
            raise WilmaAuthError("two-step verification failed: unexpected response") from e
        if not ((data.get("payload") or {}).get("success") or data.get("success")):
            raise WilmaAuthError("two-step verification failed: Wilma rejected the code (check wilma_totp_secret)")

    # --- requests ---

    def _is_logged_out(self, resp: httpx.Response) -> bool:
        if resp.status_code == 401:
            return True
        if resp.is_redirect:
            target = httpx.URL(self.base_url).join(resp.headers.get("location", ""))
            return target.host == httpx.URL(self.base_url).host and target.path.rstrip("/") == "/login"
        return False

    def _get_once(self, path: str) -> httpx.Response:
        # Follow redirects by hand so a bounce to /login is seen rather than parsed as content.
        resp = self._http.get(path)
        for _ in range(10):
            if not resp.is_redirect or self._is_logged_out(resp):
                return resp
            target = httpx.URL(self.base_url).join(resp.headers["location"])
            if target.host != httpx.URL(self.base_url).host:
                raise WilmaError("Wilma redirected to another site")
            resp = self._http.get(target)
        raise WilmaError("Wilma redirected too many times")

    def _session_alive(self) -> bool:
        probe = self._http.get("/api/v1/accounts/me/roles")
        return probe.status_code not in (401, 403) and not probe.is_redirect

    def get(self, path: str, student: str | None = None) -> httpx.Response:
        if student:
            path = f"/!{student}{path}"
        with self._lock:
            if not self._logged_in:
                self.login()
            t0 = time.monotonic()
            resp = self._get_once(path)
            # 403 is either a cancelled session (another login, e.g. the browser) or just this
            # item being off-limits; probe before re-logging in, since that logs the browser out.
            if self._is_logged_out(resp) or (resp.status_code == 403 and not self._session_alive()):
                log.info("wilma: session gone (HTTP %s); logging in again", resp.status_code)
                self.login()
                resp = self._get_once(path)
            log.info(
                "wilma: GET %s status=%s latency=%.2fs",
                re.sub(r"^/![^/]+", "", path).split("?")[0], resp.status_code, time.monotonic() - t0,
            )
            if self._is_logged_out(resp):
                raise WilmaError("Wilma keeps asking to log in again")
            if resp.status_code >= 400:
                raise WilmaError(f"Wilma HTTP {resp.status_code}", status=resp.status_code)
            return resp

    # --- data ---

    def students(self) -> list[dict[str, str]]:
        with self._lock:
            if self._students is None:
                # Only a missing/non-JSON roles API means "older Wilma": fall back to the home page.
                # Anything else (5xx, network) propagates uncached so the next call retries.
                try:
                    self._students = parse_students(self.get("/api/v1/accounts/me/roles").json())
                except WilmaError as e:
                    if e.status != 404:
                        raise
                    log.warning("wilma: no roles API; reading students from the home page")
                    self._students = parse_students_from_home(self.get("/").text)
                except ValueError:
                    log.warning("wilma: roles API not JSON; reading students from the home page")
                    self._students = parse_students_from_home(self.get("/").text)
                log.info("wilma: %d student(s)", len(self._students))
            return self._students

    def list_messages(self, student: str | None, folder: str = "inbox") -> list[dict[str, Any]]:
        try:
            path = FOLDERS[folder]
        except KeyError as e:
            raise ValueError(f"unknown folder {folder!r}; use one of {sorted(FOLDERS)}") from e
        resp = self.get(path, student)
        try:
            return parse_message_list(resp.json())
        except ValueError as e:
            raise WilmaError("Wilma's message list wasn't JSON") from e

    def get_message(self, student: str | None, message_id: int) -> dict[str, Any]:
        try:
            resp = self.get(f"/messages/{message_id}?format=json", student)
            if "json" in resp.headers.get("content-type", ""):
                msg = parse_message_json(resp.json(), message_id)
                if msg:
                    return msg
        except WilmaError as e:
            if e.status != 404:
                raise
        resp = self.get(f"/messages/{message_id}", student)
        if "json" in resp.headers.get("content-type", ""):
            data = resp.json()
            return {
                "id": message_id,
                "subject": _compact(data.get("Subject")),
                "sender": _compact(data.get("Sender")) or None,
                "sent_at": _iso(data.get("TimeStamp")),
                "recipients": [],
                "content": _html_to_markdown(data.get("ContentHtml") or data.get("Content")),
                "replies": [],
            }
        return parse_message_html(resp.text, message_id)


_CLIENT: WilmaClient | None = None
_CLIENT_LOCK = threading.Lock()


def get_client() -> WilmaClient:
    """Process-wide client, built lazily from config so the session is shared by every caller."""
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            cfg = get_config()
            if not (cfg.wilma_url and cfg.wilma_username and cfg.wilma_password):
                raise ValueError(
                    "Wilma not configured. Set wilma_url, wilma_username and wilma_password in config.yaml "
                    "(or WILMA_URL / WILMA_USERNAME / WILMA_PASSWORD env vars)."
                )
            _CLIENT = WilmaClient(cfg.wilma_url, cfg.wilma_username, cfg.wilma_password, cfg.wilma_totp_secret)
        return _CLIENT


def reset_client() -> None:
    global _CLIENT
    with _CLIENT_LOCK:
        _CLIENT = None


def resolve_students(client: WilmaClient, student: str | None) -> list[dict[str, str | None]]:
    """Students to query: all of them, or those whose number/name matches ``student``.

    An account without role info (single student, older Wilma) yields one entry with
    ``number=None`` — requests then go unprefixed.
    """
    students: list[dict[str, str | None]] = [dict(s) for s in client.students()] or [{"number": None, "name": None}]
    if not student:
        return students
    needle = student.strip().lower()
    hits = [s for s in students if needle == s["number"] or needle in (s["name"] or "").lower()]
    if not hits:
        names = ", ".join(s["name"] or s["number"] or "?" for s in students)
        raise ValueError(f"no Wilma student matches {student!r} (known: {names})")
    return hits


_NOTIFICATION_PREFIX_RE = re.compile(r"^\s*(viesti wilmasta|meddelande från wilma|message from wilma)\s*:\s*", re.IGNORECASE)


def is_notification_subject(subject: str) -> bool:
    return bool(_NOTIFICATION_PREFIX_RE.match(subject))


def normalize_subject(subject: str) -> str:
    return _compact(_NOTIFICATION_PREFIX_RE.sub("", subject or "")).casefold()


def match_messages(
    messages: list[dict[str, Any]], subject: str, sent_at: str | None, tolerance: timedelta = timedelta(minutes=15)
) -> list[dict[str, Any]]:
    """Messages whose subject starts with ``subject`` (notification emails truncate it), near ``sent_at`` if given."""
    want = normalize_subject(subject)
    when = parse_timestamp(sent_at) if sent_at else None
    hits = []
    for m in messages:
        have = normalize_subject(m["subject"])
        if not want or not (have.startswith(want) or (have and want.startswith(have))):
            continue
        if when:
            mt = parse_timestamp(m["sent_at"])
            if mt and abs(mt - when) > tolerance:
                continue
        hits.append(m)
    return hits
