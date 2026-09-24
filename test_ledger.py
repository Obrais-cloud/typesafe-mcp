#!/usr/bin/env python3
"""Tests: the usage ledger can never break a judgment.

Run: python3 -m unittest -q test_ledger
"""
import asyncio
import os
import unittest

import core
import ledger


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeClient:
    """Stands in for httpx.AsyncClient: post() returns a canned answer."""
    def __init__(self, payload):
        self._payload = payload
        self.calls = 0

    async def post(self, *args, **kwargs):
        self.calls += 1
        return FakeResponse(self._payload)


class LedgerRobustness(unittest.TestCase):
    def test_no_config_is_a_silent_noop(self):
        # With no SUPABASE_* set, recording does nothing and never raises.
        for var in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"):
            os.environ.pop(var, None)
        # Also ensure no .env leaks credentials into this test.
        self.assertIsNone(
            ledger.record_jev_call(caller="test.noconfig", state_hash="x")
        )

    def test_record_swallows_a_broken_dispatch(self):
        # Even if the underlying send blows up, record_jev_call must not raise.
        os.environ["SUPABASE_URL"] = "http://127.0.0.1:1"  # unroutable-ish
        os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "test-key"
        original = ledger._dispatch
        ledger._dispatch = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            self.assertIsNone(ledger.record_jev_call(caller="test.boom", state_hash="x"))
        finally:
            ledger._dispatch = original
            os.environ.pop("SUPABASE_URL", None)
            os.environ.pop("SUPABASE_SERVICE_ROLE_KEY", None)

    def test_hash_state_is_stable_and_hex(self):
        h1 = ledger.hash_state({"a": 1, "b": 2})
        h2 = ledger.hash_state({"b": 2, "a": 1})  # order-independent
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 64)


class JudgeSurvivesLedgerFailure(unittest.TestCase):
    def test_ts_execute_returns_even_when_ledger_raises(self):
        # This is the guarantee the brief asks for: a ledger failure inside the
        # single call point must not break the judgment that passes through it.
        os.environ.setdefault("TYPESAFE_API_KEY", "test-key")  # _key() needs a value
        payload = {"answers": {"q": {"noul": 0.9}}, "usage": {"input_tokens": 7}, "model": "jev-1.13.0"}
        fake = FakeClient(payload)

        original = core.ledger.record_jev_call

        def boom(**kwargs):
            raise RuntimeError("ledger exploded")

        core.ledger.record_jev_call = boom
        try:
            result = asyncio.run(
                core._ts_execute(fake, {"model": "jev-1.13.0", "state": "s", "questions": {"q": {}}},
                                 caller="mcp.judge")
            )
        finally:
            core.ledger.record_jev_call = original

        self.assertEqual(result, payload)
        self.assertEqual(fake.calls, 1)


if __name__ == "__main__":
    unittest.main()
