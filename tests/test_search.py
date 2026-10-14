from agres import db, memory, search
from tests.helpers import TempHome


class SearchTest(TempHome):
    fts = None

    def setUp(self):
        super().setUp()
        self.root = self.git_repo("work", "okas")
        self.s = db.Store(self.root, create=True, fts=self.fts)
        self.pin = memory.add_item(self.s, "pin", "Secure boot dev constraint: NO sacrificial board. Zero brick tolerance.",
                                   priority=1)
        self.rej = memory.add_item(self.s, "decision", "Burn eFuses on a sacrificial board first", status="rejected")
        self.other = memory.add_item(self.s, "decision", "Splash shows only before sign-in")
        self.old = memory.add_item(self.s, "decision", "Board talks HTTP polling")
        self.new = memory.add_item(self.s, "decision", "Board talks MQTT on 1884", replaces=self.old)
        memory.add_turn(self.s, "s1", "user", "please never brick the board, it is the only sacrificial-free unit")
        memory.add_turn(self.s, "s1", "assistant", "Understood: staged secure boot, reversible stages first.")
        memory.add_turn(self.s, "s1", "user", "and what about the zebra protocol?")

    def tearDown(self):
        self.s.close()
        super().tearDown()

    def ids(self, hits):
        return [h["id"] for h in hits]

    def test_constraint_and_rejection_rank_top(self):
        hits = search.search(self.s, "sacrificial board", include_code=False)
        self.assertEqual(set(self.ids(hits)[:2]), {f"P{self.pin}", f"D{self.rej}"})

    def test_turns_are_searchable(self):
        hits = search.search(self.s, "zebra protocol", include_code=False)
        self.assertTrue(hits and hits[0]["id"].startswith("M"), hits)
        self.assertIn("zebra", hits[0]["snippet"])

    def test_kind_filter(self):
        hits = search.search(self.s, "board", kinds=["decision"], include_code=False)
        self.assertTrue(hits)
        self.assertTrue(all(h["id"].startswith("D") for h in hits))

    def test_superseded_hidden_unless_all(self):
        self.assertNotIn(f"D{self.old}", self.ids(search.search(self.s, "HTTP polling", include_code=False)))
        self.assertIn(f"D{self.old}", self.ids(search.search(self.s, "HTTP polling", include_closed=True,
                                                              include_code=False)))

    def test_no_match(self):
        self.assertEqual(search.search(self.s, "kubernetes helm chart", include_code=False), [])

    def test_show_item(self):
        out = search.show(self.s, f"D{self.old}")
        self.assertIn("Board talks HTTP polling", out)
        self.assertIn(f"superseded by D{self.new}", out)
        out = search.show(self.s, f"D{self.new}")
        self.assertIn(f"replaces D{self.old}", out)

    def test_show_turn_with_context(self):
        mid = self.s.one("SELECT id FROM turns WHERE role='assistant'")[0]
        out = search.show(self.s, f"M{mid}", around=1)
        self.assertIn("never brick the board", out)
        self.assertIn("staged secure boot", out)
        self.assertIn("zebra", out)

    def test_show_unknown(self):
        with self.assertRaises(ValueError):
            search.show(self.s, "D999")


class SearchNoFtsTest(SearchTest):
    fts = False
