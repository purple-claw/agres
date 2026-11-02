"""`agres graph`: graph JSON, memory links, live events, local-only server guards."""
import http.client
import json
import os
import threading

from agres import db, fold, graphserve, memory
from agres.code import engine
from tests.test_index import IndexCase


class GraphTest(IndexCase):
    def setUp(self):
        super().setUp()
        self.root = self.project("g", git=True)
        self.put(self.root, {
            "app/knx.py": "class Tunnel:\n    def keepalive(self):\n        return 30\n",
            "app/main.py": "from app.knx import Tunnel\n\ndef run():\n    Tunnel().keepalive()\n",
            "app/util.py": "import json\n\ndef dump(x):\n    return json.dumps(x)\n",
        })
        self.s = self.store(self.root)
        engine.sync(self.s, use_git=False)
        engine.ensure_resolved(self.s)
        memory.add_item(self.s, "decision", "Keepalive 30s in app/knx.py for the tunnel", files=["app/knx.py"])
        memory.add_item(self.s, "decision", "Poll every second", status="rejected")
        fold.add_fold(self.s, "tool", "Bash stdout: pytest", "stdout", body="FAILED in app/main.py line 3\n")

    def test_files_level(self):
        g = graphserve.build_graph(self.s, "files")
        ids = {n["id"] for n in g["nodes"]}
        self.assertIn("f:app/knx.py", ids)
        self.assertIn("f:app/main.py", ids)
        self.assertTrue(any(e["source"] == "f:app/main.py" and e["target"] == "f:app/knx.py" for e in g["edges"]),
                        g["edges"])
        self.assertTrue(g["indexed"])
        json.dumps(g)

    def test_memory_links_to_files(self):
        g = graphserve.build_graph(self.s, "files")
        m = {(e["source"], e["target"]) for e in g["edges"] if e["kind"] == "mentions"}
        self.assertIn(("D1", "f:app/knx.py"), m)
        self.assertIn(("F1", "f:app/main.py"), m)
        rej = next(n for n in g["nodes"] if n["id"] == "D2")
        self.assertEqual(rej["sub"], "rejected")

    def test_symbols_level(self):
        g = graphserve.build_graph(self.s, "symbols")
        kinds = {n["kind"] for n in g["nodes"]}
        self.assertIn("symbol", kinds)
        self.assertTrue(any(e["kind"] == "defines" for e in g["edges"]))

    def test_fingerprint_moves_on_memory_write(self):
        a = graphserve.fingerprint(self.s)
        memory.add_item(self.s, "pin", "a new pin")
        self.assertNotEqual(a, graphserve.fingerprint(self.s))


class ServerTest(IndexCase):
    def setUp(self):
        super().setUp()
        self.root = self.project("srv", git=True)
        self.put(self.root, {"a.py": "def f():\n    return 1\n"})
        with db.Store(self.root, create=True) as s:
            memory.add_item(s, "pin", "pin about a.py")
        ready = threading.Event()
        box = {}

        def on_ready(url, httpd):
            box["url"], box["httpd"] = url, httpd
            ready.set()

        self.t = threading.Thread(target=graphserve.serve, args=(self.root,),
                                  kwargs={"port": 0, "open_browser": False, "ready": on_ready}, daemon=True)
        self.t.start()
        self.assertTrue(ready.wait(5))
        self.httpd = box["httpd"]
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        super().tearDown()

    def req(self, method, path, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path, headers=headers or {})
        r = c.getresponse()
        return r.status, r.getheader("Content-Type") or "", r

    def test_page_and_graph(self):
        code, ctype, r = self.req("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn("text/html", ctype)
        self.assertIn(b"agres graph", r.read())
        code, ctype, r = self.req("GET", "/api/graph?level=files")
        g = json.loads(r.read())
        self.assertFalse(g["indexed"])
        self.assertTrue(any(n["id"] == "P1" for n in g["nodes"]))

    def test_events_stream_first_message(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/api/events")
        r = c.getresponse()
        self.assertEqual(r.getheader("Content-Type"), "text/event-stream")
        first = r.fp.readline() + r.fp.readline()
        self.assertIn(b"event: changed", first)
        self.assertIn(b'"first": true', first)
        c.close()

    def test_foreign_host_is_refused(self):
        code, _, _ = self.req("GET", "/api/graph", {"Host": "evil.example:80"})
        self.assertEqual(code, 403)

    def test_cli_graph_is_a_real_command(self):
        import subprocess
        import sys
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ, PYTHONPATH=repo, HOME=self.home)
        p = subprocess.run([sys.executable, "-m", "agres", "graph", "--help"], cwd=self.root, capture_output=True,
                           text=True, env=env, timeout=30)
        self.assertEqual(p.returncode, 0)
        self.assertIn("--no-open", p.stdout)

    def test_index_post_needs_same_origin(self):
        code, _, _ = self.req("POST", "/api/index", {"Origin": "http://evil.example"})
        self.assertEqual(code, 403)
        code, _, r = self.req("POST", "/api/index", {"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(code, 202)
        self.assertTrue(json.loads(r.read())["started"])
