"""Policy-generation tests for the nixvm-firewall-test suite."""

import importlib.util
import ipaddress
import json
import os
from collections import Counter
from importlib.machinery import SourceFileLoader
from pathlib import Path
import shutil
import socketserver
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET


spec = importlib.util.spec_from_file_location(
    "nixvm", Path(__file__).parents[1] / "scripts/nixvm.py"
)
nixvm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nixvm)

wrapper_spec = importlib.util.spec_from_loader(
    "nixvm_wrapper", SourceFileLoader("nixvm_wrapper", str(Path(__file__).parents[1] / "nixvm"))
)
nixvm_wrapper = importlib.util.module_from_spec(wrapper_spec)
wrapper_spec.loader.exec_module(nixvm_wrapper)


class NixvmFirewallTest(unittest.TestCase):
    """Exercise the filter policy used by the VM's libvirt interface."""

    def setUp(self):
        self.access = {
            "host": {
                "dhcp_server": "192.168.124.1",
                "ips": ["192.168.124.1"],
                "tcp_ports": [53, 2049],
                "udp_ports": [53],
                "all_ports": False,
            },
            "internal": {
                "deny_cidrs": ["10.0.0.0/8", "192.168.0.0/16", "fc00::/7"],
                "blocked_ports": [80, 443, 8443, 8080, 8081, 8082],
                "allow": [
                    {"ip": "10.0.0.25", "tcp_ports": [443], "udp_ports": [53], "all_ports": False},
                    {"ip": "10.0.0.26", "tcp_ports": [], "udp_ports": [], "all_ports": True},
                    {"ip": "fc00::25", "tcp_ports": [22], "udp_ports": [], "all_ports": False},
                ],
            },
        }
        self.filter = nixvm.build_network_filter("nixvm-access", self.access)

    def rules(self, *, action=None, priority=None, element=None):
        rules = list(self.filter.findall("rule"))
        if action is not None:
            rules = [rule for rule in rules if rule.get("action") == action]
        if priority is not None:
            rules = [rule for rule in rules if rule.get("priority") == str(priority)]
        if element is not None:
            rules = [rule for rule in rules if rule.find(element) is not None]
        return rules

    def test_filter_keeps_anti_spoofing_arp_and_l2_rules(self):
        self.assertEqual(self.filter.get("chain"), "root")
        references = [node.get("filter") for node in self.filter.findall("filterref")]
        self.assertEqual(references, [
            "no-mac-spoofing", "no-ip-spoofing", "allow-incoming-ipv4",
            "no-arp-spoofing", "no-other-l2-traffic", "qemu-announce-self-rarp",
        ])
        arp_allow = next(rule for rule in self.filter.findall("rule")
                         if rule[0].tag == "mac" and rule[0].get("protocolid") == "arp")
        self.assertEqual(arp_allow.get("direction"), "inout")

    def test_host_tcp_and_udp_ports_are_allowed_independently_then_other_traffic_dropped(self):
        accepts = [rule for rule in self.rules(action="accept", priority=100)
                   if rule[0].tag in {"tcp", "udp"}]
        destinations = {
            (rule[0].get("dstportstart"), rule[0].tag)
            for rule in accepts
            if rule[0].get("dstipaddr") == "192.168.124.1"
        }
        self.assertEqual(destinations, {
            ("53", "tcp"), ("53", "udp"),
            ("2049", "tcp"),
        })
        host_drop = next(
            rule for rule in self.rules(action="drop", priority=800, element="all")
            if rule.find("all").get("dstipaddr") == "192.168.124.1"
        )
        self.assertLess(self.filter.findall("rule").index(accepts[0]),
                        self.filter.findall("rule").index(host_drop))

    def test_gateway_dns_is_allowed_even_when_host_access_ports_are_empty(self):
        access = json.loads(json.dumps(self.access))
        access["host"]["tcp_ports"] = []
        access["host"]["udp_ports"] = []
        access["internal"]["allow"].append({
            "ip": "192.168.124.1", "tcp_ports": [53], "udp_ports": [53],
            "all_ports": False, "managed_allow": True,
        })
        policy = nixvm.build_network_filter("nixvm-access", access)
        dns_accepts = [
            rule for rule in policy.findall("rule")
            if rule.get("action") == "accept" and rule.get("priority") == "100"
            and rule[0].get("dstipaddr") == "192.168.124.1"
        ]
        self.assertEqual({(rule[0].tag, rule[0].get("dstportstart")) for rule in dns_accepts},
                         {("tcp", "53"), ("udp", "53")})

    def test_empty_global_port_block_keeps_http_egress_unblocked(self):
        access = json.loads(json.dumps(self.access))
        access["internal"]["blocked_ports"] = []
        policy = nixvm.build_network_filter("nixvm-access", access)
        global_port_drops = [
            rule for rule in policy.findall("rule")
            if rule.get("action") == "drop" and rule.get("priority") == "90"
        ]
        self.assertEqual(global_port_drops, [])
        self.assertTrue(any(
            rule.get("action") == "drop" and rule.get("priority") == "900"
            for rule in policy.findall("rule")
        ), "internal-network CIDR protection must remain active")

    def test_internal_allow_rules_control_tcp_and_udp_ports_independently(self):
        ipv4 = [
            rule[0]
            for rule in self.rules(action="accept", priority=100)
            if rule[0].tag in {"tcp", "udp"} and rule[0].get("dstipaddr") == "10.0.0.25"
        ]
        self.assertEqual({(item.get("dstportstart"), item.tag) for item in ipv4},
                         {("443", "tcp"), ("53", "udp")})
        ipv6 = [
            rule[0]
            for rule in self.rules(action="accept", priority=100)
            if rule[0].tag == "ipv6"
            and rule[0].get("dstipaddr") == "fc00::25"
        ]
        self.assertEqual({(item.get("dstportstart"), item.get("protocol")) for item in ipv6},
                         {("22", "tcp")})
        blocked_port_exceptions = [
            rule[0]
            for rule in self.rules(action="accept", priority=85)
            if rule[0].get("dstipaddr") == "10.0.0.25"
        ]
        self.assertEqual({(item.get("dstportstart"), item.tag) for item in blocked_port_exceptions},
                         {("443", "tcp")})

    def test_all_ports_rule_has_no_protocol_or_port_restriction(self):
        rule = next(
            rule for rule in self.rules(action="accept", priority=100, element="all")
            if rule.find("all").get("dstipaddr") == "10.0.0.26"
        )
        attrs = rule.find("all").attrib
        self.assertEqual(attrs, {"dstipaddr": "10.0.0.26"})

    def test_ipv4_and_ipv6_deny_cidrs_are_emitted_after_allow_rules(self):
        rules = self.filter.findall("rule")
        denies = self.rules(action="drop", priority=900)
        ranges = {
            (rule[0].tag, rule[0].get("dstipaddr"), rule[0].get("dstipmask"))
            for rule in denies
            if rule[0].tag in {"all", "ipv6"} and rule[0].get("dstipmask") is not None
        }
        self.assertEqual(ranges, {
            ("all", "10.0.0.0", "8"),
            ("all", "192.168.0.0", "16"),
            ("ipv6", "fc00::", "7"),
        })
        ipv6_denies = [rule for rule in denies if rule[0].tag == "ipv6"]
        self.assertTrue(ipv6_denies)
        self.assertTrue(all(rule.get("statematch") == "false" for rule in ipv6_denies))
        self.assertTrue(all("state" not in rule[0].attrib for rule in ipv6_denies))
        last_allow_index = max(
            index for index, rule in enumerate(rules)
            if rule.get("action") == "accept" and rule.get("priority") == "100"
        )
        first_deny_index = min(rules.index(rule) for rule in denies)
        self.assertLess(last_allow_index, first_deny_index)

    def test_denies_only_block_new_connections_so_host_replies_can_pass(self):
        denies = self.rules(action="drop", priority=900, element="all")
        self.assertTrue(denies)
        self.assertTrue(all(rule.find("all").get("state") == "NEW" for rule in denies))

    def test_unrestricted_traffic_is_accepted_after_destination_drops(self):
        rules = self.filter.findall("rule")
        catch_all = [rule for rule in rules if rule.get("action") == "accept"
                     and rule.get("priority") == "999"]
        self.assertEqual([rule[0].tag for rule in catch_all], ["ip", "ipv6"])
        denies = self.rules(action="drop", priority=900, element="all")
        self.assertLess(max(rules.index(rule) for rule in denies),
                        min(rules.index(rule) for rule in catch_all))

    def test_established_host_connections_can_reply_before_new_drops(self):
        replies = [rule for rule in self.filter.findall("rule")
                   if rule.get("action") == "accept" and rule.get("priority") == "50"
                   and rule[0].tag == "all"]
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0][0].get("state"), "ESTABLISHED,RELATED")
        denies = self.rules(action="drop", priority=900, element="all")
        self.assertTrue(denies)

    def test_dhcp_client_requests_are_limited_to_gateway_and_broadcast(self):
        rules = self.filter.findall("rule")
        dhcp = [rule for rule in rules
                if rule.get("action") == "accept" and rule.get("priority") == "75"
                and rule.find("udp") is not None]
        self.assertEqual({rule[0].get("dstipaddr") for rule in dhcp},
                         {"192.168.124.1", "255.255.255.255"})
        self.assertTrue(all({key: value for key, value in rule.find("udp").attrib.items()
                             if key != "dstipaddr"} == {
            "srcportstart": "68", "srcportend": "68",
            "dstportstart": "67", "dstportend": "67",
        } for rule in dhcp))
        dhcp_port_drops = [rule for rule in rules
                           if rule.get("action") == "drop" and rule.get("priority") == "80"
                           and rule.find("udp") is not None]
        self.assertTrue(any(rule.find("udp").get("dstportstart") == "67"
                            for rule in dhcp_port_drops))
        self.assertTrue(any(rule.find("udp").get("srcportstart") == "68"
                            for rule in dhcp_port_drops))
        self.assertLess(max(rules.index(rule) for rule in dhcp),
                        min(rules.index(rule) for rule in dhcp_port_drops))

    def test_public_host_addresses_are_dropped_before_host_access_rules(self):
        access = json.loads(json.dumps(self.access))
        access["host"]["blocked_ips"] = ["2a02:2f04::1234"]
        policy = nixvm.build_network_filter("nixvm-access", access)
        rules = policy.findall("rule")
        host_drop = next(rule for rule in rules
                         if rule.get("action") == "drop" and rule.get("priority") == "80"
                         and rule.find("ipv6") is not None
                         and rule.find("ipv6").get("dstipaddr") == "2a02:2f04::1234")
        self.assertEqual(host_drop.get("statematch"), "false")
        self.assertLess(rules.index(host_drop), min(
            rules.index(rule) for rule in rules
            if rule.get("action") == "accept" and rule.get("priority") == "100"
        ))

    def test_host_addresses_are_blocked_except_explicit_host_access_ips(self):
        access = json.loads(json.dumps(self.access))
        access["host"]["ips"].append("192.0.2.20")
        access["internal"]["allow"].append({
            "ip": "192.0.2.21", "tcp_ports": [49231], "udp_ports": [],
            "all_ports": False, "managed_allow": True,
        })
        with patch.object(nixvm, "host_interface_addresses",
                          return_value={"192.168.124.1", "192.0.2.20", "192.0.2.21",
                                        "2001:db8::20"}):
            nixvm.add_host_address_blocks(access)
        self.assertEqual(access["host"]["blocked_ips"], ["2001:db8::20"])

    def test_host_interface_address_discovery_includes_live_global_ipv6(self):
        ipv4_table = """Main:
           |-- 192.0.2.15
              /32 host LOCAL
        Local:
           |-- 192.0.2.15
              /32 host LOCAL
"""
        ipv6_table = "2a022f049406d900605d31a7d6920f87 02 40 00 01 enx0\n"
        original_read_text = Path.read_text

        def fake_read_text(path, encoding=None):
            if path == Path("/proc/net/fib_trie"):
                return ipv4_table
            if path == Path("/proc/net/if_inet6"):
                return ipv6_table
            return original_read_text(path, encoding=encoding)

        with patch.object(Path, "read_text", fake_read_text):
            addresses = nixvm.host_interface_addresses()
        self.assertEqual(addresses, {"192.0.2.15", "2a02:2f04:9406:d900:605d:31a7:d692:f87"})

    def test_nixvm_firewall_test_gateway_is_covered_by_an_internal_deny_rule(self):
        config = json.loads(os.environ.get("NIXVM_FIREWALL_TEST_CONFIG", "{}"))
        network = config.get("network", {
            "name": "nixvm-firewall-test-subnet", "uri": "qemu:///system",
            "cidr": "192.168.123.0/24", "gateway": "192.168.123.1", "prefix": 24,
        })
        gateway = ipaddress.ip_address(network["gateway"])
        access = config.get("filter_access", self.access)
        vm_name = config.get("vm_name", "nixvm")
        self.assertEqual(network["name"], f"{vm_name}-firewall-test-subnet")
        gateway_allows = [rule for rule in access["internal"]["allow"]
                          if ipaddress.ip_address(rule["ip"]) == gateway]
        if config:
            self.assertEqual(gateway_allows, [{
                "ip": gateway.compressed,
                "tcp_ports": [network["allow_test_port"]],
                "udp_ports": [network["allow_test_port"]],
                "all_ports": False,
            }], "only the temporary connectivity-test port may reach the test gateway")
        else:
            self.assertEqual(gateway_allows, [])
        denied = [
            ipaddress.ip_network(f"{rule[0].get('dstipaddr')}/{rule[0].get('dstipmask')}", strict=False)
            for rule in nixvm.build_network_filter("nixvm-test-access", access).findall("rule")
            if rule.get("action") == "drop" and rule.get("priority") == "900"
            and rule[0].tag == "all" and rule[0].get("dstipmask") is not None
        ]
        self.assertTrue(any(gateway in cidr for cidr in denied),
                        f"test network gateway {gateway} is not in an internal deny CIDR")

    def test_connectivity_probe_exception_allows_only_its_configured_tcp_port(self):
        gateway = "192.168.123.1"
        allow_port = 49231
        access = {
            "host": {"ips": [], "tcp_ports": [], "udp_ports": [], "all_ports": False},
            "internal": {
                "deny_cidrs": ["192.168.0.0/16"],
                "allow": [{"ip": gateway, "tcp_ports": [allow_port],
                           "udp_ports": [], "all_ports": False}],
            },
        }
        rules = nixvm.build_network_filter("nixvm-test-access", access).findall("rule")
        allowed = [rule[0] for rule in rules
                   if rule.get("action") == "accept" and rule[0].tag in {"tcp", "udp"}
                   and rule[0].get("dstipaddr") == gateway]
        self.assertEqual({(rule.get("dstportstart"), rule.tag)
                          for rule in allowed}, {(str(allow_port), "tcp")})
        self.assertTrue(any(rule.get("action") == "drop"
                            and rule.find("all") is not None
                            and rule.find("all").get("dstipaddr") == "192.168.0.0"
                            for rule in rules))

    def test_ufw_probe_exception_uses_guest_subnet_and_test_gateway(self):
        config = {
            "vm_network": {"cidr": "192.168.184.0/24"},
            "network": {"cidr": "10.254.184.0/24", "gateway": "10.254.184.1",
                        "allow_test_port": 49231},
        }
        commands = nixvm.ufw_probe_allow_commands(["sudo"], config, "nixvm-nixvm2-firewall-test")
        self.assertEqual(len(commands), 2)
        for command, protocol in zip(commands, ("tcp", "udp")):
            self.assertEqual(command[command.index("from") + 1], "192.168.184.0/24")
            self.assertEqual(command[command.index("to") + 1], "10.254.184.1")
            self.assertEqual(command[command.index("proto") + 1], protocol)
            self.assertEqual(command[command.index("port") + 1], "49231")

    def test_live_firewall_test_refreshes_filter_before_comparing_it(self):
        config = {
            "filter_name": "nixvm-access",
            "filter_access": {"host": {}, "internal": {}},
            "connection": {"uri": "qemu:///system"},
            "network": {"uri": "qemu:///system"},
        }
        with patch.object(nixvm, "tofu_firewall_test_config", return_value=config), \
             patch.object(nixvm, "define_network_filter_config") as define_filter, \
             patch.object(nixvm, "wait_ssh"), \
             patch.object(nixvm, "tofu_host_firewall_config",
                          return_value={"method": "disabled", "name": "nixvm"}), \
             patch.object(nixvm, "run"):
            nixvm.run_firewall_tests([])
        define_filter.assert_called_once_with(
            "nixvm-access", config["filter_access"], "qemu:///system"
        )

    def test_internal_allow_ip_must_be_in_a_denied_cidr(self):
        access = {
            "host": {"ips": [], "tcp_ports": [], "udp_ports": [], "all_ports": False},
            "internal": {
                "deny_cidrs": ["10.0.0.0/8"],
                "allow": [{"ip": "192.168.1.25", "tcp_ports": [443],
                           "udp_ports": [], "all_ports": False}],
            },
        }
        with self.assertRaisesRegex(ValueError, "outside internal_network_deny"):
            nixvm.build_network_filter("nixvm-access", access)

    def test_managed_external_nfs_allow_can_be_outside_denied_cidrs(self):
        access = {
            "host": {"ips": [], "tcp_ports": [], "udp_ports": [], "all_ports": False},
            "internal": {
                "deny_cidrs": ["10.0.0.0/8"],
                "allow": [{
                    "ip": "198.51.100.25", "tcp_ports": [2049], "udp_ports": [],
                    "all_ports": False, "managed_allow": True,
                }],
            },
        }
        policy = nixvm.build_network_filter("nixvm-access", access)
        self.assertTrue(any(
            rule.get("action") == "accept" and rule.get("priority") == "100"
            and rule[0].get("dstipaddr") == "198.51.100.25"
            and rule[0].tag == "tcp" and rule[0].get("dstportstart") == "2049"
            for rule in policy.findall("rule")
        ))

    def test_host_and_internal_allow_lists_cannot_overlap(self):
        access = {
            "host": {"ips": ["10.0.0.25"], "tcp_ports": [53], "udp_ports": [], "all_ports": False},
            "internal": {
                "deny_cidrs": ["10.0.0.0/8"],
                "allow": [{"ip": "10.0.0.25", "tcp_ports": [443],
                           "udp_ports": [], "all_ports": False}],
            },
        }
        with self.assertRaisesRegex(ValueError, "only in host_access"):
            nixvm.build_network_filter("nixvm-access", access)

    def test_filter_xml_round_trips(self):
        serialized = ET.tostring(self.filter)
        parsed = ET.fromstring(serialized)
        self.assertEqual(parsed.get("name"), "nixvm-access")
        self.assertEqual(len(parsed.findall("rule")), len(self.filter.findall("rule")))

    def test_up_runs_live_firewall_tests_after_apply(self):
        events = []
        with patch.object(nixvm_wrapper, "split_arguments", return_value=([], [])), \
             patch.object(nixvm_wrapper, "ensure_workspace"), \
             patch.object(nixvm_wrapper, "firewall_tests",
                          side_effect=lambda *_: events.append("firewall-tests")), \
             patch.object(nixvm_wrapper, "internal",
                          side_effect=lambda *args: events.append(("internal", *args))), \
             patch.object(nixvm_wrapper, "tofu",
                          side_effect=lambda *args: events.append(("tofu", *args))), \
             patch.object(nixvm_wrapper, "current_vm_name", return_value="nixvm"), \
             patch.object(nixvm_wrapper, "install_spice_shortcut",
                          side_effect=lambda _name: events.append("install-spice-shortcut")):
            nixvm_wrapper.main(["up"])
        self.assertEqual(events, [
            ("internal", "setup-host-nfs", []),
            ("internal", "check-host-nfs", []),
            ("internal", "define-network-filter", []),
            ("internal", "ensure-vm-nat", []),
            ("internal", "setup-host-firewall", []),
            ("internal", "setup-host-mitmproxy", []),
            ("tofu", "apply", ["-auto-approve"]),
            ("internal", "update-config", []),
            "firewall-tests",
            ("internal", "setup-hostname"),
            "install-spice-shortcut",
        ])

    def test_spice_shortcut_installs_gnome_launcher_and_desktop_icon(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            desktop = home / "Desktop"
            desktop.mkdir(parents=True)
            data_home = root / "data"
            with patch.object(nixvm_wrapper.Path, "home", return_value=home), \
                 patch.dict(os.environ, {"XDG_DATA_HOME": str(data_home)}), \
                 patch.object(nixvm_wrapper.shutil, "which", side_effect={
                     "virt-viewer": "/usr/bin/virt-viewer",
                     "update-desktop-database": "/usr/bin/update-desktop-database",
                     "xdg-user-dir": "/usr/bin/xdg-user-dir",
                     "gio": "/usr/bin/gio",
                 }.get), \
                 patch.object(nixvm_wrapper.subprocess, "run") as run:
                nixvm_wrapper.install_spice_shortcut("nixvm")

            launcher = data_home / "applications/nixvm-spice-nixvm.desktop"
            shortcut = desktop / "nixvm-spice-nixvm.desktop"
            self.assertEqual(launcher.read_text(encoding="utf-8"), shortcut.read_text(encoding="utf-8"))
            self.assertIn('Exec=env GDK_BACKEND=x11 GDK_SCALE=1 "/usr/bin/virt-viewer" --auto-resize=always -c qemu:///system --domain-name "nixvm"',
                          launcher.read_text())
            self.assertIn("TryExec=/usr/bin/virt-viewer\n", launcher.read_text())
            self.assertIn("Name=NixVM SPICE (nixvm)\n", launcher.read_text())
            self.assertIn("Keywords=nixvm-spice;nixvm;spice;virt-viewer;console;", launcher.read_text())
            self.assertEqual(launcher.stat().st_mode & 0o777, 0o644)
            self.assertEqual(shortcut.stat().st_mode & 0o777, 0o755)
            validator = shutil.which("desktop-file-validate")
            if validator:
                subprocess.run([validator, str(launcher), str(shortcut)], check=True,
                               capture_output=True, text=True)
            run.assert_any_call([
                "/usr/bin/gio", "set", str(shortcut), "metadata::trusted", "true"
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

    @unittest.skipUnless(os.environ.get("NIXVM_FIREWALL_TEST_CONFIG"),
                         "live OpenTofu and libvirt resources are only checked by nixvm up")
    def test_live_filter_xml_matches_configured_firewall_policy(self):
        config = json.loads(os.environ["NIXVM_FIREWALL_TEST_CONFIG"])
        expected = nixvm.build_network_filter(config["filter_name"], config["filter_access"])
        actual_xml = subprocess.run(
            ["virsh", "--connect", config["network"]["uri"], "nwfilter-dumpxml", config["filter_name"]],
            check=True, capture_output=True, text=True,
        ).stdout
        actual = ET.fromstring(actual_xml)
        self.assertEqual(actual.get("name"), expected.get("name"))
        self.assertEqual([node.attrib for node in actual.findall("filterref")],
                         [node.attrib for node in expected.findall("filterref")])

        def signature(rule):
            children = tuple((child.tag, tuple(sorted(child.attrib.items())))
                             for child in rule)
            return (tuple(sorted(rule.attrib.items())), children)

        self.assertEqual(Counter(signature(rule) for rule in actual.findall("rule")),
                         Counter(signature(rule) for rule in expected.findall("rule")))
        domain_xml = subprocess.run(
            ["virsh", "--connect", config["network"]["uri"], "dumpxml", config["vm_name"]],
            check=True, capture_output=True, text=True,
        ).stdout
        domain = ET.fromstring(domain_xml)
        interfaces = domain.findall("./devices/interface")
        attached_filters = {
            node.get("filter") for node in domain.findall("./devices/interface/filterref")
        }
        self.assertIn(config["filter_name"], attached_filters,
                      "the VM has no interface attached to the configured libvirt firewall filter")
        primary_nic = next((interface for interface in interfaces
                            if interface.find("mac").get("address").lower()
                            == config["network"]["primary_mac"].lower()), None)
        self.assertIsNotNone(primary_nic, "the VM has no primary NAT interface")
        self.assertEqual(primary_nic.find("source").get("network"), config["vm_network"]["name"])
        self.assertEqual(primary_nic.find("filterref").get("filter"), config["filter_name"])

    @unittest.skipUnless(os.environ.get("NIXVM_FIREWALL_TEST_CONFIG"),
                         "live OpenTofu and libvirt resources are only checked by nixvm up")
    def test_live_primary_network_is_nat_with_dhcp(self):
        config = json.loads(os.environ["NIXVM_FIREWALL_TEST_CONFIG"])
        network = config["vm_network"]
        info = subprocess.run(
            ["virsh", "--connect", network["uri"], "net-info", network["name"]],
            check=True, capture_output=True, text=True,
        ).stdout
        self.assertRegex(info, r"(?m)^Active:\s+yes\s*$")
        self.assertRegex(info, r"(?m)^Autostart:\s+yes\s*$")
        xml = subprocess.run(
            ["virsh", "--connect", network["uri"], "net-dumpxml", network["name"]],
            check=True, capture_output=True, text=True,
        ).stdout
        actual = ET.fromstring(xml)
        self.assertEqual(actual.find("./forward").get("mode"), "nat")
        subnet = ipaddress.ip_network(network["cidr"], strict=False)
        ip_configs = actual.findall("./ip")
        primary_ip = next(ip for ip in ip_configs if ip.get("address") == network["gateway"])
        self.assertEqual(ipaddress.ip_network(
            f"{primary_ip.get('address')}/{primary_ip.get('prefix', primary_ip.get('netmask'))}", strict=False), subnet)
        dhcp_range = primary_ip.find("./dhcp/range")
        self.assertIsNotNone(dhcp_range, "the VM NAT network has no DHCP range")
        self.assertIn(ipaddress.ip_address(dhcp_range.get("start")), subnet)
        self.assertIn(ipaddress.ip_address(dhcp_range.get("end")), subnet)
        test_ip = next(ip for ip in ip_configs if ip.get("address") == config["network"]["gateway"])
        self.assertEqual(ipaddress.ip_network(
            f"{test_ip.get('address')}/{test_ip.get('prefix', test_ip.get('netmask'))}", strict=False),
            ipaddress.ip_network(config["network"]["cidr"], strict=False))

    @unittest.skipUnless(os.environ.get("NIXVM_FIREWALL_TEST_CONFIG"),
                         "guest internet connectivity is checked by nixvm up")
    def test_guest_can_reach_the_public_internet(self):
        config = json.loads(os.environ["NIXVM_FIREWALL_TEST_CONFIG"])
        try:
            nixvm.remote(config["connection"],
                         "curl --fail --silent --show-error --max-time 20 https://example.com/ >/dev/null",
                         capture=True, timeout=30)
        except subprocess.CalledProcessError as error:
            self.fail(f"guest could not reach https://example.com/ (status {error.returncode}); "
                      f"stdout={error.stdout!r}; stderr={error.stderr!r}")

    @unittest.skipUnless(os.environ.get("NIXVM_FIREWALL_TEST_CONFIG"),
                         "live OpenTofu and libvirt resources are only checked by nixvm up")
    def test_live_test_network_matches_configured_subnet_and_is_autostarted(self):
        config = json.loads(os.environ["NIXVM_FIREWALL_TEST_CONFIG"])
        network = config["network"]
        info = subprocess.run(
            ["virsh", "--connect", network["uri"], "net-info", network["bridge_network"]],
            check=True, capture_output=True, text=True,
        ).stdout
        self.assertRegex(info, r"(?m)^Active:\s+yes\s*$")
        self.assertRegex(info, r"(?m)^Autostart:\s+yes\s*$")
        actual_xml = subprocess.run(
            ["virsh", "--connect", network["uri"], "net-dumpxml", network["bridge_network"]],
            check=True, capture_output=True, text=True,
        ).stdout
        actual = ET.fromstring(actual_xml)
        test_ip = next((ip for ip in actual.findall("ip")
                        if ip.get("address") == network["gateway"]), None)
        self.assertIsNotNone(test_ip, "the firewall test gateway is not configured on the NAT bridge")
        actual_prefix = test_ip.get("prefix", test_ip.get("netmask"))
        expected_subnet = ipaddress.ip_network(network["cidr"], strict=False)
        actual_subnet = ipaddress.ip_network(f"{test_ip.get('address')}/{actual_prefix}", strict=False)
        self.assertEqual(actual_subnet, expected_subnet)

    @unittest.skipUnless(os.environ.get("NIXVM_FIREWALL_TEST_CONFIG"),
                         "guest connectivity is tested by nixvm up after the VM starts")
    def test_guest_can_reach_only_the_temporary_allowed_test_port(self):
        config = json.loads(os.environ["NIXVM_FIREWALL_TEST_CONFIG"])
        network = config["network"]

        class ProbeHandler(socketserver.BaseRequestHandler):
            def handle(self):
                self.server.accepted.set()
                self.request.recv(16)
                self.request.sendall(b"ok")

        class ProbeServer(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        class ProbeUDPHandler(socketserver.BaseRequestHandler):
            def handle(self):
                data, sock = self.request
                self.server.accepted.set()
                sock.sendto(b"ok", self.client_address)

        class ProbeUDPServer(socketserver.ThreadingUDPServer):
            allow_reuse_address = True
            daemon_threads = True

        servers = []
        threads = []
        try:
            for server_class, handler in ((ProbeServer, ProbeHandler),
                                          (ProbeUDPServer, ProbeUDPHandler)):
                for port in (network["allow_test_port"], network["deny_test_port"]):
                    server = server_class((network["gateway"], port), handler)
                    server.accepted = threading.Event()
                    servers.append(server)
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    threads.append(thread)

            gateway = network["gateway"]
            primary_gateway = network["primary_gateway"]
            primary_mac = network["primary_mac"]
            allow_port = network["allow_test_port"]
            deny_port = network["deny_test_port"]
            script = "\n".join([
                "set -eu",
                f"iface=$(ip -o link show | awk -F': ' -v mac='{primary_mac}' 'tolower($0) ~ tolower(mac) {{sub(/:.*/, \"\", $2); print $2; exit}}')",
                "test -n \"$iface\" || { echo 'primary NAT interface was not found' >&2; exit 1; }",
                f"trap 'sudo ip route del {gateway}/32 via {primary_gateway} dev \"$iface\" 2>/dev/null || true' EXIT",
                f"sudo ip route replace {gateway}/32 via {primary_gateway} dev \"$iface\"",
                f"timeout 4 bash -c 'exec 3<>/dev/tcp/{gateway}/{allow_port}' || {{ echo 'allowed test connection failed' >&2; exit 1; }}",
                f"echo probe >/dev/udp/{gateway}/{allow_port}",
                f"echo probe >/dev/udp/{gateway}/{deny_port}",
                "set +e",
                f"timeout 3 bash -c 'exec 3<>/dev/tcp/{gateway}/{deny_port}' >/dev/null 2>&1",
                "result=$?",
                "set -e",
                "test \"$result\" -eq 124 || { echo \"denied test connection was not dropped (status $result)\" >&2; exit 1; }",
                "",
            ])
            try:
                nixvm.remote(config["connection"], "bash -s", input=script,
                             capture=True, timeout=30, stderr=subprocess.STDOUT)
            except subprocess.CalledProcessError as error:
                self.fail(f"guest connectivity probe failed (status {error.returncode}); "
                          f"output={error.stdout!r}")
            self.assertTrue(servers[0].accepted.wait(2),
                            "the allowed test connection never reached the host listener")
            self.assertFalse(servers[1].accepted.wait(0.2),
                             "the denied test connection reached the host listener")
            self.assertTrue(servers[2].accepted.wait(2),
                            "the allowed UDP probe never reached the host listener")
            self.assertFalse(servers[3].accepted.wait(0.2),
                             "the denied UDP probe reached the host listener")
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
