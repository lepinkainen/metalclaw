"""``/status`` and ``/usage`` report formatting, plus the WARNING+ ring buffer."""

import collections
import logging
import time
from datetime import UTC, datetime, timedelta

import channels
import heartbeat
import history
import mcp_client
from config import get_config

log = logging.getLogger(__name__)


# --- /status and /usage ---

_STARTED = time.monotonic()


class RecentWarnings(logging.Handler):
    """In-memory ring buffer of the last WARNING+ records, for /status."""

    def __init__(self, maxlen: int = 5) -> None:
        super().__init__(level=logging.WARNING)
        self.records: collections.deque[str] = collections.deque(maxlen=maxlen)

    def emit(self, record: logging.LogRecord) -> None:
        ts = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S")
        self.records.append(
            f"{ts} {record.levelname} {record.name}: {record.getMessage()}"
        )


recent_warnings = RecentWarnings()


def _fmt_td(td: timedelta) -> str:
    mins = int(td.total_seconds()) // 60
    d, rem = divmod(mins, 1440)
    h, m = divmod(rem, 60)
    return (f"{d}d " if d else "") + f"{h}h {m}m"


def format_status() -> str:
    cfg = get_config()
    model = cfg.model if cfg.provider == "ollama" else cfg.litellm_model
    lines = [
        f"provider: {cfg.provider} / {model}",
        (
            f"escalation: {cfg.escalation_provider} / {cfg.escalation_model}"
            if cfg.escalation_enabled
            else "escalation: disabled"
        ),
        f"uptime: {_fmt_td(timedelta(seconds=time.monotonic() - _STARTED))}",
    ]
    active = {
        name: sorted(ch.active_scopes()) for name, ch in channels.CHANNELS.items()
    }
    lines.append(
        "frontends: "
        + (
            "; ".join(f"{n} ({len(sc)} active)" for n, sc in sorted(active.items()))
            or "none registered"
        )
    )
    try:
        actions = heartbeat.list_active()
        state = heartbeat.load_state()
    except Exception as e:
        log.exception("/status: heartbeat read failed")
        actions, state = [], {}
        lines.append(f"heartbeat: ledger read failed: {e}")
    else:
        lines.append(
            f"heartbeat: enabled={cfg.heartbeat_enabled} "
            f"interval={cfg.heartbeat_interval_seconds}s actions={len(actions)}"
        )
    now = datetime.now(UTC)
    for a in actions:
        st = state.get(a.id)
        nxt = heartbeat.next_due(a, st.last_run if st else None, now)
        retry = st.retry_at() if st else None
        if nxt is not None and retry is not None and retry > nxt:
            nxt = retry
        when = "never" if nxt is None else nxt.astimezone().strftime("%Y-%m-%d %H:%M")
        suffix = ""
        if st is not None and st.failure_count > 0:
            plural = "s" if st.failure_count != 1 else ""
            suffix = f" ({st.failure_count} failure{plural}, backing off)"
        lines.append(f"  • {a.id} [{a.kind}] next: {when}{suffix} | {a.prompt[:50]}")
    try:
        pipeline_actions = heartbeat.config_pipeline_actions()
        latest_runs = {r[1]: r for r in history.pipeline_runs_latest()}
    except Exception as e:
        log.exception("/status: pipeline read failed")
        lines.append(f"pipelines: read failed: {e}")
        pipeline_actions, latest_runs = [], {}
    if pipeline_actions:
        lines.append(f"pipelines: {len(pipeline_actions)} configured")
        for a in pipeline_actions:
            st = state.get(a.id)
            nxt = heartbeat.next_due(a, st.last_run if st else None, now)
            retry = st.retry_at() if st else None
            if nxt is not None and retry is not None and retry > nxt:
                nxt = retry
            when = (
                "never" if nxt is None else nxt.astimezone().strftime("%Y-%m-%d %H:%M")
            )
            run = latest_runs.get(a.pipeline or "")
            last = "no runs yet"
            if run is not None:
                _, _, _, started, _, outcome, error, _ = run
                last = f"last: {outcome} {started[:16]}"
                if error:
                    last += f" ({error[:60]})"
            lines.append(f"  • {a.id} [{a.kind}] next: {when} | {last}")
    servers = mcp_client.status()
    if servers:
        lines.append(f"mcp: {len(servers)} configured")
        for m in servers:
            line = f"  • {m['name']} {m['status']} {m['transport']} {len(m['tools'])} tools"
            if m["tools"]:
                line += f" ({', '.join(m['tools'])})"
            if m["error"]:
                line += f" — {m['error'][:120]}"
            lines.append(line)
    recent = list(recent_warnings.records)
    lines.append(
        f"recent warnings/errors (last {len(recent)}):"
        if recent
        else "recent warnings/errors: none"
    )
    lines.extend(f"  {r[:200]}" for r in recent)
    return "\n".join(lines)


def _usage_block(title: str, since: datetime) -> list[str]:
    rows = history.usage_since(since.astimezone(UTC).isoformat())
    if not rows:
        return [f"{title}: no calls recorded"]
    out = [f"{title}:"]
    for provider, model, calls, prompt, completion, cost, known in rows:
        if known == 0:
            cost_s = "cost n/a"
        else:
            cost_s = f"${cost:.4f}" + ("" if known == calls else " (partial)")
        out.append(
            f"  • {provider}/{model}: {calls} call(s), {prompt} in / {completion} out, {cost_s}"
        )
    return out


def format_usage() -> str:
    now = datetime.now().astimezone()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return "\n".join(
        [
            *_usage_block("today", midnight),
            *_usage_block("last 30 days", now - timedelta(days=30)),
        ]
    )
