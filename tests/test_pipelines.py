"""Watcher pipelines: shared ledger, mail pipeline stages, heartbeat dispatch."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import channels
import config
import heartbeat
import history
import pipelines
from pipelines import mail as mail_pipeline
from pipelines.context import Ledger, Outcome, PipelineContext, Trace
from tools import mail

# --- history: reported_items + pipeline_runs ---


def test_reported_ledger_round_trip():
    assert history.reported_ids("mail", ["a", "b"]) == set()
    assert history.reported_mark("mail", ["a", "a", ""], "delivered") == 1
    history.reported_mark("mail", ["a"], "dismissed")  # first outcome wins
    assert history.reported_ids("mail", ["a", "b"]) == {"a"}
    assert history.reported_ids("wilma", ["a"]) == set()  # namespaced by source
    assert history.reported_count("mail") == 1


def test_pipeline_run_record_and_latest():
    history.pipeline_run_record(
        "r1",
        "mail",
        "p",
        "2026-01-01T00:00:00",
        "2026-01-01T00:00:01",
        "suppressed",
        None,
        [],
    )
    history.pipeline_run_record(
        "r2",
        "mail",
        "p",
        "2026-01-02T00:00:00",
        "2026-01-02T00:00:01",
        "failed",
        "boom",
        [{"name": "fetch", "ms": 3}],
    )
    latest = history.pipeline_runs_latest()
    assert [r[0] for r in latest] == ["r2"]
    assert latest[0][6] == "boom"
    assert [r[0] for r in history.pipeline_runs_recent("mail")] == ["r2", "r1"]


# --- tools.mail: legacy ledger migration, head fetch, label filter ---


def test_legacy_mail_ledger_imported_once(tmp_path, monkeypatch):
    data = tmp_path / "xdg-data" / "metalclaw"
    data.mkdir(parents=True)
    legacy = data / "mail_reported.sqlite3"
    with sqlite3.connect(legacy) as db:
        db.execute(
            "CREATE TABLE reported_messages(message_id TEXT PRIMARY KEY, first_reported_at TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO reported_messages VALUES ('old1', '2025-01-01T00:00:00')"
        )
    monkeypatch.setattr(mail, "_legacy_ledger_checked", False)
    assert mail.migrate_legacy_ledger() == 1
    assert not legacy.exists()
    assert legacy.with_suffix(".sqlite3.bak").exists()
    assert history.reported_ids("mail", ["old1"]) == {"old1"}
    assert mail.migrate_legacy_ledger() == 0


MBS = {
    "by_role": {"inbox": {"id": "mb-in"}, "trash": {"id": "mb-trash"}},
    "by_name": {
        "inbox": {"id": "mb-in", "name": "Inbox"},
        "newsletters": {"id": "mb-news", "name": "Newsletters"},
    },
    "by_path": {
        "inbox": {"id": "mb-in"},
        "newsletters": {"id": "mb-news"},
        "bot/receipts": {"id": "mb-rcpt"},
    },
    "by_id": {
        "mb-in": {"name": "Inbox"},
        "mb-news": {"name": "Newsletters"},
        "mb-rcpt": {"name": "Receipts"},
    },
}


@pytest.fixture
def fm(monkeypatch):
    monkeypatch.setattr(
        mail, "_fm_session", lambda: {"api_url": "u", "account_id": "a", "token": "t"}
    )
    monkeypatch.setattr(mail, "_fm_mailboxes", lambda: MBS)
    monkeypatch.setattr(mail, "_legacy_ledger_checked", True)


def _head(i, box="mb-in"):
    return {
        "id": i,
        "receivedAt": f"2026-01-01T00:00:{int(i) % 60:02d}Z",
        "mailboxIds": {box: True},
    }


def test_fetch_unread_heads_pages_until_short_page(fm, monkeypatch):
    monkeypatch.setattr(mail, "_HEADS_PAGE", 2)
    calls = []

    def post(method_calls):
        pos = method_calls[0][1]["position"]
        calls.append(pos)
        ids = ["1", "2", "3"][pos : pos + 2]
        return {"q": {"ids": ids}, "g": {"list": [_head(i) for i in ids]}}

    monkeypatch.setattr(mail, "_jmap_post", post)
    heads = mail.fetch_unread_heads()
    assert [h["id"] for h in heads] == ["1", "2", "3"]
    assert calls == [0, 2]
    assert heads[0]["mailbox_ids"] == ["mb-in"]


def test_fetch_unread_heads_filter_shape(fm, monkeypatch):
    seen = {}

    def post(method_calls):
        seen["q"] = method_calls[0][1]
        return {"q": {"ids": []}, "g": {"list": []}}

    monkeypatch.setattr(mail, "_jmap_post", post)
    mail.fetch_unread_heads()
    q = seen["q"]
    assert q["sort"] == [{"property": "receivedAt", "isAscending": True}]
    assert {"inMailboxOtherThan": ["mb-trash"]} in q["filter"]["conditions"]
    assert {"notKeyword": "$seen"} in q["filter"]["conditions"]


def test_drop_labelled_by_name_or_path_and_unknown_ignored(fm):
    heads = [
        {"id": "1", "mailbox_ids": ["mb-in"]},
        {"id": "2", "mailbox_ids": ["mb-in", "mb-news"]},
        {"id": "3", "mailbox_ids": ["mb-rcpt"]},
    ]
    kept, dropped = mail.drop_labelled(heads, ["Newsletters", "bot/Receipts", "Nope"])
    assert [h["id"] for h in kept] == ["1"]
    assert dropped == {"Newsletters": 1, "bot/Receipts": 1}
    assert mail.drop_labelled(heads, []) == (heads, {})


# --- pipelines.mail: outcomes and ordering ---


def _ctx(
    monkeypatch,
    *,
    reply="HEARTBEAT_OK",
    deliver=None,
    max_batch=50,
    labels=(),
    instructions="",
):
    cfg = SimpleNamespace(
        mail_ignore_labels=tuple(labels),
        mail_pipeline_max_batch=max_batch,
        mail_pipeline_instructions=instructions,
    )
    calls = {"asked": [], "delivered": []}

    async def ask_model(*, system, user, tools):
        calls["asked"].append({"system": system, "user": user, "tools": tools})
        return reply

    async def _deliver(text):
        calls["delivered"].append(text)
        if deliver is not None:
            await deliver(text)

    ctx = PipelineContext(
        config=cfg,  # type: ignore[arg-type]
        now=datetime.now(UTC),
        channel="telegram-1",
        trace=Trace(),
        ledger=Ledger(),
        ask_model=ask_model,
        deliver=_deliver,
    )
    monkeypatch.setattr(mail, "migrate_legacy_ledger", lambda: 0)
    monkeypatch.setattr(mail, "attach_handler_hints", lambda emails: emails)
    return ctx, calls


def _stub_mail(monkeypatch, heads, labels_drop=()):
    monkeypatch.setattr(mail, "fetch_unread_heads", lambda: list(heads))
    monkeypatch.setattr(
        mail,
        "drop_labelled",
        lambda hs, names: ([h for h in hs if h["id"] not in labels_drop], {}),
    )
    monkeypatch.setattr(
        mail,
        "fetch_bodies",
        lambda ids: [
            {
                "id": i,
                "subject": f"s{i}",
                "from": "a@x",
                "received_at": "",
                "preview": "p",
                "folders": [],
            }
            for i in ids
        ],
    )


def test_pipeline_suppressed_when_nothing_survives(monkeypatch):
    ctx, calls = _ctx(monkeypatch)
    _stub_mail(monkeypatch, [{"id": "1", "mailbox_ids": []}], labels_drop={"1"})
    assert asyncio.run(mail_pipeline.run(ctx)) == Outcome.SUPPRESSED
    assert calls["asked"] == []
    assert [s.name for s in ctx.trace.steps] == ["fetch", "labels", "ledger"]


def test_pipeline_dismissed_marks_every_id(monkeypatch):
    ctx, calls = _ctx(monkeypatch, reply="Nothing new here. HEARTBEAT_OK")
    _stub_mail(
        monkeypatch, [{"id": "1", "mailbox_ids": []}, {"id": "2", "mailbox_ids": []}]
    )
    assert asyncio.run(mail_pipeline.run(ctx)) == Outcome.DISMISSED
    assert calls["delivered"] == []
    assert history.reported_ids("mail", ["1", "2"]) == {"1", "2"}
    # next tick: the ledger stage drops them → suppressed, no model call
    ctx2, calls2 = _ctx(monkeypatch)
    assert asyncio.run(mail_pipeline.run(ctx2)) == Outcome.SUPPRESSED
    assert calls2["asked"] == []


def test_pipeline_delivered_marks_batch_and_narrow_tools(monkeypatch):
    ctx, calls = _ctx(monkeypatch, reply="Plumber replied, see email 2.")
    _stub_mail(
        monkeypatch, [{"id": "1", "mailbox_ids": []}, {"id": "2", "mailbox_ids": []}]
    )
    assert asyncio.run(mail_pipeline.run(ctx)) == Outcome.DELIVERED
    assert calls["delivered"] == ["Plumber replied, see email 2."]
    assert calls["asked"][0]["tools"] == mail_pipeline.TOOLS
    assert "mark_emails_reported" not in calls["asked"][0]["tools"]
    assert '"id": "1"' in calls["asked"][0]["user"]
    assert history.reported_ids("mail", ["1", "2"]) == {"1", "2"}


def test_pipeline_cap_applies_after_filters_so_backlog_cannot_starve(monkeypatch):
    # 200 unread: the oldest 150 are labelled away or already reported; the
    # model must still get the newest-surviving 50, not an empty batch.
    heads = [{"id": str(i), "mailbox_ids": []} for i in range(200)]
    history.reported_mark("mail", [str(i) for i in range(100, 150)], "delivered")
    ctx, calls = _ctx(monkeypatch, reply="summary", max_batch=50)
    _stub_mail(monkeypatch, heads, labels_drop={str(i) for i in range(100)})
    assert asyncio.run(mail_pipeline.run(ctx)) == Outcome.DELIVERED
    user = calls["asked"][0]["user"]
    assert user.startswith("50 unread")
    assert '"id": "150"' in user and '"id": "199"' in user
    assert '"id": "99"' not in user
    bodies = next(s for s in ctx.trace.steps if s.name == "bodies")
    assert bodies.note == "50 fetched"


def test_pipeline_defers_overflow_to_next_tick(monkeypatch):
    heads = [{"id": str(i), "mailbox_ids": []} for i in range(5)]
    ctx, _calls = _ctx(monkeypatch, reply="summary", max_batch=2)
    _stub_mail(monkeypatch, heads)
    asyncio.run(mail_pipeline.run(ctx))
    assert history.reported_ids("mail", [str(i) for i in range(5)]) == {"0", "1"}
    bodies = next(s for s in ctx.trace.steps if s.name == "bodies")
    assert bodies.note == "2 fetched, 3 deferred"
    ctx2, calls2 = _ctx(monkeypatch, reply="summary", max_batch=2)
    asyncio.run(mail_pipeline.run(ctx2))
    assert '"id": "2"' in calls2["asked"][0]["user"]


def test_pipeline_failed_delivery_leaves_ids_unreported(monkeypatch):
    async def boom(text):
        raise RuntimeError("telegram down")

    ctx, _calls = _ctx(monkeypatch, reply="summary", deliver=boom)
    _stub_mail(monkeypatch, [{"id": "1", "mailbox_ids": []}])
    with pytest.raises(RuntimeError, match="telegram down"):
        asyncio.run(mail_pipeline.run(ctx))
    assert history.reported_ids("mail", ["1"]) == set()
    assert [s.name for s in ctx.trace.steps][-1] == "deliver"


def test_pipeline_restart_between_deliver_and_mark_redelivers_then_marks(monkeypatch):
    # Delivery succeeded but the process died before ledger.mark: the next
    # run re-delivers (at-least-once, duplicate accepted) and then marks.
    ctx, calls = _ctx(monkeypatch, reply="summary")
    _stub_mail(monkeypatch, [{"id": "1", "mailbox_ids": []}])

    def die(source, items, outcome):
        raise KeyboardInterrupt  # simulate the process going away mid-mark

    monkeypatch.setattr(ctx.ledger, "mark", die)
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(mail_pipeline.run(ctx))
    assert calls["delivered"] == ["summary"]
    assert history.reported_ids("mail", ["1"]) == set()

    ctx2, calls2 = _ctx(monkeypatch, reply="summary")
    assert asyncio.run(mail_pipeline.run(ctx2)) == Outcome.DELIVERED
    assert calls2["delivered"] == ["summary"]
    assert history.reported_ids("mail", ["1"]) == {"1"}


def test_pipeline_empty_reply_is_a_failure(monkeypatch):
    ctx, _calls = _ctx(monkeypatch, reply="")
    _stub_mail(monkeypatch, [{"id": "1", "mailbox_ids": []}])
    with pytest.raises(RuntimeError, match="empty reply"):
        asyncio.run(mail_pipeline.run(ctx))
    assert history.reported_ids("mail", ["1"]) == set()


# --- heartbeat: strip_sentinel, config-seeded actions, dispatch, trace row ---


@pytest.mark.parametrize(
    "reply,expected",
    [
        ("HEARTBEAT_OK", ("", True)),
        ("Nothing to report. HEARTBEAT_OK", ("", True)),
        ("ALERT: x", ("ALERT: x", False)),
        ("", ("", False)),
    ],
)
def test_strip_sentinel(reply, expected):
    assert heartbeat.strip_sentinel(reply) == expected


def test_strip_sentinel_keeps_long_alert_with_stray_sentinel():
    text, acked = heartbeat.strip_sentinel("x" * 400 + " HEARTBEAT_OK")
    assert acked and text == "x" * 400


@pytest.fixture
def hb_cfg(tmp_path, monkeypatch, clear_env, write_config):
    cfg_path = tmp_path / "config.yaml"
    write_config(
        cfg_path,
        vault_path=str(tmp_path / "vault"),
        memory_subdir="Mem",
        heartbeat_interval_seconds=60,
        heartbeat_default_channel="tg-1",
        heartbeat_pipelines=[
            {"pipeline": "mail", "every": "5m"},
            {"pipeline": "nope", "every": 60, "id": "custom"},
            {"pipeline": "broken"},  # neither every nor schedule → skipped
        ],
        mail_ignore_labels=["Newsletters"],
    )
    monkeypatch.setenv("METALCLAW_CONFIG", str(cfg_path))
    config.reset_cache()
    (tmp_path / "vault" / "Mem").mkdir(parents=True)
    yield config.get_config()
    config.reset_cache()
    channels.CHANNELS.clear()


def test_config_pipeline_actions(hb_cfg):
    acts = heartbeat.config_pipeline_actions()
    assert [(a.id, a.pipeline, a.kind, a.every) for a in acts] == [
        ("pipeline-mail", "mail", heartbeat.ActionKind.EVERY, 300),
        ("custom", "nope", heartbeat.ActionKind.EVERY, 60),
    ]
    assert hb_cfg.mail_ignore_labels == ("Newsletters",)
    assert "pipeline:mail" in heartbeat.summary() or heartbeat.summary() == ""


class _Chan:
    name = "tg"

    def __init__(self):
        self.sent = []

    async def notify(self, scope, text):
        self.sent.append((scope, text))

    def active_scopes(self):
        return ["tg-1"]


def test_run_tick_dispatches_pipeline_and_records_trace(hb_cfg, monkeypatch):
    chan = _Chan()
    channels.register(chan)

    async def fake_mail(ctx):
        with ctx.trace.step("fetch") as st:
            st.set_note("2 unread")
        await ctx.deliver("mail summary")
        return pipelines.Outcome.DELIVERED

    monkeypatch.setitem(pipelines.REGISTRY, "mail", fake_mail)
    with patch("chat_loop.chat") as chat:
        replies = asyncio.run(heartbeat.run_tick())
    chat.assert_not_called()
    assert chan.sent == [("tg-1", "mail summary")]
    assert replies["pipeline-mail"] == "delivered"
    # unknown pipeline name → failure + backoff, not a crash
    assert replies.get("custom") is None
    st = heartbeat.get_state("custom")
    assert st.failure_count == 1 and "unknown pipeline" in st.meta["last_error"]
    ok = heartbeat.get_state("pipeline-mail")
    assert ok.failure_count == 0 and ok.meta["last_outcome"] == "delivered"
    assert ok.meta["pipeline"] == "mail"
    runs = {r[1]: r for r in history.pipeline_runs_latest()}
    assert runs["mail"][5] == "delivered"
    assert '"note": "2 unread"' in runs["mail"][7]
    assert "nope" not in runs  # config error: fails before a run starts, no trace row


def test_run_tick_pipeline_exception_writes_failed_trace_and_backs_off(
    hb_cfg, monkeypatch
):
    channels.register(_Chan())

    async def crash(ctx):
        with ctx.trace.step("fetch"):
            raise ConnectionError("jmap down")

    monkeypatch.setitem(pipelines.REGISTRY, "mail", crash)
    t0 = datetime.now(UTC)
    asyncio.run(heartbeat.run_tick(now=t0))
    st = heartbeat.get_state("pipeline-mail")
    assert st.failure_count == 1
    row = history.pipeline_runs_recent("mail")[0]
    assert row[5] == "failed" and row[6] == "ConnectionError: jmap down"
    assert '"name": "fetch"' in row[7]
    # state rows for config pipelines survive the orphan prune
    assert heartbeat.get_state("pipeline-mail") is not None


def test_pipeline_action_round_trips_through_ledger_yaml(hb_cfg):
    raw = {
        "id": "x",
        "kind": "every",
        "every": 30,
        "channel": "tg-1",
        "pipeline": "mail",
    }
    action = heartbeat._action_from_dict(raw)
    assert action.pipeline == "mail" and action.prompt == ""
    assert action.to_dict()["pipeline"] == "mail"
    assert (
        "pipeline"
        not in heartbeat._action_from_dict({**raw, "pipeline": None}).to_dict()
    )


def test_pipeline_instructions_appended_to_system(monkeypatch):
    ctx, calls = _ctx(monkeypatch, instructions="Acme is urgent.")
    _stub_mail(monkeypatch, [{"id": "1", "mailbox_ids": []}])
    asyncio.run(mail_pipeline.run(ctx))
    assert calls["asked"][0]["system"] == (
        mail_pipeline.SYSTEM + "\n\nUser's mail rules:\nAcme is urgent."
    )


def test_pipeline_system_unchanged_without_instructions(monkeypatch):
    ctx, calls = _ctx(monkeypatch)
    _stub_mail(monkeypatch, [{"id": "1", "mailbox_ids": []}])
    asyncio.run(mail_pipeline.run(ctx))
    assert calls["asked"][0]["system"] == mail_pipeline.SYSTEM
