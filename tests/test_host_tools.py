import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("nixvm", Path(__file__).parents[1] / "scripts/nixvm.py")
nixvm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nixvm)


class RepositoryTransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "source"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "tracked").write_text("original\n")
        (self.repo / "deleted").write_text("delete me\n")
        (self.repo / ".gitignore").write_text("ignored\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Initial")
        self.git("remote", "add", "origin", "git@example.invalid:user/config.git")

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True).strip()

    def test_copy_preserves_history_edits_deletions_remotes_and_excludes_secrets(self):
        (self.repo / "tracked").write_text("dirty edit\n")
        (self.repo / "deleted").unlink()
        (self.repo / "new").write_text("untracked\n")
        (self.repo / "ignored").write_text("ignored\n")
        (self.repo / ".env").write_text("SECRET=hidden\n")
        (self.repo / ".ssh").mkdir()
        (self.repo / ".ssh/id_ed25519").write_text("private\n")
        (self.repo / ".ssh/config").write_text("Host example\n")
        dest = self.root / "destination"
        nixvm.stage_repository(self.repo, dest)
        self.assertEqual((dest / "tracked").read_text(), "dirty edit\n")
        self.assertFalse((dest / "deleted").exists())
        self.assertEqual((dest / "new").read_text(), "untracked\n")
        self.assertFalse((dest / ".env").exists())
        self.assertFalse((dest / ".ssh/id_ed25519").exists())
        self.assertTrue((dest / ".ssh/config").exists())
        self.assertFalse((dest / "ignored").exists())
        self.assertEqual(nixvm.output(["git", "-C", dest, "rev-parse", "HEAD"]), self.git("rev-parse", "HEAD"))
        self.assertEqual(nixvm.output(["git", "-C", dest, "remote", "get-url", "origin"]),
                         "git@example.invalid:user/config.git")

    def test_worktree_becomes_independent_git_directory(self):
        worktree = self.root / "worktree"
        self.git("worktree", "add", "-qb", "vm", str(worktree))
        dest = self.root / "destination"
        nixvm.stage_repository(worktree, dest)
        self.assertTrue((dest / ".git").is_dir())
        self.assertEqual(nixvm.output(["git", "-C", dest, "branch", "--show-current"]), "vm")
        self.git("worktree", "remove", str(worktree))
        self.assertEqual(nixvm.output(["git", "-C", dest, "show", "HEAD:tracked"]), "original")

    def test_other_local_branches_remain_reachable(self):
        self.git("branch", "other")
        dest = self.root / "destination"
        nixvm.stage_repository(self.repo, dest)
        self.assertEqual(nixvm.output(["git", "-C", dest, "rev-parse", "other"]), self.git("rev-parse", "other"))

    def test_embedded_remote_credentials_are_not_copied(self):
        self.git("remote", "set-url", "origin", "https://user:fake-secret@example.invalid/repo.git")
        dest = self.root / "destination"
        with self.assertRaisesRegex(ValueError, "embedded HTTPS credentials"):
            nixvm.stage_repository(self.repo, dest)
        self.assertFalse(dest.exists())

    def test_dotfiles_adapter_is_idempotent_and_does_not_change_host(self):
        original = ('export ZSH="$HOME/.oh-my-zsh"\nsource $ZSH/oh-my-zsh.sh\n'
                    'PKCS11_MODULE="/usr/lib/x86_64-linux-gnu/opensc-pkcs11.so"\n'
                    'eval "$(/home/linuxbrew/.linuxbrew/bin/brew shellenv zsh)"\n')
        (self.repo / ".zshrc").write_text(original)
        dest = self.root / "destination"
        nixvm.stage_repository(self.repo, dest, dotfiles=True)
        adjusted = (dest / ".zshrc").read_text()
        self.assertIn("NIXVM_ZSH_MANAGED", adjusted)
        self.assertIn("${PKCS11_MODULE:-", adjusted)
        self.assertEqual(nixvm.portable_zsh(adjusted), adjusted)
        self.assertEqual((self.repo / ".zshrc").read_text(), original)

    def test_dotfiles_adapter_never_rewrites_a_symlink_target_on_host(self):
        outside = self.root / "host-zshrc"
        outside.write_text('export ZSH="$HOME/.oh-my-zsh"\n')
        (self.repo / ".zshrc").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "regular file"):
            nixvm.stage_repository(self.repo, self.root / "destination", dotfiles=True)
        self.assertEqual(outside.read_text(), 'export ZSH="$HOME/.oh-my-zsh"\n')

    def test_retry_never_overwrites_existing_guest_checkout(self):
        with patch.object(nixvm, "ssh_args", return_value=["ssh", "guest"]), \
             patch.object(nixvm, "output", return_value="yes"), \
             patch.object(nixvm, "stage_repository") as stage:
            nixvm.copy_checkout({}, self.repo, "/home/user/config")
        stage.assert_not_called()

    def test_partial_unknown_destination_is_rejected(self):
        with patch.object(nixvm, "ssh_args", return_value=["ssh", "guest"]), \
             patch.object(nixvm, "output", return_value=""), \
             patch.object(nixvm, "remote", side_effect=RuntimeError("exists")), \
             patch.object(nixvm, "stage_repository") as stage:
            with self.assertRaises(RuntimeError):
                nixvm.copy_checkout({}, self.repo, "/home/user/config")
        stage.assert_not_called()


class HostTests(unittest.TestCase):
    def test_lease_lookup_matches_ipv4(self):
        with patch.object(nixvm, "virsh", return_value="52:54:00:dc:10:01 ipv4 192.168.122.42/24 devvm -"):
            self.assertEqual(nixvm.guest_ip({"network": "default", "mac": "52:54:00:dc:10:01"}), "192.168.122.42")

    def test_no_lease_is_retryable(self):
        with patch.object(nixvm, "virsh", return_value="Expiry Time MAC Protocol IP Hostname"):
            with self.assertRaises(RuntimeError):
                nixvm.guest_ip({"network": "default", "mac": "52:54:00:dc:10:01"})

    def test_usb_detach_returns_key_before_ssh_authentication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            saved = root / ".runtime/devvm/yubikey.xml"
            saved.parent.mkdir(parents=True)
            saved.write_text('<hostdev type="usb"><source><address bus="1" device="3"/></source></hostdev>')
            events = []

            def virsh(_connection, *args):
                events.append(args[0])
                if args[0] == "dumpxml":
                    return '<domain><devices>' + saved.read_text() + '</devices></domain>'
                return ""

            with patch.object(nixvm, "ROOT", root), patch.object(nixvm, "virsh", side_effect=virsh), \
                 patch.object(nixvm, "remote", side_effect=lambda *_: events.append("ssh")):
                nixvm.usb({"name": "devvm"}, False)
            self.assertEqual(events, ["dumpxml", "detach-device", "ssh"])
            self.assertFalse(saved.exists())


if __name__ == "__main__":
    unittest.main()
