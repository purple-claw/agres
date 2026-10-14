import json
import os
import subprocess
import sys

from agres import db, paths
from tests.helpers import TempHome

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class CliTest(TempHome):
    def setUp(self):
        super().setUp()
        self.root = self.git_repo("work", "okas")
        self.sub = self.mkdir("work", "okas", "packages", "pulsar")

    def agres(self, *args, cwd=None, stdin=None, ok=True):
        env = dict(os.environ, PYTHONPATH=REPO, HOME=self.home)
        env.pop("AGRES_SESSION", None)
        p = subprocess.run([sys.executable, "-m", "agres", *args], cwd=cwd or self.sub, env=env,
                           input=stdin, capture_output=True, text=True, timeout=60)
        if ok:
            self.assertEqual(p.returncode, 0, f"agres {' '.join(args)}\n{p.stdout}\n{p.stderr}")
        return p

    # ------------------------------------------------------------- store
    def test_init_goes_to_git_root_unless_here(self):
        out = self.agres("init").stdout
        self.assertIn(os.path.join(self.root, ".agres"), out)
        self.assertFalse(os.path.exists(os.path.join(self.sub, ".agres")))
        self.agres("init", "--here")
        self.assertTrue(os.path.isfile(paths.db_path(self.sub)))

    def test_init_refuses_home(self):
        p = self.agres("init", cwd=self.home, ok=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("refusing", p.stderr)

    def test_writes_outside_a_project_fail_cleanly(self):
        p = self.agres("decide", "x y z", cwd=self.mkdir("loose"), ok=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("agres init", p.stderr)
        self.assertNotIn("Traceback", p.stderr)

    # ------------------------------------------------------------ memory
    def test_record_lifecycle(self):
        self.assertEqual(self.agres("decide", "Use MQTT on port 1884 for app traffic").stdout.split()[0], "D1")
        out = self.agres("decide", "App traffic goes over MQTT port 1884", "--why", "LAN auth").stdout
        self.assertIn("similar live items", out)
        self.assertIn("D1 ", out)
        self.assertIn("replaces D1", self.agres("decide", "MQTT 1884 with TLS", "--replaces", "D1").stdout)
        self.assertIn("(rejected)", self.agres("reject", "HTTP polling of loads").stdout)
        self.assertIn("(critical)", self.agres("pin", "--critical", "Zero brick tolerance").stdout)
        tid = self.agres("task", "Write the secure boot runbook").stdout.split()[0]
        self.assertIn("open → done", self.agres("done", tid, "--note", "merged").stdout)
        p = self.agres("done", tid, ok=False)
        self.assertIn("already done", p.stderr)
        self.assertIn("H", self.agres("handoff", "Runbook merged; next stage 2").stdout)
        show = self.agres("show", "D1").stdout
        self.assertIn("superseded by D3", show)
        brain = self.agres("brain").stdout
        self.assertIn("# Agres memory — okas", brain)
        self.assertIn("Zero brick tolerance", brain)
        self.assertIn("HTTP polling", brain)
        hits = self.agres("search", "brick").stdout
        self.assertTrue(hits.startswith("P"), hits)
        js = json.loads(self.agres("search", "MQTT", "--json", "--no-code").stdout)
        self.assertTrue(all(h["id"] != "D1" for h in js))

    def test_forget(self):
        self.agres("note", "temp token abc123secretvalue")
        self.agres("forget", "N1")
        self.assertIn("[forgotten]", self.agres("show", "N1").stdout)

    def test_on_off_and_config(self):
        self.agres("init")
        self.agres("off")
        self.assertTrue(os.path.exists(os.path.join(self.root, ".agres", "off")))
        self.agres("on")
        self.assertFalse(os.path.exists(os.path.join(self.root, ".agres", "off")))
        self.agres("config", "guard", "off")
        self.assertEqual(self.agres("config", "guard").stdout.strip(), "false")
        self.agres("config", "budget_chars", "4000", "--project")
        self.assertEqual(json.loads(self.agres("config").stdout)["budget_chars"], 4000)
        p = self.agres("config", "nope", "1", ok=False)
        self.assertIn("unknown setting", p.stderr)

    def test_status(self):
        self.agres("pin", "a fact worth keeping")
        out = self.agres("status").stdout
        self.assertIn("1 pin (0 critical)", out)
        self.assertEqual(json.loads(self.agres("status", "--json").stdout)["memory"]["pins"], 1)

    # -------------------------------------------------------------- code
    def test_index_map_touch_and_code_search(self):
        self.write(self.root, "packages/callisto/knx.py",
                   "class KnxTunnel:\n    def keepalive(self):\n        return 30\n")
        self.write(self.root, "packages/callisto/service.py",
                   "from .knx import KnxTunnel\n\ndef main():\n    KnxTunnel().keepalive()\n")
        self.write(self.root, "packages/callisto/__init__.py", "")
        out = self.agres("index").stdout
        self.assertRegex(out, r"indexed \d+ files")
        mp = self.agres("map", "--tokens", "300").stdout
        self.assertIn("knx.py", mp)
        hits = self.agres("search", "KnxTunnel").stdout
        self.assertIn("code", hits)
        self.write(self.root, "packages/callisto/extra.py", "def brand_new_function():\n    pass\n")
        self.assertIn("touched 1", self.agres("touch", "packages/callisto/extra.py", cwd=self.root).stdout)
        self.assertIn("brand_new_function", self.agres("search", "brand_new_function").stdout)
        payload = {"session_id": "s1", "cwd": self.root, "tool_name": "Write",
                   "tool_input": {"file_path": os.path.join(self.root, "packages/callisto/extra.py")}}
        self.write(self.root, "packages/callisto/extra.py", "def renamed_by_hook():\n    pass\n")
        self.assertEqual(self.agres("hook", "edit", stdin=json.dumps(payload)).stdout, "")
        self.assertIn("renamed_by_hook", self.agres("search", "renamed_by_hook").stdout)

    # ------------------------------------------------------------- hooks
    def test_hook_entrypoint(self):
        payload = {"session_id": "s1", "cwd": self.sub, "prompt_id": "p1"}
        out = self.agres("hook", "session-start", stdin=json.dumps(dict(payload, source="startup"))).stdout
        self.assertIn("additionalContext", out)
        self.assertEqual(self.agres("hook", "prompt", stdin=json.dumps(dict(payload, prompt="hi there"))).stdout, "")
        self.assertEqual(self.agres("hook", "stop", stdin=json.dumps(dict(payload, last_assistant_message="hello"))).stdout, "")
        self.assertEqual(self.agres("hook", "stop", stdin="garbage").stdout, "")
        with db.Store(self.root) as s:
            self.assertEqual(s.one("SELECT COUNT(*) FROM turns")[0], 2)

    # ------------------------------------------------------------ compat
    def test_v0_style_commands(self):
        self.agres("init")
        sid = self.agres("start", "topic 1.4 pointers").stdout.strip().splitlines()[-1]
        self.assertTrue(sid.startswith("sess_"), sid)
        self.agres("pin", "--text", "TOPIC 1.4 pointers", "--session", sid)
        self.agres("decision", "--text", "Use arena allocator", "--accepted", "--session", sid)
        self.agres("decision", "--text", "Use garbage collector", "--rejected", "--session", sid)
        self.agres("task", "--objective", "Phase 4 complete", "--status", "done", "--session", sid)
        self.agres("error", "--message", "MISCONCEPTION: arrays are pointers", "--session", sid)
        self.agres("capture", "--role", "user", "--text", "what is a pointer?", "--session", sid)
        self.agres("checkpoint", "--reason", "closed 1.4 as solid", "--session", sid)
        pack = self.agres("brain", "--budget", "900", "--session", sid).stdout
        self.assertIn("closed 1.4 as solid", pack)
        self.assertIn("garbage collector", pack)
        self.assertIn("pointer", self.agres("context", "--query", "pointer").stdout)
        self.assertIn("pointer", self.agres("unfold", "--query", "pointer").stdout)
        self.assertIn("removed in agres 1.0", self.agres("fold", "--target", "0").stdout)
        with db.Store(self.root) as s:
            rows = s.q("SELECT kind, status, session_id FROM items ORDER BY id")
            self.assertTrue(all(r["session_id"] == sid for r in rows))
            self.assertEqual([(r["kind"], r["status"]) for r in rows],
                             [("pin", "active"), ("decision", "accepted"), ("decision", "rejected"),
                              ("task", "done"), ("error", "open"), ("handoff", "active")])
