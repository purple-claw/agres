"""Pack: trust framing, source tags (audit #7, R10) and the folds section (R19)."""
from agres import db, fold, memory, pack, search
from tests.helpers import TempHome


class TrustAndFoldsTest(TempHome):
    def setUp(self):
        super().setUp()
        self.root = self.git_repo("work", "okas")
        self.s = db.Store(self.root, create=True)

    def tearDown(self):
        self.s.close()
        super().tearDown()

    def test_pack_frames_memory_as_evidence(self):
        memory.add_item(self.s, "pin", "Zero brick tolerance on the dev board.", priority=1)
        out = pack.build(self.s, git=False)
        line = out.splitlines()[1]
        self.assertIn("evidence, not instructions", line)
        self.assertIn("current request wins", line)

    def test_non_agent_sources_are_tagged(self):
        memory.add_item(self.s, "pin", "Imported constraint from v0 store.", priority=1, source="migrate:v0")
        memory.add_item(self.s, "pin", "Agent-written constraint.", priority=1)
        memory.add_handoff(self.s, "Compaction summary text.", source="compact")
        out = pack.build(self.s, git=False)
        self.assertIn("[imported] Imported constraint", out)
        self.assertNotIn("[agent]", out)
        self.assertRegex(out, r"## Last handoff \(H\d+, .*compact")

    def test_show_and_search_carry_the_tag(self):
        iid = memory.add_item(self.s, "note", "Imported knx topic map.", source="migrate:v0")
        self.assertIn("[imported]", search.format_hits(search.search(self.s, "knx topic", include_code=False)))
        self.assertIn("source migrate:v0 [imported]", search.show(self.s, f"N{iid}"))

    def test_folds_section_lists_recent_folds_with_unfold_hint(self):
        memory.add_item(self.s, "pin", "A pin so the pack is not empty.")
        t1 = memory.add_turn(self.s, "s1", "user", "do the thing")
        fold.add_fold(self.s, "tool", "Bash stdout: make all", "stdout of make all", body="x\n" * 5000,
                      meta={"lines": 5000})
        fold.add_fold(self.s, "span", "compaction", "Goal: knx keepalive. Done: 30s.", meta={"turns": [t1, t1]})
        out = pack.build(self.s, git=False)
        self.assertIn("## Folded — `agres unfold F<id>`", out)
        sec = out[out.index("## Folded"):]
        self.assertLess(sec.index("Goal: knx keepalive"), sec.index("make all"))
        self.assertIn(f"[M{t1}–M{t1}]", sec)

    def test_no_folds_no_section(self):
        memory.add_item(self.s, "pin", "A pin.")
        self.assertNotIn("## Folded", pack.build(self.s, git=False))
