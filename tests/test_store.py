import os
import tempfile
import threading
import unittest

from paidanswer.store import Store, ST_ANSWERED, ST_AWAITING, ST_DELIVERED, ST_PAID, ST_REFUND_PENDING, ST_REFUNDED


def new_order(store, chat="c1", now=100.0):
    o = store.create_order("fake", chat, "q", None, None, 9900, "INR", now)
    store.set_link(o.id, "plink_1", "https://pay/x", now)
    return o


class StoreTests(unittest.TestCase):
    def test_mark_paid_results(self):
        s = Store()
        a, b = new_order(s, "a"), new_order(s, "b")
        self.assertEqual(s.mark_paid(a.id, "p1", 100, 600), "ok")
        self.assertEqual(s.mark_paid(a.id, "p1", 101, 600), "already")
        self.assertEqual(s.mark_paid(a.id, "p2", 101, 600), "conflict")
        self.assertEqual(s.mark_paid(b.id, "p1", 101, 600), "reused")
        self.assertEqual(s.mark_paid("nope", "p9", 101, 600), "unknown")
        self.assertEqual(s.get(a.id).deadline_at, 700)
        self.assertEqual(s.get(b.id).status, ST_AWAITING)

    def test_transitions_are_guarded(self):
        s = Store()
        o = new_order(s)
        self.assertFalse(s.record_answer(o.id, "x", 100))          # not paid yet
        self.assertFalse(s.to_refund(o.id, "r", 100))               # nothing to refund yet
        s.mark_paid(o.id, "p1", 100, 600)
        self.assertTrue(s.record_answer(o.id, "answer", 100))
        self.assertFalse(s.record_answer(o.id, "again", 100))       # only once
        self.assertTrue(s.mark_delivered(o.id, 101))
        self.assertFalse(s.mark_delivered(o.id, 102))               # only once
        self.assertFalse(s.to_refund(o.id, "late", 103))            # delivered orders cannot be refunded
        self.assertEqual(s.get(o.id).status, ST_DELIVERED)

    def test_refund_path(self):
        s = Store()
        o = new_order(s)
        s.mark_paid(o.id, "p1", 100, 600)
        self.assertTrue(s.to_refund(o.id, "r", 200))
        self.assertFalse(s.to_refund(o.id, "r", 200))
        self.assertTrue(s.mark_refunded(o.id, 201))
        self.assertFalse(s.mark_refunded(o.id, 202))
        self.assertEqual(s.get(o.id).status, ST_REFUNDED)

    def test_claim_respects_lease_and_schedule(self):
        s = Store()
        o = new_order(s)
        s.mark_paid(o.id, "p1", 100, 600)
        self.assertIsNone(s.claim_due(99, 60))                      # not due yet
        self.assertEqual(s.claim_due(100, 60).id, o.id)
        self.assertIsNone(s.claim_due(120, 60))                     # leased
        self.assertEqual(s.claim_due(161, 60).id, o.id)             # lease expired
        s.fail_attempt(o.id, "err", 300, 161)
        self.assertIsNone(s.claim_due(200, 60))                     # backing off
        self.assertEqual(s.claim_due(300, 60).id, o.id)

    def test_event_dedupe(self):
        s = Store()
        self.assertTrue(s.mark_event_seen("e1", 1))
        self.assertFalse(s.mark_event_seen("e1", 2))
        self.assertTrue(s.event_seen("e1"))
        self.assertFalse(s.event_seen("e2"))

    def test_open_order_lookup_and_unpaid_delete(self):
        s = Store()
        o = s.create_order("fake", "c1", "q", None, None, 9900, "INR", 100)
        self.assertIsNone(s.find_open_order("fake", "c1", 100, 3600))   # no link yet
        s.set_link(o.id, "pl", "https://x", 100)
        self.assertEqual(s.find_open_order("fake", "c1", 200, 3600).id, o.id)
        self.assertIsNone(s.find_open_order("fake", "c1", 10_000, 3600))  # too old
        o2 = s.create_order("fake", "c2", "q", None, None, 9900, "INR", 100)
        s.delete_unpaid(o2.id)
        self.assertIsNone(s.get(o2.id))
        s.delete_unpaid(o.id)                                           # has a link: kept
        self.assertIsNotNone(s.get(o.id))

    def test_two_connections_never_claim_the_same_order(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "db.sqlite")
            a, b = Store(path), Store(path)
            ids = []
            for i in range(30):
                o = new_order(a, f"c{i}")
                a.mark_paid(o.id, f"p{i}", 100, 600)
                ids.append(o.id)
            claimed, lock = [], threading.Lock()

            def worker(store):
                while True:
                    o = store.claim_due(100, 60)
                    if o is None:
                        return
                    with lock:
                        claimed.append(o.id)

            threads = [threading.Thread(target=worker, args=(st,)) for st in (a, b, a, b)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(sorted(claimed), sorted(ids))
            self.assertEqual(len(claimed), len(set(claimed)))


if __name__ == "__main__":
    unittest.main()
