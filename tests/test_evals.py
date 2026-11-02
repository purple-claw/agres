"""Eval set (spec R12, R20): retrieval, staleness, abstention, guard, pack, origami. Latency is
reported by `python3 -m agres.evalrun` but not asserted here (noisy under a loaded test runner)."""
import unittest

from agres import evalrun


class EvalSetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = evalrun.run()

    def test_quality_checks_pass(self):
        failed = {k: self.r.get(k) for k, ok in self.r["checks"].items() if not ok and not k.endswith("_ms")}
        self.assertEqual(failed, {}, self.r.get("cases"))

    def test_origami_is_exact_and_small(self):
        self.assertTrue(self.r["unfold_exact"])
        self.assertTrue(self.r["no_refold_of_unfold"])
        self.assertLessEqual(self.r["fold_ratio"], 0.1)
        self.assertEqual(self.r["salient_kept"], 1.0)
