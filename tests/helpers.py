"""Shared test scaffolding: throwaway HOME and project trees."""
import os
import shutil
import tempfile
import unittest
from unittest import mock


class TempHome(unittest.TestCase):
    """Each test gets a fake $HOME with `work/` beneath it for projects."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="agres-test-"))
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        env = {"HOME": self.home}
        for k in ("AGRES_ROOT", "AGRES_SESSION"):
            env[k] = ""
        self._env = mock.patch.dict(os.environ, env)
        self._env.start()
        for k in ("AGRES_ROOT", "AGRES_SESSION"):
            os.environ.pop(k, None)

    def tearDown(self):
        self._env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def mkdir(self, *parts):
        p = os.path.join(self.home, *parts)
        os.makedirs(p, exist_ok=True)
        return p

    def git_repo(self, *parts):
        """A directory that looks like a git checkout (a `.git` dir is enough for root lookup)."""
        p = self.mkdir(*parts)
        os.makedirs(os.path.join(p, ".git"), exist_ok=True)
        return p

    def write(self, root, rel, text):
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        return p
