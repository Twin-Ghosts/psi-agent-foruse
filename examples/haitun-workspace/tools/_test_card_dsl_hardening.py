"""Hardening / adversarial tests for the card DSL engine (_card_dsl).

The DSL is fed by an LLM (海豚) and business code, i.e. semi-trusted input, so
this suite pushes the untrusted-input and rebuild-invariant edges that the unit /
spec / fixes suites don't: XML entity attacks, serialization invariants, the
``rounds`` vs round clamp interaction (dead buttons on rebuild), XSD boolean
drift, and click-target routability under stress.

Run:  PYTHONPATH=. python -m pytest _test_card_dsl_hardening.py -q
  or: PYTHONPATH=. python _test_card_dsl_hardening.py
"""

from __future__ import annotations

import json
import os
import sys
import types
import unittest
from typing import Any

# ── stub runtime deps (superset shape, order-independent across suites) ──────
_impl = types.ModuleType("_todo_card_impl")
_impl._UNDO_ROUNDS = 20
_impl._build_card_from_state = lambda state: {"schema": "2.0", "_state": state, "_legacy_state": state}
_impl._tick_action_id = lambda i, r: f"todo_tick_{i}_r{r}"
_impl._untick_action_id = lambda i, r: f"todo_untick_{i}_r{r}"
sys.modules.setdefault("_todo_card_impl", _impl)

_paths = types.ModuleType("_runtime_paths")
_paths.agent_dir = lambda: os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.modules.setdefault("_runtime_paths", _paths)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _card_dsl  # noqa: E402


def _walk_values(card: dict[str, Any]):
    """Yield every callback value embedded in the compiled card."""

    def walk(node: Any):
        if isinstance(node, dict):
            if node.get("tag") in ("button", "input"):
                for b in node.get("behaviors", []):
                    yield b["value"]
            for v in node.values():
                yield from walk(v)
        elif isinstance(node, list):
            for v in node:
                yield from walk(v)

    yield from walk(card)


def _click_targets(card: dict[str, Any]) -> set[str]:
    return {v["action"] for v in _walk_values(card)}


# ── 1. Untrusted XML: entity / injection attacks ─────────────────────────────
class TestUntrustedXml(unittest.TestCase):
    def test_xxe_external_entity_blocked(self):
        # External entities must never be resolved (would leak local files).
        xxe = (
            '<?xml version="1.0"?>'
            '<!DOCTYPE card [ <!ENTITY x SYSTEM "file:///etc/hostname"> ]>'
            '<card title="&x;"><info label="a" value="b"/></card>'
        )
        out = _card_dsl.render_card(xxe)
        self.assertFalse(out["ok"])
        self.assertIn("not valid XML", out["error"])

    def test_billion_laughs_amplification_bounded(self):
        # 6 levels x10 => 10^6 expansion from a tiny doc; the parser must refuse,
        # not blow up memory. (Python 3.14 expat caps the amplification factor.)
        defs = ['<!ENTITY lol0 "haha">']
        for i in range(1, 7):
            ref = f"&lol{i - 1};" * 10
            defs.append(f'<!ENTITY lol{i} "{ref}">')
        doc = (
            '<?xml version="1.0"?>\n<!DOCTYPE card [\n'
            + "\n".join(defs)
            + '\n]>\n<card title="&lol6;"><info label="a" value="b"/></card>'
        )
        out = _card_dsl.render_card(doc)
        self.assertFalse(out["ok"], "billion-laughs must not compile")
        self.assertIn("not valid XML", out["error"])

    def test_undefined_entity_rejected(self):
        out = _card_dsl.render_card('<card title="&nope;"><info label="a" value="b"/></card>')
        self.assertFalse(out["ok"])
        self.assertIn("not valid XML", out["error"])

    def test_small_internal_entity_passthrough_is_escaped_in_json(self):
        # A single benign internal entity is allowed to expand; its value must
        # survive into the card as plain text (no structural leakage).
        doc = (
            '<?xml version="1.0"?>'
            '<!DOCTYPE card [<!ENTITY a "A&amp;B">]>'
            '<card title="&a;"><info label="x" value="y"/></card>'
        )
        out = _card_dsl.render_card(doc)
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(out["card"]["header"]["title"]["content"], "A&B")
        json.dumps(out["card"])  # round-trips

    def test_attribute_with_quotes_and_markup_never_breaks_json(self):
        # Values carrying " ' < > & { } must land as data, never as structure.
        nasty = 'he said "hi" & <b>{record_id}</b> \'x\''
        out = _card_dsl.render_card(
            f'<card title="{_card_dsl._xml_escape(nasty)}"><info label="l" value="v"/></card>'
        )
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(out["card"]["header"]["title"]["content"], nasty)
        blob = json.dumps(out["card"])
        self.assertIn("record_id", blob)  # present as literal text, not expanded


# ── 2. Rebuild invariant: no dead buttons at any round (rounds vs clamp) ─────
class TestNoDeadButtonOnRebuild(unittest.TestCase):
    def test_short_rounds_rebuilt_past_depth_still_routable(self):
        # <score rounds="5"> rebuilt to round 5+ used to render review_score_r5
        # with no handler → a dead button. Every rendered click target must have
        # a handler at every reachable round.
        xml = '<card title="t"><score min="1" max="3" rounds="5"/></card>'
        for rnd in (0, 4, 5, 6, 10, 19, 25):
            out = _card_dsl.render_card(xml, round_=rnd)
            self.assertTrue(out["ok"], out.get("error"))
            dead = _click_targets(out["card"]) - set(out["handlers"])
            self.assertEqual(dead, set(), f"round_={rnd} dead buttons: {dead}")

    def test_rounds_is_minimum_preregistration_depth(self):
        # rounds still governs the *minimum* handler depth at round 0.
        out = _card_dsl.render_card('<card title="t"><score rounds="5"/></card>', round_=0)
        self.assertIn("review_score_r4", out["handlers"])
        self.assertNotIn("review_score_r5", out["handlers"])

    def test_every_reachable_round_routable_default_card(self):
        review = (
            '<card title="t"><info label="a" value="b"/>'
            '<score min="1" max="5" bind-record="recX"/>'
            '<comment bind-record="recX"/>'
            '<action-row><button text="打回" type="reject" action="review_reject"/></action-row></card>'
        )
        for rnd in range(0, 20):
            out = _card_dsl.render_card(review, round_=rnd)
            self.assertTrue(out["ok"], out.get("error"))
            dead = _click_targets(out["card"]) - set(out["handlers"])
            self.assertEqual(dead, set(), f"round_={rnd} dead: {dead}")


# ── 3. XSD boolean drift: <row done="..."> ───────────────────────────────────
class TestDoneBooleanMatchesXsd(unittest.TestCase):
    def _done(self, literal: str) -> bool:
        out = _card_dsl.render_card(f'<card title="t"><list><row title="r" done="{literal}"/></list></card>')
        self.assertTrue(out["ok"], out.get("error"))
        return out["card"]["_state"]["rows"][0]["done"]

    def test_xsd_true_literals_are_done(self):
        # xs:boolean true set: "true"/"1" (plus common LLM casings tolerated).
        for lit in ("true", "TRUE", "True", "1"):
            self.assertTrue(self._done(lit), f"done={lit!r} should be done")

    def test_xsd_false_literals_are_not_done(self):
        for lit in ("false", "0", "", "no", "yes"):
            self.assertFalse(self._done(lit), f"done={lit!r} should not be done")

    def test_done_row_is_locked(self):
        out = _card_dsl.render_card('<card title="t"><list><row title="r" done="1"/></list></card>')
        self.assertTrue(out["card"]["_state"]["rows"][0]["locked"])


# ── 4. Serialization invariant: card + handlers are always JSON-clean ────────
class TestSerializationInvariant(unittest.TestCase):
    CARDS = (
        '<card title="眉" template="green"><info label="l" value="v"/></card>',
        '<card title="t"><score min="2" max="8" rounds="3" bind-record="r" selected="4"/></card>',
        '<card title="t"><comment placeholder="写点" bind-record="r"/><comment bind-record="r"/></card>',
        '<card title="t"><action-row><button text="通过" type="accept" action="review_reject"/>'
        '<button text="再" type="primary" action="review_reject"/></action-row></card>',
        '<card title="t"><list><row title="a" done="1"/><row title="b" bind-record="r2"/></list></card>',
    )

    def test_every_card_json_roundtrips(self):
        for xml in self.CARDS:
            out = _card_dsl.render_card(xml)
            self.assertTrue(out["ok"], f"{xml[:40]}: {out.get('error')}")
            reloaded = json.loads(json.dumps(out["card"], ensure_ascii=False))
            self.assertEqual(reloaded["schema"], "2.0")
            # handlers dict is str->str and JSON-clean too
            for k, v in out["handlers"].items():
                self.assertIsInstance(k, str)
                self.assertIsInstance(v, str)

    def test_handlers_keys_are_unique_and_round_named(self):
        out = _card_dsl.render_card('<card title="t"><score rounds="6"/></card>')
        keys = list(out["handlers"])
        self.assertEqual(len(keys), len(set(keys)), "handler keys must be unique")
        self.assertTrue(all(k.startswith("review_score_r") for k in keys))


# ── 5. Routability property: no rendered value ever points nowhere ───────────
class TestRoutabilityProperty(unittest.TestCase):
    SAMPLES = (
        '<card title="t"><score min="1" max="9" rounds="1"/></card>',
        '<card title="t"><score action="grade" rounds="2"/></card>',
        '<card title="t"><comment action="note"/></card>',
        '<card title="t"><action-row><button text="x" action="approve"/>'
        '<button text="y" action="approve"/></action-row></card>',
    )
    OVERRIDES = json.dumps({"grade": "g", "note": "n", "approve": "a"})

    def test_no_dead_targets_across_rounds(self):
        for xml in self.SAMPLES:
            for rnd in (0, 1, 2, 3, 19, 30):
                out = _card_dsl.render_card(xml, round_=rnd, handler_overrides_json=self.OVERRIDES)
                self.assertTrue(out["ok"], f"{xml[:40]} r{rnd}: {out.get('error')}")
                dead = _click_targets(out["card"]) - set(out["handlers"])
                self.assertEqual(dead, set(), f"{xml[:40]} r{rnd} dead: {dead}")


if __name__ == "__main__":
    # Run the *whole* module (all TestCase classes), not a hand-picked subset —
    # unittest.main() discovers every class here, so this stays in sync as
    # classes are added. (Guards the "only half the tests ran" trap.)
    unittest.main(verbosity=2)
