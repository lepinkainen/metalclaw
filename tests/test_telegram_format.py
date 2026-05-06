from telegram_format import to_html


def test_empty():
    assert to_html("") == ""


def test_plain_text():
    assert to_html("hello") == "hello"


def test_bold_double_asterisk():
    assert to_html("**hi**") == "<b>hi</b>"


def test_italic_single_asterisk():
    assert to_html("*hi*") == "<i>hi</i>"


def test_bold_italic_inline():
    out = to_html("This is **bold** and *italic*.")
    assert out == "This is <b>bold</b> and <i>italic</i>."


def test_strikethrough():
    assert to_html("~~gone~~") == "<s>gone</s>"


def test_inline_code():
    assert to_html("use `foo()`") == "use <code>foo()</code>"


def test_inline_code_escapes_html():
    assert to_html("`<div>`") == "<code>&lt;div&gt;</code>"


def test_link():
    out = to_html("[click](https://example.com)")
    assert out == '<a href="https://example.com">click</a>'


def test_link_quote_in_href_safe():
    out = to_html('[x](https://e.com/?q="hi")')
    # markdown-it percent-encodes the quote, which is also safe for HTML attrs.
    assert '"hi"' not in out
    assert out.startswith('<a href="') and out.endswith('">x</a>')


def test_text_escapes_html_specials():
    assert to_html("a < b & c > d") == "a &lt; b &amp; c &gt; d"


def test_bullet_list():
    md = "- one\n- two\n- three"
    assert to_html(md) == "• one\n• two\n• three"


def test_nested_bullet_list():
    md = "- outer\n  - inner\n- next"
    out = to_html(md)
    assert "• outer" in out
    assert "  • inner" in out
    assert "• next" in out


def test_ordered_list():
    md = "1. first\n2. second"
    assert to_html(md) == "1. first\n2. second"


def test_heading_becomes_bold():
    assert to_html("# Title") == "<b>Title</b>"


def test_heading_then_paragraph():
    out = to_html("# Title\n\nbody")
    assert out == "<b>Title</b>\n\nbody"


def test_fenced_code_no_lang():
    md = "```\nx = 1\n```"
    assert to_html(md) == "<pre>x = 1</pre>"


def test_fenced_code_with_lang():
    md = "```python\nx = 1\n```"
    assert to_html(md) == '<pre><code class="language-python">x = 1</code></pre>'


def test_fenced_code_escapes_html():
    md = "```\n<a>&\n```"
    assert to_html(md) == "<pre>&lt;a&gt;&amp;</pre>"


def test_blockquote():
    out = to_html("> quoted")
    assert out == "<blockquote>quoted</blockquote>"


def test_realistic_email_summary():
    md = (
        "**LinkedIn (3 unread emails):**\n"
        "* **Subject:** Riku, looking for a new job? (From: LinkedIn)\n"
        "* **Subject:** A private message from Vesa Laakso (From: LinkedIn)"
    )
    out = to_html(md)
    assert "<b>LinkedIn (3 unread emails):</b>" in out
    assert "• <b>Subject:</b> Riku, looking for a new job? (From: LinkedIn)" in out
    assert "**" not in out
