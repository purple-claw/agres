import re

from agres import db, memory, pack
from tests.helpers import TempHome

SECTIONS = ["## Last handoff", "## Constraints", "## Rejected", "## Open work", "## Pinned",
            "## Recent decisions", "## More", "Recall:"]


def seed(s):
    ids = {}
    ids["crit"] = memory.add_item(s, "pin", "Secure boot dev: NO sacrificial board, zero brick tolerance.", priority=1)
    ids["pin"] = memory.add_item(s, "pin", "Board services: mosquitto, hermes, kerberos, hades.")
    ids["rej"] = memory.add_item(s, "decision", "Blue centre dot in the app icon.", status="rejected",
                                 why="user wants ring only")
    ids["dec"] = memory.add_item(s, "decision", "Keep applicationId com.okas.okas_smarthome.")
    ids["task"] = memory.add_item(s, "task", "Secure boot runbook for the fresh dev board")
    ids["done"] = memory.add_item(s, "task", "Phase 4 command execution")
    memory.close_item(s, ids["done"], "done")
    ids["err"] = memory.add_item(s, "error", "KNX tunnel drops after 10 minutes")
    ids["h_old"] = memory.add_item(s, "handoff", "Old handoff text")
    ids["h_new"] = memory.add_item(s, "handoff", "Splash work done; next: runbook stage 2.", replaces=ids["h_old"])
    ids["note"] = memory.add_item(s, "note", "MQTT topic map: loads/getLoads, loads/setLoads")
    return ids


class PackTest(TempHome):
    def setUp(self):
        super().setUp()
        self.root = self.git_repo("work", "okas")
        self.s = db.Store(self.root, create=True)

    def tearDown(self):
        self.s.close()
        super().tearDown()

    def test_sections_in_priority_order(self):
        ids = seed(self.s)
        out = pack.build(self.s)
        pos = [out.find(h) for h in SECTIONS]
        self.assertTrue(all(p >= 0 for p in pos), dict(zip(SECTIONS, pos)))
        self.assertEqual(pos, sorted(pos))
        self.assertTrue(out.startswith("# Agres memory — okas"))
        self.assertIn(f"P{ids['crit']} Secure boot dev", out)
        self.assertIn(f"D{ids['rej']}", out)
        self.assertIn("user wants ring only", out)
        self.assertIn("next: runbook stage 2", out)
        self.assertNotIn("Old handoff text", out)
        open_work = out[out.find("## Open work"):out.find("## Pinned")]
        self.assertIn(f"T{ids['task']}", open_work)
        self.assertIn(f"E{ids['err']}", open_work)
        self.assertNotIn(f"T{ids['done']}", out)
        self.assertIn(f"N{ids['note']}", out[out.find("## More"):])

    def test_budget_respected_and_footer_kept(self):
        seed(self.s)
        for i in range(80):
            memory.add_item(self.s, "pin", f"Pinned fact number {i} about subsystem {i} " + "detail " * 20)
        for budget in (900, 1500, 3000, 6000):
            out = pack.build(self.s, budget_chars=budget)
            self.assertLessEqual(len(out), budget, budget)
            self.assertIn("Recall:", out)
            self.assertIn("Secure boot dev", out)
        out = pack.build(self.s, budget_chars=6000)
        self.assertIn("more: `agres search", out)
        for budget in (3000, 6000):
            more = pack.build(self.s, budget_chars=budget).split("## More")[1]
            self.assertGreaterEqual(len(re.findall(r"\bP\d+ ", more)), 5, more)

    def test_hard_max(self):
        for i in range(400):
            memory.add_item(self.s, "pin", f"fact {i} " + "x" * 150)
        self.assertLessEqual(len(pack.build(self.s, budget_chars=50_000)), pack.MAX_CHARS)

    def test_items_cut_at_word_boundary_with_id(self):
        memory.add_item(self.s, "pin", "alpha " * 400)
        out = pack.build(self.s)
        line = next(l for l in out.splitlines() if l.startswith("- P"))
        self.assertLessEqual(len(line), pack.ITEM_CHARS + 12)
        self.assertTrue(line.endswith("…"), line)
        self.assertFalse(line.rstrip("…").endswith("alph"))

    def test_empty_store(self):
        out = pack.build(self.s)
        self.assertIn("No memory recorded yet", out)
        self.assertIn("Recall:", out)
        self.assertLess(len(out), 900)

    def test_notice_goes_under_header(self):
        seed(self.s)
        out = pack.build(self.s, notice="Agres is OFF for this project.")
        self.assertEqual(out.splitlines()[1], "Agres is OFF for this project.")

    def test_stale_tasks_leave_open_work(self):
        t = memory.add_item(self.s, "task", "Ancient refactor nobody touched")
        self.s.x("UPDATE items SET updated_at='2020-01-01T00:00:00Z' WHERE id=?", (t,))
        out = pack.build(self.s)
        self.assertNotIn("## Open work", out)
        self.assertIn(f"T{t} (stale)", out)

    def test_focus_files_rank_pins(self):
        a = memory.add_item(self.s, "pin", "callisto restarts with mosquitto", files=["packages/callisto/main.py"])
        memory.add_item(self.s, "pin", "unrelated newer fact one")
        memory.add_item(self.s, "pin", "unrelated newer fact two")
        out = pack.build(self.s, focus_files=["packages/callisto/main.py"])
        pinned = out[out.find("## Pinned"):].splitlines()
        self.assertTrue(pinned[1].startswith(f"- P{a} "), pinned[:3])

    def test_query_adds_related_section(self):
        ids = seed(self.s)
        out = pack.build(self.s, query="mqtt topic map")
        rel = out[out.find("## Related"):]
        self.assertIn(f"N{ids['note']}", rel)


class HandoffPrecedenceTest(TempHome):
    def setUp(self):
        super().setUp()
        self.s = db.Store(self.git_repo("work", "p"), create=True)

    def tearDown(self):
        self.s.close()
        super().tearDown()

    def test_auto_handoff_does_not_replace_a_written_one(self):
        rich = memory.add_handoff(self.s, "Done: stage 1. Next: stage 2 rollback test.", source="agent")
        auto = memory.add_handoff(self.s, "Auto-handoff (session ended): 1 prompt. Last request: hi", source="auto")
        self.assertEqual(memory.get_item(self.s, rich)["status"], "active")
        out = pack.build(self.s, git=False)
        block = out[out.find("## Last handoff"):]
        self.assertIn(f"H{rich}", block.splitlines()[0])
        self.assertIn("Next: stage 2 rollback test", block)
        self.assertIn(f"Since then (H{auto}", block)
        newer = memory.add_handoff(self.s, "Done: stage 2.", source="agent")
        for old in (rich, auto):
            self.assertEqual(memory.get_item(self.s, old)["status"], "superseded")
        self.assertNotIn("Since then", pack.build(self.s, git=False))
        self.assertIn(f"H{newer}", pack.build(self.s, git=False))

    def test_auto_alone_is_shown(self):
        a = memory.add_handoff(self.s, "Auto-handoff: last request x", source="auto")
        b = memory.add_handoff(self.s, "Auto-handoff: last request y", source="auto")
        self.assertEqual(memory.get_item(self.s, a)["status"], "superseded")
        self.assertIn(f"## Last handoff (H{b}", pack.build(self.s, git=False))
