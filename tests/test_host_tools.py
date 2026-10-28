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

    def test_start_is_idempotent_when_already_running(self):
        with patch.object(nixvm, "domain_state", return_value="running"), \
             patch.object(nixvm, "virsh") as virsh:
            nixvm.start_vm({"name": "devvm"})
        virsh.assert_not_called()

    def test_start_starts_a_stopped_vm_and_waits_for_running(self):
        events = []
        with patch.object(nixvm, "domain_state", side_effect=["shut off", "running"]), \
             patch.object(nixvm, "virsh", side_effect=lambda _c, *args: events.append(args)), \
             patch.object(nixvm.time, "monotonic", return_value=0):
            nixvm.start_vm({"name": "devvm"})
        self.assertEqual(events, [("start", "devvm")])

    def test_stop_requests_graceful_shutdown_and_waits(self):
        events = []
        with patch.object(nixvm, "domain_state", side_effect=["running", "shut off"]), \
             patch.object(nixvm, "virsh", side_effect=lambda _c, *args: events.append(args)), \
             patch.object(nixvm.time, "monotonic", return_value=0):
            nixvm.stop_vm({"name": "devvm"})
        self.assertEqual(events, [("shutdown", "devvm")])

    def test_graceful_stop_timeout_does_not_force_destroy(self):
        with patch.object(nixvm, "domain_state", return_value="running"), \
             patch.object(nixvm, "virsh") as virsh, \
             patch.object(nixvm, "wait_domain_state", side_effect=RuntimeError("timeout")):
            with self.assertRaisesRegex(RuntimeError, "stop --force"):
                nixvm.stop_vm({"name": "devvm"})
        virsh.assert_called_once_with({"name": "devvm"}, "shutdown", "devvm")

    def test_force_stop_is_explicit(self):
        events = []
        with patch.object(nixvm, "domain_state", return_value="running"), \
             patch.object(nixvm, "virsh", side_effect=lambda _c, *args: events.append(args)), \
             patch.object(nixvm, "wait_domain_state") as wait:
            nixvm.stop_vm({"name": "devvm"}, force=True)
        self.assertEqual(events, [("destroy", "devvm")])
        wait.assert_called_once_with({"name": "devvm"}, "shut off", 30)

    def test_test_key_is_excluded_from_repo_transfers(self):
        self.assertTrue(nixvm.excluded("nixvm_test_ed25519"))
        self.assertTrue(nixvm.excluded("nixvm_test_ed25519.pub"))

    def test_ssh_identity_environment_override(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(nixvm, "ROOT", Path(tmp)), \
             patch.object(nixvm, "guest_ip", return_value="192.0.2.12"), \
             patch.dict(os.environ, {"NIXVM_SSH_IDENTITY": "/repo/nixvm_test_ed25519"}):
            args = nixvm.ssh_args({"name": "devvm", "username": "test", "identity": ""})
        self.assertIn("/repo/nixvm_test_ed25519", args)
        self.assertIn("IdentitiesOnly=yes", args)

    def test_bootstrap_resets_only_guest_runtime_host_key(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(nixvm, "ROOT", Path(tmp)), \
             patch.object(nixvm, "guest_ip", return_value="192.0.2.12"), \
             patch.object(nixvm, "run") as run:
            nixvm.reset_guest_host_key({"name": "devvm"})
        run.assert_called_once_with([
            "ssh-keygen", "-R", "192.0.2.12", "-f", Path(tmp) / ".runtime/devvm/known_hosts"
        ], capture=True)

    def test_reboot_guest_waits_for_disconnect_reconnect_and_gnome(self):
        errors = [subprocess.CalledProcessError(255, ["ssh"])]
        with patch.object(nixvm, "remote", side_effect=[None, *errors, None, None]) as remote, \
             patch.object(nixvm, "wait_ssh") as wait_ssh, \
             patch.object(nixvm.time, "monotonic", side_effect=[0, 1, 2]), \
             patch.object(nixvm.time, "sleep"):
            nixvm.reboot_guest({"name": "devvm"}, timeout=20)
        self.assertEqual(remote.call_args_list[0].args[1],
                         "sudo systemd-run --quiet --on-active=2s --collect "
                         "/run/current-system/sw/bin/systemctl reboot")
        wait_ssh.assert_called_once_with({"name": "devvm"}, 18)
        self.assertEqual(remote.call_args_list[-1].args[1],
                         "systemctl is-active graphical.target && "
                         "systemctl is-active display-manager.service")

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
