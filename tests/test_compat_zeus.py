"""Run the real zeus memory wrapper (unchanged) against this agres via PATH."""
import os
import subprocess
import sys
import unittest

from tests.helpers import TempHome

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ZEUS = os.path.expanduser(os.environ.get("ZEUS_MEM", "~/.agent-skills/zeus/scripts/zeus_mem.py"))


@unittest.skipUnless(os.path.isfile(ZEUS), "zeus skill not installed")
class ZeusCompatTest(TempHome):
    def setUp(self):
        super().setUp()
        self.bin = self.mkdir("bin")
        shim = os.path.join(self.bin, "agres")
        os.symlink(os.path.join(REPO, "bin", "agres"), shim)
        self.env = dict(os.environ, HOME=self.home, ZEUS_HOME=os.path.join(self.home, ".zeus"),
                        PATH=self.bin + os.pathsep + os.environ.get("PATH", ""))

    def zm(self, *args):
        p = subprocess.run([sys.executable, ZEUS, *args], env=self.env, capture_output=True, text=True,
                           timeout=120, cwd=self.home)
        self.assertEqual(p.returncode, 0, f"zeus_mem {' '.join(args)}\n{p.stdout}\n{p.stderr}")
        return p.stdout

    def test_track_topic_log_open_close(self):
        self.zm("track", "new", "arus", "C pointers", "--goal", "master pointers")
        out = self.zm("topic", "new", "c-pointers", "1.4", "Pointer arithmetic")
        self.assertIn("agres session: sess_", out)
        self.zm("decision", "c-pointers", "1.4", "teach with memory diagrams first")
        self.zm("decision", "c-pointers", "1.4", "skip malloc until 2.1", "--rejected")
        self.zm("misconception", "c-pointers", "1.4", "arrays are pointers")
        self.zm("capture", "c-pointers", "1.4", "why does p+1 skip 4 bytes?", "--role", "user")
        opened = self.zm("topic", "open", "c-pointers", "1.4")
        self.assertIn("## agres brain", opened)
        self.assertIn("memory diagrams", opened)
        self.assertIn("skip malloc", opened)
        self.zm("topic", "close", "c-pointers", "1.4", "solid", "--note", "nailed it")
        topic_dirs = [d for d, _, f in os.walk(os.path.join(self.home, ".zeus")) if d.endswith(".agres")]
        self.assertEqual(len(topic_dirs), 2, topic_dirs)  # _track + the topic: no strays
