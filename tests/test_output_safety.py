"""Defences against untrusted text travelling out through the app's own output.

The agent reads text that anyone could have written (support tickets, CRM
fields). Whatever it writes back (briefings, proposal reasons) must be inert
wherever it's shown: nothing fetched, nothing clickable to an unknown host,
no Slack mentions.
"""

import json

from rro.web.briefing import briefing_renderer
from test_approvals import RISK_PROPS, propose
from test_slack import slack_setup

TICKETS = "https://github.com/example-org/support-tickets/"


def render(markdown: str) -> str:
    return briefing_renderer([TICKETS]).render(markdown)


def test_briefing_images_are_never_fetched():
    html = render("Summary ![chart](https://images.example/p.png?arr=186000)")
    assert "<img" not in html
    assert 'href="https://images.example' not in html
    assert "link not followed: https://images.example/p.png?arr=186000" in html


def test_briefing_links_only_to_the_ticket_tracker():
    html = render(f"See [#101]({TICKETS}issues/101) and [the dashboard](https://elsewhere.example/?d=1).")
    assert f'<a href="{TICKETS}issues/101">#101</a>' in html
    assert 'href="https://elsewhere.example' not in html
    assert "the dashboard <span" in html  # the text stays readable, the address is shown, nothing is clickable


def test_briefing_raw_html_is_escaped():
    html = render('<img src="https://images.example/x.png"> <script>alert(1)</script>')
    assert "<img" not in html and "<script" not in html


async def test_slack_cards_make_model_text_inert(rt):
    # The reason is model-written: it must not ping the channel or hide a link behind friendly text.
    reason = "Usage down <!channel> see <https://elsewhere.example/?d=1|the dashboard>"
    approval = propose(rt, properties={**RISK_PROPS, "renewal_risk_reason": reason})
    fake, notifier, _ = slack_setup(rt)
    await notifier.post_approval(approval)

    (method, kwargs), = fake.calls
    assert method == "chat_postMessage"
    assert kwargs["unfurl_links"] is False and kwargs["unfurl_media"] is False
    card = json.dumps(kwargs["blocks"])
    assert "<!channel>" not in card and "<https://elsewhere.example" not in card
    assert "&lt;!channel&gt;" in card  # still readable by the approver, just inert


def test_run_page_renders_briefings_safely(rt, monkeypatch):
    from fastapi.testclient import TestClient

    import rro.web.app as web

    path = rt.settings.output_dir / "briefings" / "halcyon.md"
    path.parent.mkdir(parents=True)
    path.write_text("# Briefing\n\n![x](https://images.example/p.png?arr=186000)", encoding="utf-8")
    run_id = rt.store.create_run("Prep the renewal for Halcyon Robotics", "scripted")
    rt.store.finish_run(run_id, status="completed", account_slug="halcyon-robotics", risk_score=90, risk_band="critical",
                        briefing_path=str(path), summary=None, error=None, usage={})  # fmt: skip
    monkeypatch.setattr(web, "get_settings", lambda: rt.settings)
    with TestClient(web.app) as client:
        page = client.get(f"/runs/{run_id}").text
    assert "<h1>Briefing</h1>" in page and 'src="https://images.example' not in page
