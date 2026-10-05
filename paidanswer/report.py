"""Render the resilience scorecard as one self-contained HTML page."""
from __future__ import annotations

import html
from typing import List

from .scenarios import ScenarioResult

CSS = """
:root{--paper:#F3F5F7;--panel:#fff;--ink:#0F1E26;--muted:#55656F;--line:#D7DEE3;--ok:#0B7F63;--ok-bg:rgba(14,143,111,.12);--bad:#A93A43;--bad-bg:rgba(179,66,75,.12);box-sizing:border-box}
@media(prefers-color-scheme:dark){:root{--paper:#0A1318;--panel:#101C23;--ink:#E4ECF0;--muted:#8DA0AB;--line:#21343E;--ok:#38D3A8;--ok-bg:rgba(56,211,168,.13);--bad:#EC7C84;--bad-bg:rgba(236,124,132,.14)}}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:32px 20px 56px}
h1{margin:0;font-size:clamp(1.9rem,5vw,2.8rem);line-height:1.08;letter-spacing:-.03em}
.lede{margin:10px 0 0;max-width:62ch;color:var(--muted)}
.score{display:flex;flex-wrap:wrap;gap:12px;margin:26px 0 8px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 18px;min-width:150px}
.stat b{display:block;font-size:1.9rem;line-height:1.1}.stat span{color:var(--muted);font-size:.9rem}
.stat.good b{color:var(--ok)}.stat.bad b{color:var(--bad)}
h2{margin:34px 0 10px;font-size:1.05rem;letter-spacing:-.01em}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin:0 0 12px}
.head{display:flex;gap:12px;align-items:baseline;justify-content:space-between}
.title{font-weight:600}.tag{font-size:.78rem;font-weight:700;border-radius:6px;padding:2px 8px;white-space:nowrap}
.tag.pass{color:var(--ok);background:var(--ok-bg)}.tag.fail{color:var(--bad);background:var(--bad-bg)}
ul{list-style:none;margin:10px 0 0;padding:0}li{padding:3px 0 3px 22px;position:relative;color:var(--muted);font-size:.94rem}
li::before{position:absolute;left:0;font-weight:700}li.ok::before{content:"\\2713";color:var(--ok)}li.no::before{content:"\\2717";color:var(--bad)}li.no{color:var(--bad)}
.note{margin-top:30px;color:var(--muted);font-size:.9rem;max-width:80ch}
"""


def render_html(results: List[ScenarioResult], title: str = "PaidAnswer resilience scorecard") -> str:
    total_checks = sum(len(r.checks) for r in results)
    ok_checks = sum(1 for r in results for c in r.checks if c.ok)
    failing = [r for r in results if not r.passed]
    verdict = "All guarantees hold" if not failing else f"{len(failing)} scenario(s) failing"
    groups: List[str] = []
    for r in results:
        if r.group not in groups:
            groups.append(r.group)
    parts = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
             f"<meta name='viewport' content='width=device-width,initial-scale=1'><title>{html.escape(title)}</title>"
             f"<style>{CSS}</style></head><body><div class='wrap'>",
             f"<h1>{html.escape(title)}</h1>",
             "<p class='lede'>Every scenario runs the real engine against simulated failures (model outages, flaky "
             "delivery, crashed workers, duplicate or forged webhooks) and checks one promise: a user who paid ends up "
             "with an answer or a refund, exactly once.</p>",
             "<div class='score'>",
             f"<div class='stat {'good' if not failing else 'bad'}'><b>{html.escape(verdict)}</b><span>result</span></div>",
             f"<div class='stat'><b>{len(results)}</b><span>scenarios</span></div>",
             f"<div class='stat'><b>{ok_checks}/{total_checks}</b><span>checks passed</span></div></div>"]
    for g in groups:
        parts.append(f"<h2>{html.escape(g)}</h2>")
        for r in (x for x in results if x.group == g):
            tag = "<span class='tag pass'>PASS</span>" if r.passed else "<span class='tag fail'>FAIL</span>"
            items = "".join(f"<li class='{'ok' if c.ok else 'no'}'>{html.escape(c.label)}</li>" for c in r.checks)
            parts.append(f"<div class='card'><div class='head'><span class='title'>{html.escape(r.title)}</span>{tag}"
                         f"</div><ul>{items}</ul></div>")
    parts.append("<p class='note'>Simulated with fake channels, payments and models and a fake clock; no network is "
                 "used. The live adapters (Telegram, WhatsApp, Razorpay, Gemini) are separate and must be smoke-tested "
                 "with real credentials before going live.</p></div></body></html>")
    return "".join(parts)
