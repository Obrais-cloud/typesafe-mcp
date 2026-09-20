"""Unit tests for jevkit — no network. The TypeSafe transport (`systemone`) is
monkeypatched with canned API responses, so these run free and offline in CI.

    python3 -m unittest test_jevkit -v
"""
import os
import unittest

import jevkit


def _fake_systemone(answers, usage=None):
    def _call(state, questions, model=jevkit.TS_MODEL, timeout=60.0):
        return {"answers": answers, "usage": usage or {"input_tokens": 10}}
    return _call


class JevkitTests(unittest.TestCase):
    def setUp(self):
        self._orig_systemone = jevkit.systemone
        self._orig_quality = jevkit.quality_judge

    def tearDown(self):
        jevkit.systemone = self._orig_systemone
        jevkit.quality_judge = self._orig_quality

    def test_quality_judge_normalizes(self):
        jevkit.systemone = _fake_systemone({"quality": {
            "type": "score", "score": 3.0, "confidence": 0.9,
            "legend": {"0": "a", "1": "b", "2": "c", "3": "fully meets"},
            "probabilities": {"0": 0.0, "1": 0.0, "2": 0.1, "3": 0.9}}})
        r = jevkit.quality_judge("p", "resp", "crit")
        self.assertAlmostEqual(r["value"], 1.0)
        self.assertEqual(r["max"], 3)
        self.assertEqual(r["confidence"], 0.9)
        self.assertEqual(r["level"], "fully meets")

    def test_compare_pair_winner(self):
        jevkit.systemone = _fake_systemone({"winner": {
            "type": "choice", "choice": "a", "confidence": 0.8,
            "probabilities": {"a": 0.8, "b": 0.1, "tie": 0.1}}})
        r = jevkit.compare_pair("p", "A", "B")
        self.assertEqual(r["winner"], "a")
        self.assertEqual(r["confidence"], 0.8)

    def test_answer_matches_bool_and_value(self):
        jevkit.systemone = _fake_systemone({"correct": {"type": "noul", "noul": 0.9}})
        r = jevkit.answer_matches("p", "resp", "exp")
        self.assertTrue(r["correct"])
        self.assertEqual(r["value"], 0.9)
        jevkit.systemone = _fake_systemone({"correct": {"type": "noul", "noul": 0.2}})
        self.assertFalse(jevkit.answer_matches("p", "resp", "exp")["correct"])

    def test_best_of_ranks_and_breaks_ties(self):
        vals = {"good": 0.9, "ok": 0.88, "bad": 0.2}
        jevkit.quality_judge = lambda prompt, resp, crit=None, model=jevkit.TS_MODEL: {
            "value": vals[resp], "raw_score": 0, "max": 3, "confidence": 0.8,
            "level": "x", "probabilities": {}, "input_tokens": 1}
        r = jevkit.best_of("p", {"m1": "good", "m2": "bad"})
        self.assertEqual(r["winner"], "m1")
        self.assertFalse(r["tie"])
        # good (0.9) vs ok (0.88) are within tie_epsilon -> tie, winner None
        r2 = jevkit.best_of("p", {"m1": "good", "m2": "ok"})
        self.assertTrue(r2["tie"])
        self.assertIsNone(r2["winner"])

    def test_classify_push_failure_maps_cause_to_fix(self):
        jevkit.systemone = _fake_systemone({
            "cause": {"type": "choice", "choice": "auth", "confidence": 0.95,
                      "probabilities": {"auth": 0.95, "other": 0.05}},
            "needs_history_rewrite": {"type": "noul", "noul": 0.1},
            "severity": {"type": "score", "score": 1.0, "confidence": 0.9,
                         "legend": {"0": "a", "1": "b", "2": "c", "3": "d"},
                         "probabilities": {"0": 0.0, "1": 1.0, "2": 0.0, "3": 0.0}}})
        r = jevkit.classify_push_failure("could not read Username")
        self.assertEqual(r["cause"], "auth")
        self.assertFalse(r["escalate"])
        self.assertEqual(r["fixes"], jevkit.PUSH_FIXES["auth"])

    def test_low_confidence_or_other_escalates(self):
        jevkit.systemone = _fake_systemone({
            "cause": {"type": "choice", "choice": "network", "confidence": 0.4,
                      "probabilities": {"network": 0.4, "other": 0.3, "auth": 0.3}},
            "needs_history_rewrite": {"type": "noul", "noul": 0.0},
            "severity": {"type": "score", "score": 0.0, "confidence": 0.5,
                         "legend": {"0": "a", "1": "b", "2": "c", "3": "d"},
                         "probabilities": {"0": 1.0, "1": 0.0, "2": 0.0, "3": 0.0}}})
        self.assertTrue(jevkit.classify_push_failure("weird")["escalate"])

    def test_key_prefers_env(self):
        prev = os.environ.get("TYPESAFE_API_KEY")
        os.environ["TYPESAFE_API_KEY"] = "sk-test-123"
        try:
            self.assertEqual(jevkit.key(), "sk-test-123")
        finally:
            if prev is None:
                del os.environ["TYPESAFE_API_KEY"]
            else:
                os.environ["TYPESAFE_API_KEY"] = prev


if __name__ == "__main__":
    unittest.main()
