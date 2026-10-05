import logging
import unittest

from paidanswer.scenarios import SCENARIOS


class ScenarioTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def test_every_scenario_passes_all_checks(self):
        for fn in SCENARIOS:
            with self.subTest(scenario=fn.__name__):
                result = fn()
                failed = [c.label for c in result.checks if not c.ok]
                self.assertTrue(result.checks, "scenario has no checks")
                self.assertEqual(failed, [])


if __name__ == "__main__":
    unittest.main()
