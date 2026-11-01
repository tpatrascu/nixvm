import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
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
    def test_mitmproxy_domain_allowlist_overrides_blocklist_with_glob_and_regex(self):
        with tempfile.TemporaryDirectory() as directory:
            policy_file = Path(directory) / "policy.json"
            policy_file.write_text(json.dumps({
                "blocked_domains": ["glob:*.example.com", "regex:telemetry[0-9]+\\.example\\.net"],
                "allowed_domains": ["regex:api[0-9]+\\.example\\.com", "trusted.example.net"],
                "deny_cidrs": [],
            }), encoding="utf-8")
            mitmproxy_stub = types.ModuleType("mitmproxy")
            mitmproxy_stub.ctx = types.SimpleNamespace(
                log=types.SimpleNamespace(warn=lambda _message: None)
            )
            mitmproxy_stub.http = types.SimpleNamespace(
                HTTPFlow=object, Response=types.SimpleNamespace(make=lambda *_args, **_kwargs: None)
            )
            addon_path = Path(__file__).parents[1] / "scripts" / "mitmproxy_policy.py"
            spec = importlib.util.spec_from_file_location("nixvm_test_mitmproxy_policy", addon_path)
            addon = importlib.util.module_from_spec(spec)
            with patch.dict(sys.modules, {"mitmproxy": mitmproxy_stub}), \
                 patch.dict(os.environ, {"NIXVM_MITMPROXY_POLICY": str(policy_file)}):
                spec.loader.exec_module(addon)
                policy = addon.NixvmPolicy()
        self.assertTrue(policy.domain_is_blocked("img.example.com"))
        self.assertFalse(policy.domain_is_blocked("api42.example.com"))
        self.assertTrue(policy.domain_is_blocked("telemetry9.example.net"))
        self.assertFalse(policy.domain_is_blocked("cdn.trusted.example.net"))

    def test_inspection_hosts_accept_glob_and_regex_patterns(self):
        patterns = nixvm.mitmproxy_allow_hosts([
            "glob:*.inspect.example.net",
            r"regex:api[0-9]+\.example\.com",
        ])
        self.assertRegex("img.inspect.example.net:443", patterns)
        self.assertRegex("api23.example.com:443", patterns)
        self.assertNotRegex("other.example.org:443", patterns)

    def test_mitmproxy_ca_is_initialized_in_the_persistent_shared_volume(self):
        with patch.object(nixvm, "run") as run:
            nixvm.initialize_mitmproxy_ca("docker.io/mitmproxy/mitmproxy:latest")
        run.assert_called_once_with([
            "podman", "run", "--rm", "--volume",
            "nixvm-shared-mitmproxy-data:/home/mitmproxy/.mitmproxy",
            "--entrypoint", "mitmdump", "docker.io/mitmproxy/mitmproxy:latest",
            "--set", "confdir=/home/mitmproxy/.mitmproxy",
            "--no-server", "--rfile", "/dev/null",
        ])

    def test_hosts_file_is_unchanged_when_hostname_already_has_current_ip(self):
        hosts = "192.168.124.13 nixvm developer-alias # maintained by user\n"
        updated, changed = nixvm.hosts_text_with_vm_name(
            hosts, "192.168.124.13", "nixvm"
        )
        self.assertEqual(updated, hosts)
        self.assertFalse(changed)

    def test_remove_managed_hostname_preserves_unmanaged_mappings(self):
        hosts = (
            "127.0.0.1 localhost\n"
            "192.168.124.13 nixvm # Managed by nixvm\n"
            "192.0.2.5 nixvm manual-alias\n"
            "198.51.100.7 nixvm extra # Managed by nixvm\n"
        )
        updated, changed = nixvm.remove_vm_hosts_entry(hosts, "nixvm")
        self.assertTrue(changed)
        self.assertEqual(updated, (
            "127.0.0.1 localhost\n"
            "192.0.2.5 nixvm manual-alias\n"
            "198.51.100.7 extra\n"
        ))

    def test_remove_managed_hostname_is_idempotent(self):
        hosts = "127.0.0.1 localhost\n"
        updated, changed = nixvm.remove_vm_hosts_entry(hosts, "nixvm")
        self.assertEqual(updated, hosts)
        self.assertFalse(changed)

    def test_lease_lookup_matches_ipv4(self):
        with patch.object(nixvm, "virsh", return_value="52:54:00:dc:10:01 ipv4 192.168.122.42/24 nixvm -"):
            self.assertEqual(nixvm.guest_ip({"network": "default", "mac": "52:54:00:dc:10:01"}), "192.168.122.42")

    def test_no_lease_is_retryable(self):
        with patch.object(nixvm, "virsh", return_value="Expiry Time MAC Protocol IP Hostname"):
            with self.assertRaises(RuntimeError):
                nixvm.guest_ip({"network": "default", "mac": "52:54:00:dc:10:01"})

    def test_start_is_idempotent_when_already_running(self):
        with patch.object(nixvm, "domain_state", return_value="running"), \
             patch.object(nixvm, "virsh") as virsh:
            nixvm.start_vm({"name": "nixvm"})
        virsh.assert_not_called()

    def test_start_starts_a_stopped_vm_and_waits_for_running(self):
        events = []
        with patch.object(nixvm, "domain_state", side_effect=["shut off", "running"]), \
             patch.object(nixvm, "virsh", side_effect=lambda _c, *args: events.append(args)), \
             patch.object(nixvm.time, "monotonic", return_value=0):
            nixvm.start_vm({"name": "nixvm"})
        self.assertEqual(events, [("start", "nixvm")])

    def test_stop_requests_graceful_shutdown_and_waits(self):
        events = []
        with patch.object(nixvm, "domain_state", side_effect=["running", "shut off"]), \
             patch.object(nixvm, "virsh", side_effect=lambda _c, *args: events.append(args)), \
             patch.object(nixvm.time, "monotonic", return_value=0):
            nixvm.stop_vm({"name": "nixvm"})
        self.assertEqual(events, [("shutdown", "nixvm")])

    def test_graceful_stop_timeout_does_not_force_destroy(self):
        with patch.object(nixvm, "domain_state", return_value="running"), \
             patch.object(nixvm, "virsh") as virsh, \
             patch.object(nixvm, "wait_domain_state", side_effect=RuntimeError("timeout")):
            with self.assertRaisesRegex(RuntimeError, "stop --force"):
                nixvm.stop_vm({"name": "nixvm"})
        virsh.assert_called_once_with({"name": "nixvm"}, "shutdown", "nixvm")

    def test_force_stop_is_explicit(self):
        events = []
        with patch.object(nixvm, "domain_state", return_value="running"), \
             patch.object(nixvm, "virsh", side_effect=lambda _c, *args: events.append(args)), \
             patch.object(nixvm, "wait_domain_state") as wait:
            nixvm.stop_vm({"name": "nixvm"}, force=True)
        self.assertEqual(events, [("destroy", "nixvm")])
        wait.assert_called_once_with({"name": "nixvm"}, "shut off", 30)

    def test_ssh_identity_environment_override(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(nixvm, "ROOT", Path(tmp)), \
             patch.object(nixvm, "guest_ip", return_value="192.0.2.12"), \
             patch.dict(os.environ, {"NIXVM_SSH_IDENTITY": "/repo/nixvm_test_ed25519"}):
            args = nixvm.ssh_args({"name": "nixvm", "username": "test", "identity": ""})
        self.assertIn("/repo/nixvm_test_ed25519", args)
        self.assertIn("IdentitiesOnly=yes", args)

    def test_bootstrap_resets_only_guest_runtime_host_key(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(nixvm, "ROOT", Path(tmp)), \
             patch.object(nixvm, "guest_ip", return_value="192.0.2.12"), \
             patch.object(nixvm, "run") as run:
            nixvm.reset_guest_host_key({"name": "nixvm"})
        run.assert_called_once_with([
            "ssh-keygen", "-R", "192.0.2.12", "-f", Path(tmp) / ".runtime/nixvm/known_hosts"
        ], capture=True)

    def test_reboot_guest_waits_for_disconnect_reconnect_and_gnome(self):
        errors = [subprocess.CalledProcessError(255, ["ssh"])]
        with patch.object(nixvm, "remote", side_effect=[None, *errors, None, None]) as remote, \
             patch.object(nixvm, "wait_ssh") as wait_ssh, \
             patch.object(nixvm.time, "monotonic", side_effect=[0, 1, 2]), \
             patch.object(nixvm.time, "sleep"):
            nixvm.reboot_guest({"name": "nixvm"}, timeout=20)
        self.assertEqual(remote.call_args_list[0].args[1],
                         "sudo systemd-run --quiet --on-active=2s --collect "
                         "/run/current-system/sw/bin/systemctl reboot")
        wait_ssh.assert_called_once_with({"name": "nixvm"}, 18)
        self.assertEqual(remote.call_args_list[-1].args[1],
                         "systemctl is-active graphical.target && "
                         "systemctl is-active display-manager.service")

    def test_usb_detach_returns_key_before_ssh_authentication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            saved = root / ".runtime/nixvm/yubikey.xml"
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
                nixvm.usb({"name": "nixvm"}, False)
            self.assertEqual(events, ["dumpxml", "detach-device", "ssh"])
            self.assertFalse(saved.exists())


if __name__ == "__main__":
    unittest.main()
