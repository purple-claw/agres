"""v0 -> v1 migration, driven against REAL v0 stores built by the old runtime."""
import datetime as dt
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from agres import db, memory, migrate, paths
from tests.fixtures.v0_builder import V0Builder
from tests.helpers import TempHome

_TMP = {}


# ----------------------------------------------------------------- templates
def _build_project_template():
    home = os.path.join(_TMP["dir"], "proj", "home")
    os.makedirs(home)
    b = V0Builder(home)
    p = os.path.join(home, "work", "p")
    os.makedirs(os.path.join(p, ".git"))
    # nested stray stores first, so the root's handoffs are the newest
    pulsar = os.path.join(p, "packages", "pulsar")
    sn = b.start(pulsar, "pulsar scratch")
    b.pin(pulsar, sn, "Pulsar nested pin: keep fixtures byte identical.")
    b.decision(pulsar, sn, "Do not vendor the xknx library.", rejected=True)
    b.decision(pulsar, sn, "A noted decision that carries no flag at all.", flags=False)
    b.decision(pulsar, sn, "Use MQTT on port 1884 for the status bus.")  # duplicate of a root decision
    b.task(pulsar, sn, "Nested open task about the pulsar converter")
    b.task(pulsar, sn, "Complete the pulsar audit of the ETS converter")  # imperative, not a finished task
    b.capture(pulsar, sn, "user", "nested user turn about pulsar")
    b.checkpoint(pulsar, sn, "nested handoff from pulsar scratch work")
    app = os.path.join(p, "app")  # a stray store that also has a stray store below it
    sa = b.start(app, "app scratch")
    b.pin(app, sa, "App nested pin about the flutter layout.")
    b.run(os.path.join(app, "lib"), "init")  # empty stray store
    b.standard(p)
    _TMP["project"] = os.path.join(_TMP["dir"], "proj")


def _build_global_template():
    home = os.path.join(_TMP["dir"], "glob", "home")
    os.makedirs(home)
    b = V0Builder(home)
    okas = os.path.join(home, "work", "okas")
    apa = os.path.join(home, "work", "aparena")
    for root, rels in ((okas, ("packages/pulsar/pulsar.py", "packages/hades/server.js", "app/lib/hvac_widgets.dart",
                               "README.md", "scripts/verify_options.sh", "docs/audit.md", "tools/fix_defects.py")),
                       (apa, ("data/topics/number-system.json", "js/prose.js", "index.html", "reading/analogies.html",
                              "reading/analogies-notes.md", "practice/analogies.js"))):
        os.makedirs(os.path.join(root, ".git"))
        for rel in rels:
            os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
            with open(os.path.join(root, rel), "w") as fh:
                fh.write("x\n")
    s_ok = b.start(home, "Okas v1.2 backend work")
    s_test = b.start(home, "Test model per session")
    s_sim = b.start(home, "256k sim edge 3")
    b.pin(home, s_ok, "Pulsar converter must keep ETS categories byte-identical (packages/pulsar/pulsar.py).")
    b.decision(home, s_ok, "HVAC airflow redone with soft plume beams in hvac_widgets.dart")
    b.task(home, s_ok, "Audit data/topics/number-system.json MCQs, then fix prose.js sentence spacing")
    b.pin(home, s_ok, "Added Exa remote MCP keyless and the web research skills to the global agent config")
    b.pin(home, s_ok, "Verify options, audit logs and fix defects before the demo")  # plain words only
    b.pin(home, s_test, "special %_ test 12345")
    b.decision(home, s_test, "deepseek critical decision X")
    b.decision(home, s_sim, "Hades serves the console on port 80 for packages/hades")
    b.error(home, s_ok, "hades server.js crashes on startup when OKAS_ADMIN_PORT is unset")
    b.task(home, s_ok, "T1 audit complete for number-system topics in data/topics", status="done")
    b.decision(home, s_ok, "Keep the analogies explanations short")  # weak evidence, but right after aparena items
    _TMP["glob"] = os.path.join(_TMP["dir"], "glob")


def setUpModule():
    _TMP["dir"] = os.path.realpath(tempfile.mkdtemp(prefix="agres-migrate-tpl-"))
    try:
        _build_project_template()
        _build_global_template()
    except unittest.SkipTest:
        shutil.rmtree(_TMP["dir"], ignore_errors=True)
        raise


def tearDownModule():
    shutil.rmtree(_TMP["dir"], ignore_errors=True)


# ------------------------------------------------------------------- helpers
def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def snapshot(root):
    out = {}
    for dp, dns, fns in os.walk(root):
        for n in dns + fns:
            full = os.path.join(dp, n)
            st = os.lstat(full)
            out[os.path.relpath(full, root)] = (st.st_size, st.st_mtime_ns)
    return out


def agres_dirs(root):
    found = []
    for dp, dns, _ in os.walk(root):
        if ".agres" in dns:
            found.append(os.path.relpath(os.path.join(dp, ".agres"), root))
            dns.remove(".agres")
    return sorted(found)


def ro(path):
    conn = sqlite3.connect("file:%s?mode=ro&immutable=1" % path, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


NESTED = ["app", "app/lib", "packages/pulsar"]
TOTALS = {"sessions": 4, "pins": 5, "decisions_accepted": 3, "decisions_rejected": 2, "notes": 1,
          "tasks_open": 4, "tasks_done": 3, "errors_open": 1, "errors_fixed": 1, "handoffs": 4,
          "handoffs_skipped": 3, "turns": 4, "duplicates_skipped": 1, "items": 24}


def v1_iso(ts):
    d = dt.datetime.fromisoformat(ts)
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class MigrateCase(TempHome):
    def install(self, which="project"):
        """Copy a prebuilt v0 tree into this test's fake HOME; returns work/p (the project template's root)."""
        src = os.path.join(_TMP[which], "home")
        if which == "project":
            shutil.copytree(os.path.join(src, "work", "p"), os.path.join(self.home, "work", "p"), symlinks=True)
        else:
            for name in os.listdir(src):
                s, d = os.path.join(src, name), os.path.join(self.home, name)
                (shutil.copytree(s, d, symlinks=True) if os.path.isdir(s) else shutil.copy2(s, d))
        return os.path.join(self.home, "work", "p")

    def items(self, root, kind=None):
        with db.Store(root) as s:
            if kind:
                return s.q("SELECT * FROM items WHERE kind=? ORDER BY id", (kind,))
            return s.q("SELECT * FROM items ORDER BY id")


# ------------------------------------------------------------------- finding
class FindStoresTest(MigrateCase):
    def test_finds_root_and_nested_v0_stores(self):
        p = self.install()
        found = migrate.find_v0_stores(p)
        self.assertEqual(found, sorted([p] + [os.path.join(p, n) for n in NESTED]))

    def test_start_above_finds_projects_too(self):
        p = self.install()
        self.assertIn(p, migrate.find_v0_stores(self.home))

    def test_prunes_heavy_dirs_and_ignores_symlinks_v1_and_junk(self):
        p = self.install()
        v0db = paths.db_path(p)
        for heavy in ("node_modules/x", "build/y", ".dart_tool/z"):
            d = os.path.join(p, heavy, ".agres")
            os.makedirs(d)
            shutil.copy2(v0db, os.path.join(d, "agres.db"))
        other = self.git_repo("elsewhere")
        os.makedirs(os.path.join(other, ".agres"))
        shutil.copy2(v0db, os.path.join(other, ".agres", "agres.db"))
        os.symlink(other, os.path.join(p, "linked"))
        v1 = os.path.join(p, "svc")
        os.makedirs(v1)
        db.Store(v1, create=True).close()
        junk = os.path.join(p, "junk", ".agres")
        os.makedirs(junk)
        with open(os.path.join(junk, "agres.db"), "w") as fh:
            fh.write("not a database")
        found = migrate.find_v0_stores(p)
        self.assertEqual(found, sorted([p] + [os.path.join(p, n) for n in NESTED]))


# ---------------------------------------------------------------------- plan
class PlanTest(MigrateCase):
    EXPECT = TOTALS

    def test_report_counts_mapping_and_nested(self):
        p = self.install()
        rep = migrate.plan(p)
        self.assertEqual(rep["state"], "v0")
        self.assertEqual(rep["root"], p)
        self.assertEqual(rep["nested"], NESTED)
        for k, v in self.EXPECT.items():
            self.assertEqual(rep["totals"][k], v, k)
        root = [s for s in rep["stores"] if s["relpath"] == ""][0]
        self.assertEqual(root["tables"]["pins"], 3)
        self.assertEqual(root["tables"]["checkpoints"], 6)
        self.assertEqual(root["tables"]["decisions"], 4)
        self.assertIn("folds", root["tables"])
        pulsar = [s for s in rep["stores"] if s["relpath"] == "packages/pulsar"][0]
        self.assertEqual(pulsar["origin"], "packages/pulsar")
        self.assertEqual(pulsar["tables"]["pins"], 1)

    def test_report_says_what_would_be_archived_where(self):
        p = self.install()
        rep = migrate.plan(p)
        pairs = {(a["from"], a["to"]) for a in rep["archive"]}
        self.assertIn((".agres/agres.db", ".agres/v0/agres.db"), pairs)
        self.assertIn((".agres/cas", ".agres/v0/cas"), pairs)
        self.assertIn(("packages/pulsar/.agres", ".agres/v0/nested/packages/pulsar"), pairs)
        self.assertIn(("app/lib/.agres", ".agres/v0/nested/app/lib"), pairs)
        self.assertIn(("app/.agres", ".agres/v0/nested/app"), pairs)

    def test_writes_nothing(self):
        p = self.install()
        before = snapshot(p)
        migrate.plan(p)
        self.assertEqual(snapshot(p), before)

    def test_sees_uncheckpointed_wal_rows_without_touching_files(self):
        p = self.install()
        live = sqlite3.connect(paths.db_path(p))
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("PRAGMA wal_autocheckpoint=0")
        live.execute("INSERT INTO pins(id,session_id,text,created_at) VALUES('pin_live','x','a live pin','2026-01-01T00:00:00+00:00')")
        live.commit()
        try:
            self.assertTrue(os.path.getsize(paths.db_path(p) + "-wal") > 0)
            before = snapshot(p)
            rep = migrate.plan(p)
            self.assertEqual(rep["totals"]["pins"], self.EXPECT["pins"] + 1)
            self.assertEqual(snapshot(p), before)
        finally:
            live.close()

    def test_refuses_home_and_reports_no_store(self):
        with self.assertRaises(migrate.MigrateError):
            migrate.plan(self.home)
        empty = self.git_repo("work", "empty")
        rep = migrate.plan(empty)
        self.assertEqual((rep["state"], rep["stores"], rep["nested"]), ("none", [], []))

    def test_v1_root_with_nothing_nested(self):
        p = self.git_repo("work", "fresh")
        db.Store(p, create=True).close()
        rep = migrate.plan(p)
        self.assertEqual((rep["state"], rep["stores"]), ("v1", []))


# --------------------------------------------------------------------- apply
class ApplyTest(MigrateCase):
    def setUp(self):
        super().setUp()
        self.root = self.install()
        self.v0 = ro(paths.db_path(self.root))
        self.v0_hash = sha(paths.db_path(self.root))
        self.plan = migrate.plan(self.root)
        self.result = migrate.apply(self.root)

    def tearDown(self):
        self.v0.close()
        super().tearDown()

    def counts(self, kind, status=None):
        rows = self.items(self.root, kind)
        return len([r for r in rows if status is None or r["status"] == status])

    def test_result_and_v1_store_opens(self):
        self.assertEqual(self.result["status"], "migrated")
        with db.Store(self.root) as s:  # no V0Store any more
            self.assertEqual(s.one("PRAGMA user_version")[0], db.SCHEMA_VERSION)
            self.assertEqual(s.get_meta("migrated_from"), "v0")
        self.assertEqual(self.result["totals"], self.plan["totals"])

    def test_mapping_counts_and_statuses(self):
        self.assertEqual(self.counts("pin"), 5)
        self.assertEqual(self.counts("pin", "active"), 5)
        self.assertEqual(self.counts("decision", "accepted"), 3)
        self.assertEqual(self.counts("decision", "rejected"), 2)
        self.assertEqual(self.counts("note", "active"), 1)
        self.assertEqual(self.counts("task", "open"), 4)
        self.assertEqual(self.counts("task", "done"), 3)
        self.assertEqual(self.counts("error", "open"), 1)
        self.assertEqual(self.counts("error", "fixed"), 1)
        self.assertEqual(self.counts("handoff"), 4)
        self.assertEqual(len(self.items(self.root)), 24)
        done = {r["text"] for r in self.items(self.root, "task") if r["status"] == "done"}
        self.assertEqual(done, {"Phase 5 complete: hardening verified", "Port the converter to python",
                                "Drop legacy alexa bridge"})
        errs = {r["text"]: r["status"] for r in self.items(self.root, "error")}
        self.assertEqual(errs["xknx tunnel drops after 20 min\nseen on callisto only"], "open")
        self.assertEqual(errs["AuthData wipe on redeploy is fixed and resolved"], "fixed")
        rej = {r["text"] for r in self.items(self.root, "decision") if r["status"] == "rejected"}
        self.assertIn("Use a sacrificial board for fuse tests.", rej)

    def test_provenance_fields(self):
        for r in self.items(self.root):
            self.assertEqual(r["source"], "migrate:v0")
            self.assertTrue(r["session_id"])
        self.assertIsNone([r for r in self.items(self.root, "pin") if "sacrificial" in r["text"]][0]["origin"])

    def test_timestamps_preserved(self):
        for table, col, kind in (("pins", "text", "pin"), ("tasks", "objective", "task")):
            for r in self.v0.execute(f"SELECT {col} AS t, created_at FROM {table}"):
                got = [x for x in self.items(self.root, kind) if x["text"] == r["t"]]
                self.assertEqual(len(got), 1)
                self.assertEqual(got[0]["created_at"], v1_iso(r["created_at"]))
                self.assertTrue(got[0]["created_at"].endswith("Z"))
        t = self.v0.execute("SELECT * FROM tasks WHERE status='done'").fetchone()
        it = [x for x in self.items(self.root, "task") if x["text"] == t["objective"]][0]
        self.assertEqual(it["closed_at"], v1_iso(t["updated_at"]))

    def test_checkpoints_become_handoffs_skipping_generic_reasons(self):
        hs = self.items(self.root, "handoff")
        texts = [h["text"] for h in hs]
        self.assertEqual(texts, ["nested handoff from pulsar scratch work",
                                 "callisto KNX outage fix verified on board; next: pulsar ETS categories",
                                 "secure boot runbook drafted; awaiting fresh board",
                                 "ETS categories fixed, 52 tests green"])
        for generic in ("session end", "pre-compact", "checkpoint"):
            self.assertNotIn(generic, texts)
        self.assertEqual([h["status"] for h in hs], ["superseded", "superseded", "superseded", "active"])
        self.assertEqual([h["superseded_by"] for h in hs[:3]], [hs[1]["id"], hs[2]["id"], hs[3]["id"]])

    def test_turns_verbatim_and_sessions_ended(self):
        with db.Store(self.root) as s:
            turns = s.q("SELECT role, text, created_at, session_id FROM turns ORDER BY id")
            self.assertEqual(len(turns), 4)
            orig = {(r["role"], r["content"]): r["created_at"] for r in self.v0.execute("SELECT * FROM turns")}
            for t in turns:
                if (t["role"], t["text"]) in orig:
                    self.assertEqual(t["created_at"], v1_iso(orig[(t["role"], t["text"])]))
            self.assertIn("Drafting the runbook now: step 1 is to read the OTP map.", [t["text"] for t in turns])
            sess = {r["title"]: r for r in s.q("SELECT * FROM sessions")}
            self.assertEqual(len(sess), 4)
            self.assertTrue(all(r["ended_at"] for r in sess.values()))
            self.assertEqual(sess["Secure boot runbook"]["prompts"], 2)
            self.assertEqual(sess["Secure boot runbook"]["cwd"], self.v0.execute(
                "SELECT repo FROM sessions WHERE title='Secure boot runbook'").fetchone()[0])
            self.assertEqual(sess["Secure boot runbook"]["agent"] or "",
                             self.v0.execute("SELECT COALESCE(NULLIF(model,''),NULLIF(model_provider,''),'') "
                                             "FROM sessions WHERE title='Secure boot runbook'").fetchone()[0])

    def test_imported_text_is_searchable_via_fts(self):
        with db.Store(self.root) as s:
            hits = s.q("SELECT rowid FROM fts_items WHERE fts_items MATCH 'sacrificial'") if s.fts else \
                s.q("SELECT rowid FROM fts_items WHERE body LIKE '%sacrificial%'")
            self.assertGreaterEqual(len(hits), 2)
            self.assertTrue(s.q("SELECT rowid FROM fts_turns LIMIT 1"))

    def test_v0_archived_byte_identical_and_nothing_deleted(self):
        v0dir = os.path.join(self.root, ".agres", "v0")
        self.assertEqual(sha(os.path.join(v0dir, "agres.db")), self.v0_hash)
        for name in ("cas", "checkpoints", "context_packs", "folds", "knowledge", "manifests", "sessions",
                     "pinned.md", "decisions.md", "tasks.md", "errors.md"):
            self.assertTrue(os.path.exists(os.path.join(v0dir, name)), name)
        live = {n for n in os.listdir(os.path.join(self.root, ".agres"))}
        self.assertEqual(live - {"agres.db", "agres.db-wal", "agres.db-shm", ".gitignore"}, {"v0"})

    def test_nested_merged_with_origin_and_stray_stores_gone(self):
        self.assertEqual(agres_dirs(self.root), [".agres"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "packages", "pulsar", ".agres")))
        self.assertTrue(os.path.isdir(os.path.join(self.root, "packages", "pulsar")))
        nested = os.path.join(self.root, ".agres", "v0", "nested")
        self.assertTrue(os.path.isfile(os.path.join(nested, "packages", "pulsar", "agres.db")))
        self.assertTrue(os.path.isfile(os.path.join(nested, "app", "lib", "agres.db")))
        self.assertTrue(os.path.isfile(os.path.join(nested, "app", "agres.db")))
        self.assertEqual({r["origin"] for r in self.items(self.root, "pin")},
                         {None, "packages/pulsar", "app"})
        open_tasks = {r["text"] for r in self.items(self.root, "task") if r["status"] == "open"}
        self.assertIn("Complete the pulsar audit of the ETS converter", open_tasks)
        by_text = {r["text"]: r for r in self.items(self.root)}
        self.assertEqual(by_text["Pulsar nested pin: keep fixtures byte identical."]["origin"], "packages/pulsar")
        self.assertEqual(by_text["Do not vendor the xknx library."]["origin"], "packages/pulsar")
        self.assertEqual(by_text["Do not vendor the xknx library."]["status"], "rejected")
        self.assertEqual(by_text["A noted decision that carries no flag at all."]["kind"], "note")
        self.assertIsNone(by_text["Never use a sacrificial board for the eFuse tests."]["origin"])
        # the duplicate nested decision was not imported a second time
        self.assertEqual(len([r for r in self.items(self.root, "decision")
                              if r["text"] == "Use MQTT on port 1884 for the status bus."]), 1)
        self.assertIsNone(by_text["Use MQTT on port 1884 for the status bus."]["origin"])

    def test_second_apply_is_a_noop(self):
        before = len(self.items(self.root))
        again = migrate.apply(self.root)
        self.assertEqual(again["status"], "already-v1")
        self.assertEqual(len(self.items(self.root)), before)
        self.assertEqual(len(self.items(self.root, "handoff")), 4)

    def test_remaining_nested_store_is_merged_into_existing_v1(self):
        stray = os.path.join(self.root, "docs")
        sid = V0Builder(self.home).start(stray, "docs scratch")
        V0Builder(self.home).pin(stray, sid, "Docs live under docs/ and build with mkdocs.")
        res = migrate.apply(self.root)
        self.assertEqual(res["status"], "migrated")
        pin = [r for r in self.items(self.root, "pin") if r["text"].startswith("Docs live")][0]
        self.assertEqual(pin["origin"], "docs")
        self.assertEqual(agres_dirs(self.root), [".agres"])
        self.assertEqual(len(self.items(self.root, "handoff")), 4)

    def test_refuses_without_any_store_and_on_home(self):
        with self.assertRaises(migrate.MigrateError):
            migrate.apply(self.git_repo("work", "empty"))
        with self.assertRaises(migrate.MigrateError):
            migrate.apply(self.home)


class ApplyFailureTest(MigrateCase):
    def test_failed_import_restores_everything(self):
        p = self.install()
        before = snapshot(p)
        v0 = sha(paths.db_path(p))
        with mock.patch.object(migrate.memory, "add_item", side_effect=RuntimeError("boom")):
            with self.assertRaises(Exception):
                migrate.apply(p)
        self.assertEqual(sha(paths.db_path(p)), v0)
        self.assertEqual(agres_dirs(p), [".agres"] + [f"{n}/.agres" for n in NESTED])
        self.assertFalse(os.path.exists(os.path.join(p, ".agres", "v0")))
        after = snapshot(p)
        self.assertEqual(set(after) - set(before), set())
        self.assertEqual(set(before) - set(after), set())
        with self.assertRaises(db.V0Store):
            db.Store(p)


# ------------------------------------------------------------------ rollback
class RollbackTest(MigrateCase):
    def test_rollback_restores_byte_identical_v0_and_nested(self):
        p = self.install()
        v0 = sha(paths.db_path(p))
        nested_hash = sha(os.path.join(p, "packages", "pulsar", ".agres", "agres.db"))
        migrate.apply(p)
        res = migrate.rollback(p)
        self.assertEqual(sha(paths.db_path(p)), v0)
        self.assertEqual(sha(os.path.join(p, "packages", "pulsar", ".agres", "agres.db")), nested_hash)
        self.assertEqual(agres_dirs(p), [".agres"] + [f"{n}/.agres" for n in NESTED])
        self.assertFalse(os.path.exists(os.path.join(p, ".agres", "v0")))
        aside = res["moved_aside"]
        self.assertTrue(os.path.basename(aside).startswith("v1-rolled-back-"))
        self.assertEqual(os.path.dirname(aside), os.path.join(p, ".agres"))
        conn = sqlite3.connect(os.path.join(aside, "agres.db"))
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
        conn.close()
        with self.assertRaises(db.V0Store):
            db.Store(p)
        self.assertEqual(sorted(migrate.find_v0_stores(p)), sorted([p] + [os.path.join(p, n) for n in NESTED]))

    def test_apply_again_after_rollback(self):
        p = self.install()
        first = migrate.apply(p)
        migrate.rollback(p)
        second = migrate.apply(p)
        self.assertEqual(second["totals"], first["totals"])
        self.assertEqual(len(self.items(p)), 24)
        self.assertEqual(len([n for n in os.listdir(os.path.join(p, ".agres")) if n.startswith("v1-rolled-back-")]), 1)

    def test_rollback_without_migration_refuses(self):
        p = self.install()
        with self.assertRaises(migrate.MigrateError):
            migrate.rollback(p)


# -------------------------------------------------------------------- global
class GlobalTest(MigrateCase):
    def setUp(self):
        super().setUp()
        self.install("glob")
        self.gdir = os.path.join(self.home, ".agres")
        self.okas = os.path.join(self.home, "work", "okas")
        self.apa = os.path.join(self.home, "work", "aparena")
        self.routes = migrate.global_routes(self.gdir, [self.okas, self.apa])

    def route(self, needle):
        got = [r for r in self.routes if needle in r["text"]]
        self.assertEqual(len(got), 1, needle)
        return got[0]

    def test_every_pin_decision_task_error_gets_a_route(self):
        self.assertEqual(len(self.routes), 11)
        self.assertEqual({r["kind"] for r in self.routes}, {"pin", "decision", "task", "error"})
        for r in self.routes:
            for k in ("kind", "text", "created_at", "suggested_root", "score", "reason", "junk"):
                self.assertIn(k, r)

    def test_suggests_the_matching_project(self):
        self.assertEqual(self.route("ETS categories")["suggested_root"], self.okas)
        self.assertEqual(self.route("HVAC airflow")["suggested_root"], self.okas)
        self.assertEqual(self.route("hades server.js crashes")["suggested_root"], self.okas)
        self.assertEqual(self.route("prose.js sentence spacing")["suggested_root"], self.apa)
        self.assertEqual(self.route("T1 audit complete")["suggested_root"], self.apa)
        self.assertGreater(self.route("ETS categories")["score"], 0)
        self.assertTrue(self.route("ETS categories")["reason"])

    def test_unmatched_has_no_suggestion(self):
        r = self.route("Exa remote MCP")
        self.assertIsNone(r["suggested_root"])
        self.assertFalse(r["junk"])

    def test_plain_word_overlap_alone_is_not_confident(self):
        r = self.route("Verify options, audit logs")
        self.assertIsNone(r["suggested_root"])
        self.assertGreater(r["scores"][self.okas], 0)  # words do occur in okas file names, but that proves nothing
        self.assertFalse(r["accept"])

    def test_unconfident_item_adopts_the_project_of_its_time_neighbours(self):
        r = self.route("analogies explanations")
        self.assertEqual(r["suggested_root"], self.apa)
        self.assertIn("adjacent in time", r["reason"])
        self.assertTrue(r["accept"])

    def test_session_junk_applies_only_while_the_test_session_was_live(self):
        conn = sqlite3.connect(os.path.join(self.gdir, "agres.db"))
        conn.execute("UPDATE sessions SET created_at='2020-01-01T00:00:00+00:00' WHERE title LIKE '256k%'")
        conn.commit()
        conn.close()
        later = migrate.global_routes(self.gdir, [self.okas, self.apa])
        hades = [r for r in later if "Hades serves the console" in r["text"]][0]
        self.assertFalse(hades["junk"])  # a stale pointer to a test session must not condemn real work
        self.assertEqual(hades["suggested_root"], self.okas)
        self.assertTrue([r for r in later if "special %_" in r["text"]][0]["junk"])  # text junk is unconditional

    def test_junk_flags(self):
        self.assertTrue(self.route("special %_ test 12345")["junk"])
        self.assertTrue(self.route("deepseek critical decision X")["junk"])
        # innocuous text, but recorded in a "256k sim edge 3" session
        self.assertTrue(self.route("Hades serves the console")["junk"])
        self.assertFalse(self.route("ETS categories")["junk"])
        self.assertFalse(self.route("HVAC airflow")["junk"])

    def test_default_accept_means_confident_and_not_junk(self):
        for r in self.routes:
            self.assertEqual(r["accept"], bool(r["suggested_root"]) and not r["junk"], r["text"])

    def test_statuses_follow_the_normal_mapping(self):
        self.assertEqual(self.route("T1 audit complete")["status"], "done")
        self.assertEqual(self.route("HVAC airflow")["status"], "accepted")
        self.assertEqual(self.route("hades server.js crashes")["status"], "open")

    def test_global_store_is_not_modified_by_routing(self):
        before = snapshot(self.gdir)
        migrate.global_routes(self.gdir, [self.okas, self.apa])
        self.assertEqual(snapshot(self.gdir), before)

    def test_apply_routes_imports_accepted_only_with_origin_global(self):
        res = migrate.apply_routes(self.routes)
        accepted = [r for r in self.routes if r["accept"]]
        self.assertEqual(res["imported"], len(accepted))
        self.assertEqual(res["skipped"], len(self.routes) - len(accepted))
        ok_items = self.items(self.okas)
        self.assertEqual({r["origin"] for r in ok_items}, {"global"})
        self.assertEqual({r["source"] for r in ok_items}, {"migrate:v0"})
        self.assertEqual(len(ok_items), 3)
        apa_items = self.items(self.apa)
        self.assertEqual(len(apa_items), 3)
        done = [r for r in apa_items if r["kind"] == "task" and r["status"] == "done"]
        self.assertEqual(len(done), 1)
        want = [r for r in self.routes if "HVAC" in r["text"]][0]["created_at"]
        self.assertEqual([r for r in ok_items if "HVAC" in r["text"]][0]["created_at"], want)
        self.assertNotIn("special", " ".join(r["text"] for r in ok_items + apa_items))

    def test_apply_routes_is_idempotent_and_honours_overrides(self):
        migrate.apply_routes(self.routes)
        again = migrate.apply_routes(self.routes)
        self.assertEqual(again["imported"], 0)
        self.assertEqual(len(self.items(self.okas)), 3)
        mcp = self.route("Exa remote MCP")
        mcp["accept"], mcp["target_root"] = True, self.apa
        res = migrate.apply_routes([mcp])
        self.assertEqual(res["imported"], 1)
        self.assertEqual(len(self.items(self.apa)), 4)

    def test_apply_routes_refuses_unmigrated_targets(self):
        p = self.install("project")
        r = {"kind": "pin", "text": "Some global pin", "status": "active", "created_at": "2026-01-01T00:00:00Z",
             "suggested_root": p, "accept": True, "junk": False, "score": 5, "reason": "x"}
        res = migrate.apply_routes([r])
        self.assertEqual(res["imported"], 0)
        self.assertEqual(len(res["errors"]), 1)

    def test_archive_global_moves_never_deletes(self):
        before = set(os.listdir(self.gdir))
        os.makedirs(os.path.join(self.gdir, "runtime"))  # the old runtime copy stays put for rollback
        res = migrate.archive_global(self.gdir)
        arch = os.path.join(self.gdir, "archive-v0")
        self.assertEqual(res["archive"], arch)
        self.assertTrue(os.path.isfile(os.path.join(arch, "agres.db")))
        self.assertFalse(os.path.exists(os.path.join(self.gdir, "agres.db")))
        self.assertTrue(os.path.isdir(os.path.join(self.gdir, "runtime")))
        self.assertEqual(set(os.listdir(arch)) - {"MANIFEST.json"}, before)
        self.assertEqual(migrate.archive_global(self.gdir)["moved"], [])
        with self.assertRaises(db.NoStore):  # nothing is left behind to be mistaken for a store
            db.Store(self.home)


if __name__ == "__main__":
    unittest.main()
