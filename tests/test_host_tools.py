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

    def tearDown(self):
        self.tmp.cleanup()

    def test_retry_never_overwrites_existing_guest_checkout(self):
        with patch.object(nixvm, "ssh_args", return_value=["ssh", "guest"]), \
             patch.object(nixvm, "output", return_value="yes"):
            nixvm.copy_checkout({}, self.repo, "/home/user/config")

    def test_partial_unknown_destination_is_rejected(self):
        with patch.object(nixvm, "ssh_args", return_value=["ssh", "guest"]), \
             patch.object(nixvm, "output", return_value=""), \
             patch.object(nixvm, "remote", side_effect=RuntimeError("exists")):
            with self.assertRaises(RuntimeError):
                nixvm.copy_checkout({}, self.repo, "/home/user/config")


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
