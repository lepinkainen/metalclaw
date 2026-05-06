"""CommonMark to Telegram-flavoured HTML.

Telegram's HTML parse mode supports a small set of inline tags: <b>, <i>, <u>,
<s>, <code>, <pre>, <a>, <blockquote>, <tg-spoiler>. Block constructs like
lists and headings are not supported, so we emit them as plain text with
bullet markers and bold headings. See https://core.telegram.org/bots/api#html-style.
"""

from __future__ import annotations

import html
import re

from markdown_it import MarkdownIt

_md = MarkdownIt("gfm-like", {"breaks": False, "linkify": False, "html": False})


def _escape(text: str) -> str:
    return html.escape(text, quote=False)


def _attr(text: str) -> str:
    return html.escape(text, quote=True)


def _render_inline(token) -> str:
    parts: list[str] = []
    for child in token.children or []:
        t = child.type
        if t == "text":
            parts.append(_escape(child.content))
        elif t in ("softbreak", "hardbreak"):
            parts.append("\n")
        elif t == "strong_open":
            parts.append("<b>")
        elif t == "strong_close":
            parts.append("</b>")
        elif t == "em_open":
            parts.append("<i>")
        elif t == "em_close":
            parts.append("</i>")
        elif t == "s_open":
            parts.append("<s>")
        elif t == "s_close":
            parts.append("</s>")
        elif t == "code_inline":
            parts.append(f"<code>{_escape(child.content)}</code>")
        elif t == "link_open":
            href = child.attrGet("href") or ""
            parts.append(f'<a href="{_attr(href)}">')
        elif t == "link_close":
            parts.append("</a>")
        elif t == "image":
            alt = child.content or "image"
            src = child.attrGet("src") or ""
            if src:
                parts.append(f'<a href="{_attr(src)}">{_escape(alt)}</a>')
            else:
                parts.append(_escape(alt))
        elif t == "html_inline":
            parts.append(_escape(child.content))
    return "".join(parts)


def to_html(text: str) -> str:
    """Convert CommonMark to Telegram HTML."""
    if not text:
        return ""

    tokens = _md.parse(text)
    out: list[str] = []
    list_stack: list[list] = []  # each entry: ["ul"|"ol", counter]

    for tok in tokens:
        t = tok.type

        if t == "paragraph_open":
            continue
        if t == "paragraph_close":
            out.append("\n" if list_stack else "\n\n")
            continue
        if t == "inline":
            out.append(_render_inline(tok))
            continue
        if t == "heading_open":
            out.append("<b>")
            continue
        if t == "heading_close":
            out.append("</b>\n\n")
            continue
        if t == "bullet_list_open":
            list_stack.append(["ul", 0])
            continue
        if t == "ordered_list_open":
            start = int(tok.attrGet("start") or 1)
            list_stack.append(["ol", start - 1])
            continue
        if t in ("bullet_list_close", "ordered_list_close"):
            list_stack.pop()
            if not list_stack:
                out.append("\n")
            continue
        if t == "list_item_open":
            depth = len(list_stack) - 1
            indent = "  " * depth
            kind, count = list_stack[-1]
            if kind == "ul":
                marker = "• "
            else:
                count += 1
                list_stack[-1][1] = count
                marker = f"{count}. "
            out.append(f"{indent}{marker}")
            continue
        if t == "list_item_close":
            continue
        if t in ("fence", "code_block"):
            info = (tok.info or "").strip().split()[0] if tok.info else ""
            content = _escape(tok.content.rstrip("\n"))
            if info:
                out.append(
                    f'<pre><code class="language-{_attr(info)}">{content}</code></pre>\n\n'
                )
            else:
                out.append(f"<pre>{content}</pre>\n\n")
            continue
        if t == "blockquote_open":
            out.append("<blockquote>")
            continue
        if t == "blockquote_close":
            # trim trailing newlines that paragraph_close added inside the quote
            while out and out[-1] in ("\n", "\n\n"):
                out.pop()
            out.append("</blockquote>\n\n")
            continue
        if t == "hr":
            out.append("———\n\n")
            continue
        if t == "html_block":
            out.append(_escape(tok.content))
            continue

    result = "".join(out)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()
