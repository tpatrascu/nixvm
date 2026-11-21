import importlib.util
import ipaddress
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
    def test_guest_host_ca_cleanup_is_safe_with_zsh_unmatched_globs(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(nixvm, "host_ca_cache_dir", return_value=Path(directory)), \
             patch.object(nixvm, "remote") as remote:
            nixvm.sync_guest_host_ca({"guest_config": {"repoPath": "/home/user/nixvm"}})
        command = remote.call_args.args[1]
        self.assertIn("find /home/user/nixvm/nix/machines/host-ca", command)
        self.assertIn("-name 'host-ca-*.pem' -delete", command)
        self.assertNotIn("rm -f", command)

    def test_host_ca_source_directories_follow_distribution(self):
        expected = {
            "debian": "/usr/local/share/ca-certificates",
            "fedora": "/etc/pki/ca-trust/source/anchors",
            "arch": "/etc/ca-certificates/trust-source/anchors",
            "opensuse-tumbleweed": "/etc/pki/trust/anchors",
        }
        for distro, directory in expected.items():
            with self.subTest(distro=distro):
                paths = nixvm.host_ca_source_directories(
                    {"ID": distro, "ID_LIKE": ""}
                )
                self.assertIn(Path(directory), paths)

    def test_host_ca_source_directories_accept_custom_paths(self):
        with patch.dict(os.environ, {"NIXVM_HOST_CA_DIRS": "/custom/one:/custom/two"}):
            paths = nixvm.host_ca_source_directories({"ID": "nixos", "ID_LIKE": ""})
        self.assertEqual(paths[-2:], [Path("/custom/one"), Path("/custom/two")])

    def test_ca_parser_ignores_non_certificate_content(self):
        self.assertEqual(nixvm.certificate_pem_blocks(b"private key or text", ".pem"), [])

    def test_ufw_rule_reconciliation_keeps_exact_matches(self):
        status = "\n".join([
            "Status: active",
            "[ 1] 192.168.124.1 53/udp ALLOW IN 192.168.124.0/24 # nixvm-nixvm",
            "[ 2] 192.168.124.1 53/tcp ALLOW IN 192.168.124.0/24 # nixvm-nixvm",
            "[ 3] 192.168.124.1 8080/tcp ALLOW IN 192.168.124.0/24 # nixvm-nixvm",
            "[ 4] 192.168.124.1 2049/tcp ALLOW IN 192.168.124.0/24 # nixvm-nixvm",
            "[ 5] 192.168.124.1 9999/tcp ALLOW IN 192.168.124.0/24 # nixvm-nixvm",
            "[ 6] 192.168.124.1 80/tcp ALLOW IN 192.168.124.0/24 # other-rule",
        ])
        existing = nixvm.parse_ufw_managed_rules(status, "nixvm-nixvm")
        desired = [
            ("192.168.124.0/24", "192.168.124.1", 53, "udp"),
            ("192.168.124.0/24", "192.168.124.1", 53, "tcp"),
            ("192.168.124.0/24", "192.168.124.1", 8080, "tcp"),
            ("192.168.124.0/24", "192.168.124.1", 2049, "tcp"),
            ("192.168.124.0/24", "192.168.124.1", 443, "tcp"),
        ]
        remove, add = nixvm.reconcile_ufw_rules(existing, desired)
        self.assertEqual(remove, [5])
        self.assertEqual(add, [("192.168.124.0/24", "192.168.124.1", 443, "tcp")])

    def test_ufw_rules_are_not_touched_when_already_current(self):
        status = "[ 8] 192.168.124.1 53/udp ALLOW IN 192.168.124.0/24 # nixvm-nixvm"
        existing = nixvm.parse_ufw_managed_rules(status, "nixvm-nixvm")
        desired = [("192.168.124.0/24", "192.168.124.1", 53, "udp")]
        self.assertEqual(nixvm.reconcile_ufw_rules(existing, desired), ([], []))

    def test_guest_access_waits_up_to_two_minutes_for_dhcp_and_ssh(self):
        events = []
        with patch.object(nixvm, "wait_guest_ip",
                          side_effect=lambda _c, timeout: events.append(("ip", timeout))), \
             patch.object(nixvm, "wait_ssh",
                          side_effect=lambda _c, timeout: events.append(("ssh", timeout))):
            nixvm.wait_for_guest_access({})
        self.assertEqual(events, [("ip", 120), ("ssh", 120)])

    def test_mitmproxy_domain_allowlist_overrides_blocklist_with_glob_and_regex(self):
        with tempfile.TemporaryDirectory() as directory:
            policy_file = Path(directory) / "policy.json"
            policy_file.write_text(json.dumps({
                "blocked_domains": ["glob:*.example.com", "regex:telemetry[0-9]+\\.example\\.net"],
                "allowed_domains": [
                    "regex:api[0-9]+\\.example\\.com", "trusted.example.net", "git.example.com",
                ],
                "deny_cidrs": ["192.168.0.0/16"],
                "allowed_internal_hosts": [{
                    "hostname": "git.example.com",
                    "ips": ["192.168.50.10"],
                    "ports": [443],
                }, {
                    "hostname": "192.168.124.1",
                    "ips": ["192.168.124.1"],
                    "ports": [9999],
                }],
            }), encoding="utf-8")
            mitmproxy_stub = types.ModuleType("mitmproxy")
            mitmproxy_stub.ctx = types.SimpleNamespace(
                log=types.SimpleNamespace(warn=lambda _message: None)
            )
            mitmproxy_stub.http = types.SimpleNamespace(
                HTTPFlow=object,
                Response=types.SimpleNamespace(
                    make=lambda status, content, _headers: types.SimpleNamespace(
                        status_code=status, content=content
                    )
                ),
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
        with patch.object(
            policy, "host_interface_addresses",
            return_value=({"192.168.124.1"}, None),
        ):
            host_endpoint, error = policy.resolve_and_pin("192.168.124.1", 9999)
            self.assertEqual(host_endpoint, "192.168.124.1")
            self.assertIsNone(error)
            blocked_host_port, error = policy.resolve_and_pin("192.168.124.1", 8080)
            self.assertIsNone(blocked_host_port)
            self.assertIn("not allowed", error)
        with patch.object(
            policy, "host_global_ipv6_networks",
            return_value=({ipaddress.ip_network("2a02:2f04:9406:d900::/64")}, None),
        ):
            self.assertTrue(policy.address_is_denied("2a02:2f04:9406:d900::1234"))
            self.assertFalse(policy.address_is_denied("2606:4700:4700::1111"))
        with patch.object(policy, "host_global_ipv6_networks", return_value=(None, "unavailable")):
            self.assertTrue(policy.address_is_denied("2606:4700:4700::1111"))
        flow_data = types.SimpleNamespace(server=types.SimpleNamespace(
            id="git-server", address=("git.example.com", 443), tls=True, sni=None, error=None,
        ))
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("192.168.50.10", 443)),
        ]):
            policy.server_connect(flow_data)
        self.assertEqual(flow_data.server.address, ("192.168.50.10", 443))
        self.assertEqual(flow_data.server.sni, "git.example.com")

        wrong_port, error = policy.resolve_and_pin("git.example.com", 8443)
        self.assertIsNone(wrong_port)
        self.assertIn("not allowed", error)
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("192.168.50.11", 443)),
        ]):
            wrong_address, error = policy.resolve_and_pin("git.example.com", 443)
        self.assertIsNone(wrong_address)
        self.assertIn("not allowed", error)
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("192.168.50.10", 443)),
        ]):
            child_host, error = policy.resolve_and_pin("child.git.example.com", 443)
        self.assertIsNone(child_host)
        self.assertIn("denied internal address", error)

        allowed_tls = types.SimpleNamespace(
            client_hello=types.SimpleNamespace(sni="git.example.com"),
            context=types.SimpleNamespace(
                client=types.SimpleNamespace(error=None),
                server=types.SimpleNamespace(id="git-server", address=("192.168.50.10", 443)),
            ),
        )
        denied_tls = types.SimpleNamespace(
            client_hello=types.SimpleNamespace(sni="console.internal"),
            context=types.SimpleNamespace(
                client=types.SimpleNamespace(error=None),
                server=types.SimpleNamespace(id="console-server", address=("192.168.50.10", 443)),
            ),
        )
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("192.168.50.10", 443)),
        ]):
            policy.tls_clienthello(allowed_tls)
            policy.tls_clienthello(denied_tls)
        self.assertIsNone(allowed_tls.context.client.error)
        self.assertIn("denied internal address", denied_tls.context.client.error)

        allowed_request = types.SimpleNamespace(
            request=types.SimpleNamespace(host="git.example.com", port=443),
            client_conn=types.SimpleNamespace(sni="git.example.com"),
            server_conn=types.SimpleNamespace(
                id="git-server", address=("192.168.50.10", 443)
            ),
            response=None,
        )
        policy.request(allowed_request)
        self.assertIsNone(allowed_request.response)

        pinned_ip_request = types.SimpleNamespace(
            request=types.SimpleNamespace(host="192.168.50.10", port=443),
            client_conn=types.SimpleNamespace(sni="git.example.com"),
            server_conn=types.SimpleNamespace(
                id="git-server", address=("192.168.50.10", 443)
            ),
            response=None,
        )
        policy.request(pinned_ip_request)
        self.assertIsNone(pinned_ip_request.response)

        no_sni_request = types.SimpleNamespace(
            request=types.SimpleNamespace(host="192.168.50.10", port=443),
            client_conn=types.SimpleNamespace(sni=None),
            server_conn=types.SimpleNamespace(
                id="git-server", address=("192.168.50.10", 443)
            ),
            response=None,
        )
        policy.request(no_sni_request)
        self.assertEqual(no_sni_request.response.status_code, 403)

        spoofed_request = types.SimpleNamespace(
            request=types.SimpleNamespace(host="console.internal", port=443),
            client_conn=types.SimpleNamespace(sni="console.internal"),
            server_conn=types.SimpleNamespace(id="console-server", address=("192.168.50.10", 443)),
            response=None,
        )
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("192.168.50.10", 443)),
        ]):
            policy.request(spoofed_request)
        self.assertEqual(spoofed_request.response.status_code, 403)

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
