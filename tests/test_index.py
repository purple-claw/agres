"""Code engine (agres.code.engine): discovery, parse, resolve, touch, map, search."""
import json
import os
import subprocess
import unittest
from unittest import mock

from agres.code import discover as discover_mod
from agres.code import engine
from agres.code import indexer as indexer_mod
from agres.db import Store

from tests.helpers import TempHome


class IndexCase(TempHome):
    def project(self, name="proj", git=False):
        root = self.mkdir("work", name)
        if git:
            self.git(root, "init", "-q")
        return root

    def git(self, root, *args):
        subprocess.run(["git", "-C", root, "-c", "user.email=t@t", "-c", "user.name=t", *args],
                       check=True, capture_output=True)

    def store(self, root):
        s = Store(root, create=True)
        self.addCleanup(s.close)
        return s

    def put(self, root, files):
        for rel, text in files.items():
            self.write(root, rel, text)

    def indexed(self, s):
        return {r["path"] for r in s.q("SELECT path FROM files")}

    def targets(self, s, rel, kind="imports"):
        """Active `kind` edges out of file `rel`: file targets as paths, packages as 'pkg:<name>'."""
        rows = s.q("SELECT dn.key AS k FROM edges e JOIN nodes sn ON sn.id=e.src JOIN nodes dn ON dn.id=e.dst "
                   "WHERE sn.key=? AND e.kind=? AND e.valid_to IS NULL", (f"f:{rel}", kind))
        out = set()
        for r in rows:
            k = r["k"]
            out.add(k[2:] if k.startswith("f:") else "pkg:" + k[2:] if k.startswith("x:") else k)
        return out


# ------------------------------------------------------------------ contract basics


class TestSync(IndexCase):
    def test_sync_reports_keys_counts_and_is_incremental(self):
        root = self.project()
        self.put(root, {"a.py": "import b\n\ndef f():\n    return b.g()\n", "b.py": "def g():\n    return 1\n"})
        s = self.store(root)
        r = engine.sync(s, use_git=False)
        for k in ("added", "modified", "deleted", "seconds", "counts"):
            self.assertIn(k, r)
        self.assertEqual((r["added"], r["modified"], r["deleted"]), (2, 0, 0))
        self.assertEqual(r["counts"]["files"], 2)
        self.assertEqual(r["counts"]["symbols"], 2)
        self.assertGreater(r["counts"]["edges"], 0)
        self.assertEqual(r["counts"], engine.counts(s))
        self.assertEqual(set(engine.counts(s)), {"files", "loc", "symbols", "edges"})
        r = engine.sync(s, use_git=False)
        self.assertEqual((r["added"], r["modified"], r["deleted"]), (0, 0, 0))
        self.write(root, "b.py", "def g():\n    return 2\n\ndef h():\n    return 3\n")
        os.utime(os.path.join(root, "b.py"), ns=(1, 1))
        r = engine.sync(s, use_git=False)
        self.assertEqual((r["added"], r["modified"], r["deleted"]), (0, 1, 0))
        os.remove(os.path.join(root, "a.py"))
        r = engine.sync(s, use_git=False)
        self.assertEqual(r["deleted"], 1)
        self.assertEqual(self.indexed(s), {"b.py"})

    def test_sync_uses_git_and_respects_gitignore(self):
        root = self.project(git=True)
        self.put(root, {".gitignore": "ignored.py\n", "keep.py": "x = 1\n", "ignored.py": "y = 2\n"})
        s = self.store(root)
        engine.sync(s)
        self.assertEqual(self.indexed(s), {".gitignore", "keep.py"})

    def test_git_worktree_with_dotgit_file(self):
        main = self.project("main", git=True)
        self.put(main, {"a.py": "x = 1\n"})
        self.git(main, "add", "-A")
        self.git(main, "commit", "-qm", "init")
        wt = os.path.join(self.home, "work", "wt")
        self.git(main, "worktree", "add", "-q", wt, "-b", "side")
        self.assertTrue(os.path.isfile(os.path.join(wt, ".git")))
        self.put(wt, {"b.py": "y = 1\n", ".gitignore": "skip.py\n", "skip.py": "z = 1\n"})
        s = self.store(wt)
        engine.sync(s)
        self.assertEqual(self.indexed(s), {"a.py", "b.py", ".gitignore"})

    def test_store_is_in_the_shared_db_with_memory_tables(self):
        root = self.project()
        self.put(root, {"a.py": "x = 1\n"})
        s = self.store(root)
        engine.sync(s, use_git=False)
        tables = {r["name"] for r in s.q("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"items", "turns", "nodes", "edges", "files"} <= tables)
        self.assertNotIn("search", tables)
        self.assertEqual(s.q("SELECT COUNT(*) c FROM items")[0]["c"], 0)


# ------------------------------------------------------------------- bug (a): imports


class TestImportResolution(IndexCase):
    def sync(self, root, files):
        self.put(root, files)
        s = self.store(root)
        engine.sync(s, use_git=False)
        return s

    def test_python_stdlib_does_not_resolve_to_same_stem_local_file(self):
        s = self.sync(self.project(), {
            "json.py": "def dumps(x):\n    return x\n",
            "typing.py": "List = list\n",
            "utils/os.py": "def join(*a):\n    return a\n",
            "utils/helper.py": "import os\nimport json\n",
            "app.py": "import json\nimport os\nfrom typing import List\nimport os.path\nfrom json import loads\n",
        })
        for src in ("app.py", "utils/helper.py"):
            t = self.targets(s, src)
            self.assertFalse(t & {"json.py", "typing.py", "utils/os.py"}, (src, t))
        self.assertEqual(self.targets(s, "app.py"), set())

    def test_python_stdlib_named_local_package_still_resolves_when_it_is_a_real_package(self):
        s = self.sync(self.project(), {
            "queue/__init__.py": "",
            "queue/worker.py": "def w():\n    pass\n",
            "app.py": "import queue\nimport queue.worker\nfrom queue.worker import w\nfrom pkg import *\n",
        })
        self.assertEqual(self.targets(s, "app.py"), {"queue/worker.py", "pkg:pkg"})

    def test_python_third_party_is_a_package_not_a_local_file(self):
        s = self.sync(self.project(), {
            "pkg/__init__.py": "",
            "pkg/requests.py": "def get():\n    pass\n",
            "pkg/client.py": "import requests\nfrom . import requests as local_requests\n",
        })
        t = self.targets(s, "pkg/client.py")
        self.assertIn("pkg:requests", t)
        self.assertIn("pkg/requests.py", t)  # the explicit relative import still resolves

    def test_python_local_absolute_imports_still_resolve(self):
        s = self.sync(self.project(), {
            "pkg/__init__.py": "",
            "pkg/sub/__init__.py": "",
            "pkg/sub/b.py": "def f():\n    pass\n",
            "pkg/sub/c.py": "X = 1\n",
            "tests/test_a.py": "from pkg.sub import b\nimport pkg.sub.c\nfrom helper import h\n",
            "tests/helper.py": "def h():\n    pass\n",
            "src/lib/__init__.py": "",
            "src/lib/m.py": "Y = 1\n",
            "tests/test_lib.py": "import lib.m\n",
        })
        self.assertEqual(self.targets(s, "tests/test_a.py"), {"pkg/sub/b.py", "pkg/sub/c.py", "tests/helper.py"})
        self.assertEqual(self.targets(s, "tests/test_lib.py"), {"src/lib/m.py"})

    def test_python_relative_imports_resolve(self):
        s = self.sync(self.project(), {
            "pkg/__init__.py": "",
            "pkg/sub/__init__.py": "",
            "pkg/sub/a.py": "from . import b\nfrom .c import y\nfrom ..other.mod import z\nfrom .. import top\n",
            "pkg/sub/b.py": "B = 1\n",
            "pkg/sub/c.py": "def y():\n    pass\n",
            "pkg/other/__init__.py": "",
            "pkg/other/mod.py": "def z():\n    pass\n",
            "pkg/top.py": "T = 1\n",
        })
        self.assertEqual(self.targets(s, "pkg/sub/a.py"),
                         {"pkg/sub/b.py", "pkg/sub/c.py", "pkg/other/mod.py", "pkg/top.py"})

    def test_python_relative_imports_in_namespace_package(self):
        s = self.sync(self.project(), {
            "ns/a.py": "from . import b\nfrom .c import y\n",
            "ns/b.py": "B = 1\n",
            "ns/c.py": "def y():\n    pass\n",
            "ns.py": "NOT_THE_PACKAGE = 1\n",
        })
        self.assertEqual(self.targets(s, "ns/a.py"), {"ns/b.py", "ns/c.py"})

    def test_python_relative_import_of_package_init(self):
        s = self.sync(self.project(), {
            "pkg/__init__.py": "VERSION = 1\n",
            "pkg/a.py": "from . import VERSION\nfrom .sub import thing\n",
            "pkg/sub/__init__.py": "thing = 1\n",
        })
        self.assertEqual(self.targets(s, "pkg/a.py"), {"pkg/__init__.py", "pkg/sub/__init__.py"})

    def test_js_relative_imports_resolve_and_builtins_do_not(self):
        s = self.sync(self.project(), {
            "src/a.js": ("import x from './x';\nimport y from '../lib/y/index';\nimport fs from 'fs';\n"
                         "import React from 'react';\nconst p = require('./p.js');\nimport z from 'node:path';\n"),
            "src/x.js": "export default 1;\n",
            "lib/y/index.js": "export default 2;\n",
            "src/fs.js": "export default 3;\n",
            "src/react.js": "export default 4;\n",
            "src/p.ts": "export const p = 1;\n",
        })
        self.assertEqual(self.targets(s, "src/a.js"), {"src/x.js", "lib/y/index.js", "src/p.ts", "pkg:react"})

    def test_ts_relative_imports_resolve(self):
        s = self.sync(self.project(), {
            "app/main.ts": "import { a } from './a';\nimport { b } from '../shared/b';\nimport { c } from './c.js';\n",
            "app/a.ts": "export const a = 1;\n",
            "shared/b.tsx": "export const b = 2;\n",
            "app/c.ts": "export const c = 3;\n",
        })
        self.assertEqual(self.targets(s, "app/main.ts"), {"app/a.ts", "shared/b.tsx", "app/c.ts"})

    def test_dart_package_and_relative_imports(self):
        s = self.sync(self.project(), {
            "pubspec.yaml": "name: my_app\ndescription: demo\ndependencies:\n  flutter:\n    sdk: flutter\n",
            "lib/main.dart": ("import 'dart:async';\nimport 'package:flutter/material.dart';\n"
                              "import 'package:my_app/src/widget.dart';\nimport 'src/util.dart';\n"
                              "import '../test_support/helpers.dart';\n"),
            "lib/material.dart": "class Material {}\n",
            "lib/src/widget.dart": "class Widget {}\n",
            "lib/src/util.dart": "int add(int a, int b) => a + b;\n",
            "test_support/helpers.dart": "void helper() {}\n",
        })
        t = self.targets(s, "lib/main.dart")
        self.assertIn("lib/src/widget.dart", t)
        self.assertIn("lib/src/util.dart", t)
        self.assertIn("test_support/helpers.dart", t)
        self.assertIn("pkg:flutter", t)
        self.assertNotIn("lib/material.dart", t)

    def test_dart_package_import_in_nested_app(self):
        s = self.sync(self.project(), {
            "app/pubspec.yaml": "name: okas_app\n",
            "app/lib/a.dart": "import 'package:okas_app/b.dart';\nimport 'package:other/b.dart';\n",
            "app/lib/b.dart": "class B {}\n",
        })
        t = self.targets(s, "app/lib/a.dart")
        self.assertEqual(t, {"app/lib/b.dart", "pkg:other"})


# ---------------------------------------------------------------- bug (b): denylist

SECRET_FILES = [".env", ".env.local", ".env.production", "server.pem", "tls/private.key", "cert.p12", "cert.pfx",
                "id_rsa", "id_rsa.pub", "keys/id_ed25519", "id_ed25519.pub", "secrets/token.txt",
                "deep/secrets/nested/a.py", "config/credentials.json", "aws_credentials", "release.keystore",
                "release.jks"]


class TestSecretDenylist(IndexCase):
    def test_discover_and_sync_never_read_secret_files(self):
        root = self.project(git=True)
        self.put(root, {"ok.py": "x = 1\n", **{p: "TOKEN=hunter2\n" for p in SECRET_FILES}})
        self.git(root, "add", "-f", "-A")  # even tracked secrets stay out
        s = self.store(root)
        reads = []
        real = discover_mod.read_text
        with mock.patch.object(indexer_mod, "_read_text", side_effect=lambda p: (reads.append(p), real(p))[1]):
            engine.sync(s)
        self.assertEqual(self.indexed(s), {"ok.py"})
        self.assertEqual([os.path.basename(p) for p in reads], ["ok.py"])
        self.assertEqual(s.q("SELECT COUNT(*) c FROM nodes WHERE path LIKE '%secret%' OR path LIKE '.env%'")[0]["c"], 0)

    def test_walk_fallback_prunes_secret_dirs(self):
        root = self.project()
        self.put(root, {"ok.py": "x = 1\n", "secrets/a.txt": "s\n", ".env": "A=1\n"})
        self.assertEqual([r for r, _, _ in discover_mod.discover(root)], ["ok.py"])

    def test_touch_never_reads_secret_files(self):
        root = self.project()
        self.put(root, {"ok.py": "x = 1\n", **{p: "TOKEN=hunter2\n" for p in SECRET_FILES}})
        s = self.store(root)
        engine.sync(s, use_git=False)
        with mock.patch.object(indexer_mod, "_read_text", side_effect=AssertionError("read a secret")):
            n = engine.touch(s, [os.path.join(root, p) for p in SECRET_FILES] + SECRET_FILES)
        self.assertEqual(n, 0)
        self.assertEqual(self.indexed(s), {"ok.py"})

    def test_is_secret_path_helper(self):
        from agres.util import is_secret_path
        for p in SECRET_FILES + [".envrc", "deploy/.env-prod"]:
            self.assertTrue(is_secret_path(p), p)
        for p in ("src/env.py", "environment.md", "keyboard.py", "monkey.txt", "secret_santa.py", "README.md",
                  "docs/secrets.md", "main.py"):
            self.assertFalse(is_secret_path(p), p)


# ------------------------------------------------------------ bug (c): yocto / devicetree

BB = """\
SUMMARY = "demo recipe"
LICENSE = "MIT"
require demo.inc
inherit cmake systemd

SRC_URI = " \\
    git://example.com/demo.git;branch=main \\
    file://demo.service \\
"
DEPENDS += "openssl"
SRC_URI:append = " file://extra.patch"

do_configure() {
    echo "cfg ${S}"
}

python do_stamp() {
    d.setVar('X', '1')
}

fakeroot do_install() {
    install -d ${D}${bindir}
}
do_install:append() {
    :
}
"""

DTS = """\
/dts-v1/;
#include "board.dtsi"

/ {
    model = "Demo Board";
    soc {
        uart0: serial@40001000 {
            compatible = "demo,uart";
            status = "okay";
        };
        i2c1: i2c@40002000 {
            sensor@48 {
                reg = <0x48>;
            };
        };
    };
};

&uart0 {
    status = "disabled";
};
"""


class TestYoctoAndDeviceTree(IndexCase):
    def sync(self, root, files):
        self.put(root, files)
        s = self.store(root)
        engine.sync(s, use_git=False)
        return s

    def symbols(self, s, rel):
        return {r["qualname"]: r["kind"] for r in s.q(
            "SELECT qualname, kind FROM nodes WHERE path=? AND kind NOT IN ('file','dir') AND deleted_at IS NULL",
            (rel,))}

    def test_bitbake_files_are_indexed_with_symbols(self):
        s = self.sync(self.project(), {
            "meta-x/recipes-demo/demo/demo_1.0.bb": BB,
            "meta-x/recipes-demo/demo/demo.inc": 'PV = "1.0"\n',
            "meta-x/recipes-demo/demo/demo_%.bbappend": 'FILESEXTRAPATHS:prepend := "${THISDIR}/files:"\n',
            "meta-x/classes/mine.bbclass": "do_mine() {\n    :\n}\n",
            "build/conf/local.conf": 'MACHINE ?= "qemuarm"\n',
            "build/tmp/work/junk.bb": 'JUNK = "1"\n',
            "build/conf/deeper/x.conf": 'X = "1"\n',
        })
        self.assertNotIn("build/tmp/work/junk.bb", self.indexed(s))
        self.assertNotIn("build/conf/deeper/x.conf", self.indexed(s))
        want = {"meta-x/recipes-demo/demo/demo_1.0.bb", "meta-x/recipes-demo/demo/demo.inc",
                "meta-x/recipes-demo/demo/demo_%.bbappend", "meta-x/classes/mine.bbclass",
                "build/conf/local.conf"}
        self.assertTrue(want <= self.indexed(s))
        for p in want:
            self.assertEqual(s.one("SELECT lang FROM files WHERE path=?", (p,))["lang"], "bitbake", p)
        syms = self.symbols(s, "meta-x/recipes-demo/demo/demo_1.0.bb")
        for name in ("SRC_URI", "DEPENDS", "do_configure", "do_stamp", "do_install"):
            self.assertIn(name, syms)
        self.assertEqual(syms["do_install"], "function")
        self.assertEqual(s.one("SELECT lang FROM files WHERE path='build/conf/local.conf'")["lang"], "bitbake")
        self.assertIn("MACHINE", self.symbols(s, "build/conf/local.conf"))
        self.assertIn("do_mine", self.symbols(s, "meta-x/classes/mine.bbclass"))
        hits = engine.search_code(s, "do_install")
        self.assertTrue(any(h["name"] == "do_install" for h in hits))

    def test_bitbake_variable_symbols_are_capped(self):
        text = "".join(f'VAR_{i} = "{i}"\n' for i in range(200)) + "do_install() {\n    :\n}\n"
        s = self.sync(self.project(), {"big.inc": text})
        syms = self.symbols(s, "big.inc")
        self.assertEqual(sum(1 for k in syms.values() if k == "variable"), 60)
        self.assertEqual(syms["do_install"], "function")

    def test_bitbake_require_and_inherit_resolve(self):
        s = self.sync(self.project(), {
            "r/demo_1.0.bb": BB,
            "r/demo.inc": 'PV = "1.0"\n',
            "classes/cmake.bbclass": "do_compile() {\n    :\n}\n",
        })
        self.assertEqual(self.targets(s, "r/demo_1.0.bb"), {"r/demo.inc", "classes/cmake.bbclass"})

    def test_devicetree_files_are_indexed_with_node_symbols(self):
        s = self.sync(self.project(), {"arch/board.dts": DTS, "arch/board.dtsi": "/ {\n    chosen {\n    };\n};\n"})
        for p in ("arch/board.dts", "arch/board.dtsi"):
            self.assertEqual(s.one("SELECT lang FROM files WHERE path=?", (p,))["lang"], "devicetree", p)
        names = {r["name"] for r in s.q("SELECT name FROM nodes WHERE path='arch/board.dts' AND kind NOT IN ('file','dir')")}
        self.assertTrue({"uart0", "i2c1", "soc"} <= names, names)
        self.assertEqual(self.targets(s, "arch/board.dts"), {"arch/board.dtsi"})
        self.assertTrue(any(h["name"] == "uart0" for h in engine.search_code(s, "uart0")))


# ------------------------------------------------------- bug (d): touch / ensure_resolved


class TestTouchAndResolve(IndexCase):
    def setup_pair(self):
        root = self.project()
        self.put(root, {"a.py": "def f():\n    return 1\n", "b.py": "def g():\n    return 2\n"})
        s = self.store(root)
        engine.sync(s, use_git=False)
        return root, s

    def test_touch_defers_edges_until_ensure_resolved(self):
        root, s = self.setup_pair()
        self.assertEqual(self.targets(s, "a.py"), set())
        self.assertEqual(s.get_meta("resolve_dirty"), "0")
        self.write(root, "a.py", "import b\n\ndef f():\n    return b.g()\n")
        self.assertEqual(engine.touch(s, ["a.py"]), 1)
        self.assertEqual(s.get_meta("resolve_dirty"), "1")
        self.assertEqual(self.targets(s, "a.py"), set())          # edge missing before resolve
        self.assertTrue(engine.ensure_resolved(s))
        self.assertEqual(self.targets(s, "a.py"), {"b.py"})        # present after
        self.assertEqual(s.get_meta("resolve_dirty"), "0")
        self.assertFalse(engine.ensure_resolved(s))                # nothing left to do

    def test_touch_never_runs_the_global_resolve(self):
        root, s = self.setup_pair()
        self.write(root, "a.py", "import b\n")
        with mock.patch("agres.code.resolve.resolve", side_effect=AssertionError("resolve ran")), \
                mock.patch("agres.code.rank.compute_ranks", side_effect=AssertionError("ranks ran")):
            self.assertEqual(engine.touch(s, [os.path.join(root, "a.py")]), 1)

    def test_several_touches_then_one_resolve_is_correct(self):
        root, s = self.setup_pair()
        self.put(root, {"c.py": "def h():\n    return 3\n"})
        self.write(root, "a.py", "import b\nimport c\n")
        self.write(root, "b.py", "import c\n\ndef g():\n    return c.h()\n")
        self.assertEqual(engine.touch(s, ["a.py", os.path.join(root, "b.py"), "c.py"]), 3)
        self.assertEqual(engine.touch(s, ["a.py"]), 0)             # same content: nothing to re-parse
        self.assertTrue(engine.ensure_resolved(s))
        self.assertEqual(self.targets(s, "a.py"), {"b.py", "c.py"})
        self.assertEqual(self.targets(s, "b.py"), {"c.py"})
        self.assertIn("c.py", {r["path"] for r in s.q("SELECT path FROM files")})
        # removing the import and touching again closes the edge (history kept)
        self.write(root, "a.py", "import c\n")
        engine.touch(s, ["a.py"])
        engine.ensure_resolved(s)
        self.assertEqual(self.targets(s, "a.py"), {"c.py"})
        closed = s.q("SELECT 1 FROM edges e JOIN nodes d ON d.id=e.dst WHERE d.key='f:b.py' AND e.kind='imports' "
                     "AND e.valid_to IS NOT NULL")
        self.assertTrue(closed)

    def test_touch_ignores_outside_ignored_binary_and_unknown(self):
        root, s = self.setup_pair()
        outside = os.path.join(self.home, "elsewhere.py")
        with open(outside, "w") as fh:
            fh.write("x = 1\n")
        self.put(root, {"node_modules/lib/i.js": "x\n", "gen/out.py": "x = 1\n", ".agresignore": "gen\n",
                        "logo.png": "not really\n"})
        paths = [outside, "../elsewhere.py", "node_modules/lib/i.js", "gen/out.py", "logo.png", "nope/missing.py",
                 os.path.join(root, ".agres", "agres.db")]
        self.assertEqual(engine.touch(s, paths), 0)
        self.assertEqual(self.indexed(s), {"a.py", "b.py"})
        self.assertEqual(s.get_meta("resolve_dirty"), "0")

    def test_touch_indexes_new_files_and_marks_missing_deleted(self):
        root, s = self.setup_pair()
        self.put(root, {"new/n.py": "def brand_new_func():\n    pass\n"})
        self.assertEqual(engine.touch(s, ["new/n.py"]), 1)
        self.assertIn("new/n.py", self.indexed(s))
        self.assertEqual(engine.search_code(s, "brand_new_func")[0]["path"], "new/n.py")
        os.remove(os.path.join(root, "b.py"))
        self.assertEqual(engine.touch(s, [os.path.join(root, "b.py")]), 1)
        self.assertNotIn("b.py", self.indexed(s))
        self.assertIsNotNone(s.one("SELECT 1 FROM nodes WHERE key='f:b.py' AND deleted_at IS NOT NULL"))
        self.assertIsNone(s.one("SELECT 1 FROM nodes WHERE key='s:b.py::g' AND deleted_at IS NULL"))
        self.assertEqual(s.get_meta("resolve_dirty"), "1")

    def test_touch_skips_gitignored_new_files(self):
        root = self.project(git=True)
        self.put(root, {".gitignore": "scratch.py\n", "a.py": "x = 1\n"})
        s = self.store(root)
        engine.sync(s)
        self.put(root, {"scratch.py": "y = 2\n", "b.py": "z = 3\n"})
        self.assertEqual(engine.touch(s, ["scratch.py", "b.py"]), 1)
        self.assertEqual(self.indexed(s), {".gitignore", "a.py", "b.py"})

    def test_sync_after_touch_resolves_and_clears_flag(self):
        root, s = self.setup_pair()
        self.write(root, "a.py", "import b\n")
        engine.touch(s, ["a.py"])
        r = engine.sync(s, use_git=False)
        self.assertEqual((r["added"], r["modified"]), (0, 0))
        self.assertTrue(r["resolved"])
        self.assertEqual(self.targets(s, "a.py"), {"b.py"})
        self.assertEqual(s.get_meta("resolve_dirty"), "0")

    def test_ensure_resolved_yields_to_a_running_sync(self):
        root, s = self.setup_pair()
        self.write(root, "a.py", "import b\n")
        engine.touch(s, ["a.py"])
        with discover_mod.sync_lock(s) as held:
            self.assertTrue(held)
            self.assertFalse(engine.ensure_resolved(s))
        self.assertEqual(s.get_meta("resolve_dirty"), "1")
        self.assertTrue(engine.ensure_resolved(s))

    def test_ensure_resolved_on_fresh_store_runs_once(self):
        root = self.project()
        self.put(root, {"a.py": "import b\n", "b.py": "x = 1\n"})
        s = self.store(root)
        indexer_mod.Indexer(s).scan()
        self.assertTrue(engine.ensure_resolved(s))
        self.assertFalse(engine.ensure_resolved(s))
        self.assertEqual(self.targets(s, "a.py"), {"b.py"})


# -------------------------------------------------------------- bug (e): discovery


class TestDiscovery(IndexCase):
    def test_walk_prunes_ignored_dirs_before_descending(self):
        root = self.project()
        self.put(root, {"keep/a.py": "x = 1\n", "node_modules/pkg/deep/i.js": "x\n", "build/o.py": "x = 1\n",
                        "gen/auto/g.py": "x = 1\n", ".agresignore": "gen\n*.tmp\n", "keep/x.tmp": "t\n",
                        ".github/ci.yml": "on: push\n"})
        visited = []
        real_walk = os.walk

        def spy(top, *a, **kw):
            for dp, dns, fns in real_walk(top, *a, **kw):
                visited.append(dp)
                yield dp, dns, fns

        with mock.patch.object(discover_mod.os, "walk", spy):
            found = [r for r, _, _ in discover_mod.discover(root)]
        self.assertEqual(found, [".agresignore", ".github/ci.yml", "keep/a.py"])
        joined = "\n".join(visited)
        for bad in ("node_modules", "build", "gen"):
            self.assertNotIn(os.sep + bad, joined)
        self.assertTrue(all(v == root or v.startswith(root + os.sep) for v in visited))

    def test_yocto_build_conf_is_kept_but_build_outputs_are_not(self):
        root = self.project(git=True)
        self.put(root, {"build/conf/local.conf": 'MACHINE ?= "x"\n', "build/conf/sub/y.conf": "A = 1\n",
                        "build/out.py": "x = 1\n", "a.py": "x = 1\n"})
        self.git(root, "add", "-f", "-A")
        self.assertEqual([r for r, _, _ in discover_mod.discover(root)], ["a.py", "build/conf/local.conf"])
        flat = self.project("flat2")
        self.put(flat, {"build/conf/local.conf": 'MACHINE ?= "x"\n', "build/conf/sub/y.conf": "A = 1\n",
                        "build/out.py": "x = 1\n", "meta/build/conf/bblayers.conf": 'BBLAYERS = "a"\n', "a.py": "x = 1\n"})
        self.assertEqual([r for r, _, _ in discover_mod.discover(flat)],
                         ["a.py", "build/conf/local.conf", "meta/build/conf/bblayers.conf"])

    def test_never_looks_above_store_root(self):
        top = self.project("mono", git=True)
        self.put(top, {"outside.py": "x = 1\n", "sub/inside.py": "y = 1\n", "sub/pkg/m.py": "z = 1\n"})
        self.git(top, "add", "-A")
        sub = os.path.join(top, "sub")
        s = self.store(sub)
        engine.sync(s)
        self.assertEqual(self.indexed(s), {"inside.py", "pkg/m.py"})
        # and the plain walk fallback never goes up either
        flat = self.project("flat")
        self.put(flat, {"a.py": "x = 1\n", "inner/b.py": "y = 1\n"})
        self.put(os.path.dirname(flat), {"sibling.py": "z = 1\n"})
        visited = []
        real_walk = os.walk

        def spy(top_, *a, **kw):
            for dp, dns, fns in real_walk(top_, *a, **kw):
                visited.append(dp)
                yield dp, dns, fns

        with mock.patch.object(discover_mod.os, "walk", spy):
            found = [r for r, _, _ in discover_mod.discover(flat)]
        self.assertEqual(found, ["a.py", "inner/b.py"])
        self.assertTrue(all(v == flat or v.startswith(flat + os.sep) for v in visited), visited)

    def test_discover_skips_binary_lock_empty_and_huge(self):
        root = self.project()
        self.put(root, {"a.py": "x = 1\n", "b.png": "x\n", "c.min.js": "x\n", "package-lock.json": "{}\n",
                        "empty.py": ""})
        with open(os.path.join(root, "huge.txt"), "w") as fh:
            fh.write("x" * (discover_mod.MAX_FILE_BYTES + 1))
        self.assertEqual([r for r, _, _ in discover_mod.discover(root)], ["a.py"])


# ------------------------------------------------------------------- map and search


def make_repo(test, n_mods=40):
    """core.py is imported everywhere; mqtt_client.py + mqtt_bridge.py form a small cluster."""
    root = test.project("big")
    files = {"core.py": "class Core:\n    def run(self):\n        return 1\n\n\ndef boot():\n    return Core()\n"}
    for i in range(n_mods):
        files[f"mod{i}.py"] = (
            "from core import Core\n\n\nclass Mod%d(Core):\n    def go(self, x, y=%d):\n        return x + y\n\n"
            "    def stop(self):\n        return None\n\n\ndef helper_%d(a, b):\n    return a * b\n\n\n"
            "def other_%d(value):\n    return value\n" % (i, i, i, i))
    files["mqtt_client.py"] = ("import mqtt_bridge\n\nclass MqttClient:\n    def connect(self, host, port=1883):\n"
                               "        return mqtt_bridge.open_socket(host, port)\n")
    files["mqtt_bridge.py"] = "def open_socket(host, port):\n    return (host, port)\n"
    files["tests/test_core.py"] = "from core import Core\n\n\ndef test_core():\n    assert Core().run() == 1\n"
    files["docs/guide.md"] = "# Guide\n\nSee [core](../core.py).\n"
    test.put(root, files)
    s = test.store(root)
    engine.sync(s, use_git=False)
    return root, s


class TestRepoMap(IndexCase):
    def test_map_is_plain_text_ranked_with_signatures(self):
        root, s = make_repo(self)
        m = engine.repo_map(s, tokens=1024)
        self.assertIsInstance(m, str)
        self.assertFalse(m.lstrip().startswith(("{", "[")))
        lines = m.splitlines()
        self.assertEqual(lines[0].rstrip(":"), "core.py")      # the hub ranks first
        self.assertIn("class Core", m)
        self.assertIn("def boot()", m)
        self.assertIn("def go(self, x, y=", m)

    def test_budget_is_respected_within_15_percent(self):
        root, s = make_repo(self, n_mods=80)
        for tokens in (256, 512, 1024, 2048):
            m = engine.repo_map(s, tokens=tokens)
            target = tokens * 3.5
            self.assertLessEqual(len(m), target * 1.15, tokens)
            self.assertGreaterEqual(len(m), target * 0.85, tokens)

    def test_small_repo_returns_everything_when_budget_is_large(self):
        root, s = make_repo(self, n_mods=3)
        m = engine.repo_map(s, tokens=20000)
        for f in ("core.py", "mod0.py", "mod2.py", "mqtt_client.py", "tests/test_core.py"):
            self.assertIn(f, m)
        self.assertEqual(engine.repo_map(self.store(self.project("empty")), tokens=512), "")

    def test_focus_files_and_query_pull_related_files_up(self):
        root, s = make_repo(self, n_mods=60)
        plain = engine.repo_map(s, tokens=300)
        self.assertNotIn("mqtt_client.py", plain)
        self.assertNotIn("mod37.py", plain)
        lone = engine.repo_map(s, focus_files=["mod37.py"], tokens=300)
        self.assertEqual(lone.splitlines()[0], "mod37.py:")
        focus = engine.repo_map(s, focus_files=["mqtt_client.py"], tokens=300)
        heads = [ln[:-1] for ln in focus.splitlines() if ln.endswith(".py:")]
        self.assertEqual(heads[:2], ["mqtt_client.py", "mqtt_bridge.py"])
        by_query = engine.repo_map(s, query="how does the mqtt socket connect", tokens=300)
        heads = [ln[:-1] for ln in by_query.splitlines() if ln.endswith(".py:")]
        self.assertTrue({"mqtt_client.py", "mqtt_bridge.py"} <= set(heads[:3]), heads)
        absolute = engine.repo_map(s, focus_files=[os.path.join(root, "mqtt_client.py")], tokens=300)
        self.assertEqual(absolute, focus)
        self.assertEqual(engine.repo_map(s, focus_files=["nope.py", "/etc/passwd"], tokens=300), plain)

    def test_map_resolves_pending_touches_first(self):
        root, s = make_repo(self, n_mods=5)
        self.put(root, {"late.py": "from mqtt_bridge import open_socket\n\n\ndef late():\n    return open_socket(1, 2)\n"})
        engine.touch(s, ["late.py"])
        self.assertEqual(s.get_meta("resolve_dirty"), "1")
        m = engine.repo_map(s, focus_files=["late.py"], tokens=400)
        self.assertEqual(s.get_meta("resolve_dirty"), "0")
        self.assertIn("mqtt_bridge.py", m)


class TestSearchCode(IndexCase):
    def test_results_shape_and_order(self):
        root, s = make_repo(self, n_mods=5)
        hits = engine.search_code(s, "mqtt")
        self.assertTrue(hits)
        for h in hits:
            self.assertEqual(set(h), {"id", "kind", "name", "path", "line", "sig", "score"})
            self.assertIsInstance(h["score"], float)
        scores = [h["score"] for h in hits]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertTrue({h["path"] for h in hits} <= {"mqtt_client.py", "mqtt_bridge.py"})
        self.assertNotIn("dir", {h["kind"] for h in hits})
        self.assertNotIn("package", {h["kind"] for h in hits})

    def test_exact_symbol_match_is_first_and_limit_applies(self):
        root, s = make_repo(self, n_mods=30)
        hits = engine.search_code(s, "MqttClient", limit=3)
        self.assertLessEqual(len(hits), 3)
        self.assertEqual((hits[0]["name"], hits[0]["kind"], hits[0]["path"]), ("MqttClient", "class", "mqtt_client.py"))
        self.assertEqual(hits[0]["line"], 3)
        self.assertIn("class MqttClient", hits[0]["sig"])
        self.assertEqual(len(engine.search_code(s, "helper", limit=5)), 5)
        self.assertEqual(engine.search_code(s, "zzzznotthere"), [])
        self.assertEqual(engine.search_code(s, ""), [])

    def test_search_works_unresolved_and_subwords(self):
        root = self.project()
        self.put(root, {"a.py": "def get_user_token():\n    return 1\n"})
        s = self.store(root)
        indexer_mod.Indexer(s).scan()          # parsed but never resolved
        hits = engine.search_code(s, "user token")
        self.assertEqual(hits[0]["name"], "get_user_token")

    def test_search_without_fts5_falls_back_to_like(self):
        root = self.project()
        self.put(root, {"a.py": "def get_user_token():\n    return 1\n"})
        s = Store(root, create=True, fts=False)
        self.addCleanup(s.close)
        engine.sync(s, use_git=False)
        self.assertEqual(engine.search_code(s, "token")[0]["name"], "get_user_token")


if __name__ == "__main__":
    unittest.main()
