"""Rendering briefings, which are model output, into the web UI.

The model writes the briefing after reading support tickets and CRM fields
that anyone could have written, so the rendered page must not act on what it
says. Two rules:

  - No images. A markdown image is fetched by the browser as soon as the page
    opens, so an image URL is a way to send data out without anyone clicking.
    Images are shown as plain text instead.
  - Links only to systems we know (the ticket tracker). Any other link is
    shown as text, with its address visible, so a reviewer can see it but
    not follow it by accident.

Raw HTML in the markdown is escaped, as before.
"""

from __future__ import annotations

from html import escape

from markdown_it import MarkdownIt

from mcp_servers.tickets.backends import normalise_repo
from rro.seeding import MOCK_TICKETS_REPO_URL
from rro.settings import Settings


def allowed_link_prefixes(settings: Settings) -> list[str]:
    """Where briefing links may point: the ticket tracker in use."""
    repo = settings.value("GITHUB_TICKETS_REPO")
    if settings.backend_for("tickets") == "live" and repo:
        return [f"https://github.com/{normalise_repo(repo)}/"]
    return [f"{MOCK_TICKETS_REPO_URL}/"]


def briefing_renderer(allowed_prefixes: list[str]) -> MarkdownIt:
    # With the image rule off, "![alt](url)" parses as "!" plus a link, which the link rule below neutralises.
    md = MarkdownIt("commonmark", {"html": False}).enable("table").disable("image")

    def link_open(self, tokens, idx, options, env):
        href = tokens[idx].attrGet("href") or ""
        if any(href.startswith(prefix) for prefix in allowed_prefixes):
            return self.renderToken(tokens, idx, options, env)
        # Links can't nest, so the next link_close belongs to this link.
        close = next(t for t in tokens[idx + 1 :] if t.type == "link_close")
        close.meta["blocked_href"] = href
        return ""

    def link_close(self, tokens, idx, options, env):
        href = tokens[idx].meta.get("blocked_href")
        if href is None:
            return self.renderToken(tokens, idx, options, env)
        return f' <span class="blocked-link">(link not followed: {escape(href)})</span>'

    md.add_render_rule("link_open", link_open)
    md.add_render_rule("link_close", link_close)
    return md
