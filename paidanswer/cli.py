"""Command line: `paidanswer demo | resilience | serve`."""
from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import List, Mapping, Optional, Tuple

from . import __version__
from .channels import TelegramChannel, WhatsAppChannel
from .engine import Engine, Worker
from .llm import AnthropicLLM, GeminiLLM, LLMChain
from .notify import LogNotifier, TelegramNotifier
from .payments import RazorpayPayments
from .report import render_html
from .scenarios import Harness, run_all
from .settings import Settings
from .speech import GeminiSTT, GoogleTTS
from .store import Store
from .testing import FakeChannel, FakeLLM, FakeNotifier
from .web import WebApp


# ---------------------------------------------------------------- resilience
def cmd_resilience(args) -> int:
    logging.disable(logging.CRITICAL)
    results = run_all()
    logging.disable(logging.NOTSET)
    width = max(len(r.title) for r in results)
    group = None
    for r in results:
        if r.group != group:
            group = r.group
            print(f"\n{group}")
        print(f"  {'PASS' if r.passed else 'FAIL'}  {r.title.ljust(width)}  {sum(c.ok for c in r.checks)}/{len(r.checks)}")
        for c in r.checks:
            if not c.ok:
                print(f"        x {c.label}")
    failing = [r for r in results if not r.passed]
    total = sum(len(r.checks) for r in results)
    ok = sum(1 for r in results for c in r.checks if c.ok)
    print(f"\n{len(results)} scenarios, {ok}/{total} checks passed" + ("" if not failing else f", {len(failing)} FAILING"))
    if args.html:
        with open(args.html, "w", encoding="utf-8") as f:
            f.write(render_html(results))
        print(f"report written to {args.html}")
    return 1 if failing else 0


# ---------------------------------------------------------------------- demo
class _TimedNotifier(FakeNotifier):
    def __init__(self, clock) -> None:
        super().__init__()
        self.clock, self.log = clock, []

    def alert(self, text: str) -> None:
        super().alert(text)
        self.log.append((self.clock.now, "ADMIN", text))


class _TimedChannel(FakeChannel):
    def __init__(self, clock) -> None:
        super().__init__()
        self.clock, self.log = clock, []

    def send_text(self, chat_id: str, text: str) -> None:
        super().send_text(chat_id, text)
        self.log.append((self.clock.now, "BOT ", text))


def _story(title: str, h: Harness, steps) -> None:
    print(f"\n=== {title} ===")
    t0 = h.clock.now
    seen = 0

    def flush() -> None:
        nonlocal seen
        events = sorted(h.channel.log + h.notifier.log, key=lambda e: e[0])
        for t, who, text in events[seen:]:
            print(f"  t+{int(t - t0):>4}s  {who:<5} {text}")
        seen = len(events)

    for kind, arg in steps:
        if kind == "ask":
            print(f"  t+{int(h.clock.now - t0):>4}s  USER  {arg}")
            h.order_ref = h.ask(arg)
        elif kind == "pay":
            print(f"  t+{int(h.clock.now - t0):>4}s  USER  (pays the link)")
            h.pay(h.order_ref)
        elif kind == "run":
            h.run(arg)
        flush()
    print(f"  -> final state: {h.order(h.order_ref.id).status}")


def _demo_harness(llm) -> Harness:
    h = Harness(llm=llm)
    h.channel = _TimedChannel(h.clock)
    h.notifier = _TimedNotifier(h.clock)
    h.engine.channels = {"fake": h.channel}
    h.engine.notifier = h.notifier
    return h


def cmd_demo(args) -> int:
    logging.disable(logging.CRITICAL)
    h = _demo_harness(FakeLLM(["fail", "ok"]))
    _story("The model fails once, then the user still gets the answer", h,
           [("ask", "What will my week look like?"), ("pay", None), ("run", 30)])
    h = _demo_harness(FakeLLM(default="fail"))
    _story("Every model is down: the user is refunded automatically", h,
           [("ask", "What will my week look like?"), ("pay", None), ("run", 700)])
    print("\nRun `python -m paidanswer resilience` to check all 21 failure scenarios.")
    return 0


# --------------------------------------------------------------------- serve
def build_live(env: Mapping[str, str]) -> Tuple[WebApp, Worker, Store]:
    missing: List[str] = []

    def need(*names: str) -> None:
        missing.extend(n for n in names if not env.get(n))

    need("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "RAZORPAY_WEBHOOK_SECRET")
    settings = Settings.from_env(env)
    channels = {}
    telegram: Optional[TelegramChannel] = None
    if env.get("TELEGRAM_BOT_TOKEN"):
        need("TELEGRAM_WEBHOOK_SECRET")
        telegram = TelegramChannel(env["TELEGRAM_BOT_TOKEN"])
        channels["telegram"] = telegram
    if env.get("WHATSAPP_TOKEN"):
        need("WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_API_VERSION", "WHATSAPP_VERIFY_TOKEN", "WHATSAPP_APP_SECRET")
        if not missing:
            channels["whatsapp"] = WhatsAppChannel(env["WHATSAPP_TOKEN"], env["WHATSAPP_PHONE_NUMBER_ID"],
                                                   env["WHATSAPP_API_VERSION"])
    if not channels:
        missing.append("TELEGRAM_BOT_TOKEN or WHATSAPP_TOKEN (at least one channel)")
    providers = []
    if env.get("GEMINI_API_KEY"):
        need("GEMINI_MODEL")
        if env.get("GEMINI_MODEL"):
            providers.append(GeminiLLM(env["GEMINI_API_KEY"], env["GEMINI_MODEL"]))
    if env.get("ANTHROPIC_API_KEY"):
        need("ANTHROPIC_MODEL")
        if env.get("ANTHROPIC_MODEL"):
            providers.append(AnthropicLLM(env["ANTHROPIC_API_KEY"], env["ANTHROPIC_MODEL"]))
    if not providers:
        missing.append("GEMINI_API_KEY + GEMINI_MODEL, or ANTHROPIC_API_KEY + ANTHROPIC_MODEL (at least one model)")
    if missing:
        raise SystemExit("Missing configuration:\n  - " + "\n  - ".join(dict.fromkeys(missing)) +
                         "\nSee .env.example.")
    store = Store(env.get("PAIDANSWER_DB", "paidanswer.db"))   # created only after the configuration is valid
    stt = GeminiSTT(env["GEMINI_API_KEY"], env["GEMINI_MODEL"]) if env.get("GEMINI_API_KEY") else None
    tts = GoogleTTS(env["GOOGLE_TTS_API_KEY"], env.get("TTS_LANGUAGE", "en-US")) if env.get("GOOGLE_TTS_API_KEY") else None
    notifier = LogNotifier()
    if env.get("ADMIN_TELEGRAM_CHAT_ID") and telegram is not None:
        notifier = TelegramNotifier(telegram, env["ADMIN_TELEGRAM_CHAT_ID"])
    engine = Engine(settings, store, channels, RazorpayPayments(env["RAZORPAY_KEY_ID"], env["RAZORPAY_KEY_SECRET"]),
                    LLMChain(providers), tts=tts, stt=stt, notifier=notifier)
    app = WebApp(engine, store, env["RAZORPAY_WEBHOOK_SECRET"], telegram_secret=env.get("TELEGRAM_WEBHOOK_SECRET"),
                 whatsapp_verify_token=env.get("WHATSAPP_VERIFY_TOKEN"), whatsapp_app_secret=env.get("WHATSAPP_APP_SECRET"),
                 admin_token=env.get("ADMIN_TOKEN"))
    return app, Worker(engine), store


def load_env_file(path: str) -> None:
    """Load KEY=VALUE lines into the environment (existing variables win). Lines starting with # are ignored."""
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def cmd_serve(args) -> int:
    from socketserver import ThreadingMixIn
    from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

    class Threaded(ThreadingMixIn, WSGIServer):
        daemon_threads = True

    class Quiet(WSGIRequestHandler):
        def log_message(self, *a):  # keep payloads out of the console
            pass

    if args.env_file:
        load_env_file(args.env_file)
    app, worker, _ = build_live(os.environ)
    worker.start()
    server = make_server(args.host, args.port, app, server_class=Threaded, handler_class=Quiet)
    print(f"PaidAnswer {__version__} listening on http://{args.host}:{args.port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        worker.stop()
        server.server_close()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="paidanswer", description="Pay-per-answer AI chatbot core with refund guarantees.")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("demo", help="narrated demo with fake services (no keys needed)").set_defaults(fn=cmd_demo)
    r = sub.add_parser("resilience", help="run the failure scenarios and print the scorecard")
    r.add_argument("--html", help="also write the scorecard as an HTML file")
    r.set_defaults(fn=cmd_resilience)
    s = sub.add_parser("serve", help="run the webhook server (needs the environment variables in .env.example)")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--env-file", help="load settings from a .env file (KEY=VALUE per line)")
    s.set_defaults(fn=cmd_serve)
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
