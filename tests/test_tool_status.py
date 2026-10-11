import asyncio
import threading

import pytest

from frontends import common
from frontends.common import ToolStatusReporter, format_tool_status


@pytest.mark.parametrize(
    "name,args,expected",
    [
        ("check_new_mail", {}, "📧 check_new_mail"),
        ("read_email", {"id": "M123abc"}, "📖 read_email(M123abc)"),
        ("search_vault", {"query": "dentist"}, '🔍 search_vault("dentist")'),
        ("mark_emails_reported", {}, "✅ mark_emails_reported"),
        ("add_email_label", {"label": "bot/job-offer"}, "🏷️ add_email_label(bot/job-offer)"),
        ("set_user_preference", {}, "🧠 set_user_preference"),
        ("create_heartbeat_action", {}, "⏰ create_heartbeat_action"),
        ("roll_die", {}, "🎲 roll_die"),
        ("whatever", {}, "🔧 whatever"),
    ],
)
def test_format_basic(name, args, expected):
    assert format_tool_status(name, args) == expected


def test_format_truncates_and_counts():
    line = format_tool_status("read_note", {"path": "x" * 200})
    assert "…" in line and len(line) < 80
    line = format_tool_status("add_email_label", {"ids": ["a", "b", "c"], "label": "L"})
    assert "ids=3 items" in line and "label='L'" in line


@pytest.fixture
def loop():
    lp = asyncio.new_event_loop()
    t = threading.Thread(target=lp.run_forever, daemon=True)
    t.start()
    yield lp
    lp.call_soon_threadsafe(lp.stop)
    t.join()
    lp.close()


def test_reporter_send_then_edit(loop):
    sent, edited = [], []

    async def send(text):
        await asyncio.sleep(0.05)  # edits must still wait for this
        sent.append(text)
        return "h1"

    async def edit(handle, text):
        edited.append((handle, text))

    r = ToolStatusReporter(loop, send, edit)
    r("check_new_mail", {}, "{}")
    r("read_email", {"id": "M1"}, "{}")
    r("roll_die", {}, "{}")
    r.flush()
    assert sent == ["📧 check_new_mail"]
    assert edited[-1] == ("h1", "📧 check_new_mail\n📖 read_email(M1)\n🎲 roll_die")
    assert len(edited) == 2


def test_reporter_swallows_errors(loop):
    calls = []

    async def send(text):
        calls.append("send")
        raise RuntimeError("boom")

    async def edit(handle, text):
        raise RuntimeError("boom")

    r = ToolStatusReporter(loop, send, edit)
    r("roll_die", {}, "{}")
    r("roll_die", {}, "{}")
    r.flush()  # must not raise
    assert calls == ["send", "send"]  # no handle yet -> retries send


def test_reporter_length_cap(loop):
    out = []

    async def send(text):
        out.append(text)
        return 1

    async def edit(h, text):
        out.append(text)

    r = ToolStatusReporter(loop, send, edit, limit=60)
    for i in range(10):
        r("roll_die", {"sides": i}, "{}")
    r.flush()
    assert all(len(t) <= 60 for t in out)
    assert out[-1].startswith("…\n") and out[-1].endswith("roll_die(9)")


def test_parse_tools_arg():
    assert common.parse_tools_arg(True, "off")[0] is False
    assert common.parse_tools_arg(False, "ON")[0] is True
    assert common.parse_tools_arg(True, "")[0] is True
    assert "usage" in common.parse_tools_arg(True, "x")[1]
    assert common.canonicalize("tools") == "tools"


def test_reporter_drain_awaits_updates_before_reply():
    events = []

    async def send(text):
        await asyncio.sleep(0.01)
        events.append(("send", text))
        return "h"

    async def edit(handle, text):
        events.append(("edit", text))

    async def turn():
        rep = ToolStatusReporter(asyncio.get_running_loop(), send, edit)
        await asyncio.get_running_loop().run_in_executor(None, lambda: rep("roll_die", {}, "{}"))
        await rep.drain()
        events.append(("reply", ""))

    asyncio.run(turn())
    assert [e[0] for e in events] == ["send", "reply"]
