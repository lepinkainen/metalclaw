"""Telegram 4096-char splitting (issue #29)."""

import asyncio

from frontends import common
from frontends.telegram import _split_for_telegram, _tg_send_chunks


def test_split_message_shared_with_discord():
    text = "para one\n\n" + "x" * 50 + "\n\npara three"
    chunks = common.split_message(text, 60)
    assert all(len(c) <= 60 for c in chunks)
    assert "".join(c.replace("\n", "") for c in chunks).replace(
        " ", ""
    ) == text.replace("\n", "").replace(" ", "")


def test_short_text_is_single_pair():
    pairs = _split_for_telegram("hello **world**")
    assert len(pairs) == 1
    assert pairs[0][0] == "hello **world**"
    assert "<b>world</b>" in pairs[0][1]


def test_long_text_split_and_html_fits_limit():
    text = "\n\n".join(f"paragraph {i} " + "word " * 40 for i in range(60))
    assert len(text) > 4096
    pairs = _split_for_telegram(text)
    assert len(pairs) > 1
    for plain, html in pairs:
        assert len(plain) <= 4096
        assert len(html) <= 4096


def test_html_expansion_triggers_resplit():
    # Each '<' becomes '&lt;' (4 chars): plain fits, HTML does not.
    text = ("<" * 100 + "\n") * 35  # ~3535 plain, ~14000 html
    assert len(text) <= 4096
    pairs = _split_for_telegram(text)
    assert len(pairs) > 1
    for plain, html in pairs:
        assert len(plain) <= 4096
        assert len(html) <= 4096


def test_code_fence_reopened_across_chunks():
    code = "```python\n" + "\n".join(f"line {i}" for i in range(900)) + "\n```"
    pairs = _split_for_telegram(code)
    assert len(pairs) > 1
    for plain, _ in pairs:
        assert plain.startswith("```python")
        assert plain.rstrip().endswith("```")


def test_send_chunks_sends_every_chunk_with_html_then_plain_fallback():
    sent: list[tuple[str, dict]] = []

    async def send(body, **kw):
        if kw.get("parse_mode") == "HTML" and len(sent) == 0:
            sent.append((body, kw))
            raise RuntimeError("Bad Request: can't parse entities")
        sent.append((body, kw))

    text = "\n\n".join("word " * 300 for _ in range(5))
    asyncio.run(_tg_send_chunks(send, text, what="reply"))

    n_chunks = len(_split_for_telegram(text))
    # first chunk: HTML attempt + plain fallback; others: HTML only
    assert len(sent) == n_chunks + 1
    assert sent[0][1] == {"parse_mode": "HTML"}
    assert sent[1][1] == {}
    assert all(len(b) <= 4096 for b, _ in sent)


def test_split_message_keeps_closing_fence_intact_at_limit():
    code = "```python\n" + "\n".join(f"line {i}" for i in range(400)) + "\n```"
    chunks = common.split_message(code, 500)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c) <= 500
        assert c.startswith("```python\n")
        assert c.endswith("\n```")
