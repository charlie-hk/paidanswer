import logging
import unittest

from paidanswer.models import Inbound
from paidanswer.scenarios import Harness
from paidanswer.settings import Settings, money
from paidanswer.testing import ANSWER, FakeLLM, FakePayments


class EngineExtraTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def test_payment_link_failure_means_no_charge_and_no_order(self):
        h = Harness(payments=FakePayments(fail_links=1))
        h.engine.on_message(Inbound("fake", "u1", "A question", None, None, None))
        self.assertEqual(h.store.stats(), {})
        self.assertTrue(any("not been charged" in t for t in h.channel.texts_to("u1")))
        self.assertEqual(len(h.notifier.alerts), 1)

    def test_greeting_and_empty_messages_create_no_order(self):
        h = Harness()
        h.engine.on_message(Inbound("fake", "u1", "/start", None, None, None))
        h.engine.on_message(Inbound("fake", "u1", "   ", None, None, None))
        self.assertEqual(h.store.stats(), {})
        self.assertEqual(len(h.channel.texts_to("u1")), 2)

    def test_long_answers_are_clipped_to_one_message(self):
        long_answer = "Sentence number one. " * 400
        h = Harness(llm=FakeLLM([long_answer]), settings=Settings(max_answer_chars=500))
        o = h.ask()
        h.pay(o)
        h.run(10)
        sent = h.channel.texts_to("u1")[-1]
        self.assertLessEqual(len(sent), 504)
        self.assertEqual(h.order(o.id).status, "delivered")

    def test_empty_model_answer_counts_as_failure(self):
        h = Harness(llm=FakeLLM(["   ", "ok"]))
        o = h.ask()
        h.pay(o)
        h.run(30)
        self.assertEqual(h.llm.calls, 2)
        self.assertEqual(h.channel.count(ANSWER), 1)

    def test_restart_keeps_paid_orders(self):
        import os, tempfile
        from paidanswer.engine import Engine
        from paidanswer.store import Store
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.db")
            h = Harness()
            h.store = Store(path)
            h.engine.store = h.store
            o = h.ask()
            h.pay(o)
            # 'restart': a new engine and a new connection on the same database file
            store2 = Store(path)
            engine2 = Engine(h.settings, store2, {"fake": h.channel}, h.payments, h.llm, notifier=h.notifier, clock=h.clock)
            engine2.run_pending()
            self.assertEqual(store2.get(o.id).status, "delivered")

    def test_settings(self):
        s = Settings.from_env({"PRICE_BASE": "49", "PRICE_PREMIUM": "199", "CURRENCY": "inr",
                               "PREMIUM_KEYWORDS": "love, money", "ANSWER_DEADLINE_SECONDS": "300", "VOICE_REPLIES": "1"})
        self.assertEqual((s.base_price, s.premium_price, s.currency, s.deadline_seconds, s.voice_replies),
                         (49, 199, "INR", 300, True))
        self.assertEqual(s.amount_for("my money"), 19900)
        self.assertEqual(s.amount_for("my day"), 4900)
        self.assertEqual(money(9900, "INR"), "INR 99.00")
        with self.assertRaises(ValueError):
            Settings(deadline_seconds=5)
        with self.assertRaises(ValueError):
            Settings(backoff=(0,))


if __name__ == "__main__":
    unittest.main()


class EnvFileTests(unittest.TestCase):
    def test_load_env_file(self):
        import os, tempfile
        from paidanswer.cli import load_env_file
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".env")
            with open(path, "w", encoding="utf-8") as f:
                f.write('# comment\nPA_TEST_A=one\nPA_TEST_B="two words"\n\nPA_TEST_KEEP=from-file\nbroken line\n')
            os.environ.pop("PA_TEST_A", None)
            os.environ.pop("PA_TEST_B", None)
            os.environ["PA_TEST_KEEP"] = "from-env"
            load_env_file(path)
            self.assertEqual(os.environ["PA_TEST_A"], "one")
            self.assertEqual(os.environ["PA_TEST_B"], "two words")
            self.assertEqual(os.environ["PA_TEST_KEEP"], "from-env")
            for k in ("PA_TEST_A", "PA_TEST_B", "PA_TEST_KEEP"):
                os.environ.pop(k, None)
