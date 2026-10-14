import os
import sqlite3

from agres import paths
from tests.helpers import TempHome


def make_store_file(root):
    d = os.path.join(root, ".agres")
    os.makedirs(d, exist_ok=True)
    sqlite3.connect(os.path.join(d, "agres.db")).close()


class FindRootTest(TempHome):
    def test_git_root_from_nested_dir(self):
        repo = self.git_repo("work", "okas")
        sub = self.mkdir("work", "okas", "packages", "pulsar")
        self.assertEqual(paths.find_root(sub), repo)

    def test_git_file_marks_a_worktree_root(self):
        wt = self.mkdir("work", "okas-wt")
        with open(os.path.join(wt, ".git"), "w") as fh:
            fh.write("gitdir: /elsewhere\n")
        self.assertEqual(paths.find_root(os.path.join(wt)), wt)

    def test_nearest_store_wins_over_outer_git(self):
        repo = self.git_repo("work", "zeus")
        topic = self.mkdir("work", "zeus", "topics", "1.4")
        make_store_file(topic)
        self.assertEqual(paths.find_root(topic), topic)
        self.assertEqual(paths.find_root(os.path.join(repo, "topics")), repo)

    def test_home_is_never_a_root(self):
        os.makedirs(os.path.join(self.home, ".git"))
        make_store_file(self.home)  # the old global ~/.agres/agres.db
        self.assertIsNone(paths.find_root(self.home))
        loose = self.mkdir("Downloads", "x")
        self.assertIsNone(paths.find_root(loose))

    def test_filesystem_root_is_never_a_root(self):
        self.assertIsNone(paths.find_root("/"))

    def test_plain_directory_has_no_root(self):
        self.assertIsNone(paths.find_root(self.mkdir("work", "scratch")))

    def test_env_override(self):
        target = self.mkdir("work", "explicit")
        os.environ["AGRES_ROOT"] = target
        self.assertEqual(paths.find_root(self.mkdir("elsewhere")), target)

    def test_forbidden_roots(self):
        self.assertTrue(paths.is_forbidden_root("/"))
        self.assertTrue(paths.is_forbidden_root(self.home))
        self.assertTrue(paths.is_forbidden_root(os.path.dirname(self.home)))
        self.assertFalse(paths.is_forbidden_root(self.mkdir("work", "p")))

    def test_db_path(self):
        root = self.git_repo("work", "p")
        self.assertEqual(paths.db_path(root), os.path.join(root, ".agres", "agres.db"))


class GitRootTest(TempHome):
    def test_git_root_skips_nested_stores(self):
        repo = self.git_repo("work", "okas")
        sub = self.mkdir("work", "okas", "packages", "pulsar")
        make_store_file(sub)
        self.assertEqual(paths.find_root(sub), sub)
        self.assertEqual(paths.git_root(sub), repo)
        self.assertIsNone(paths.git_root(self.mkdir("loose")))
