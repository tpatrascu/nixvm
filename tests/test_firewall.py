"""Policy-generation tests for the nixvm-firewall-test suite."""

import importlib.util
import ipaddress
import json
import os
from importlib.machinery import SourceFileLoader
from pathlib import Path
import socketserver
import subprocess
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
                "ips": ["192.168.124.1"],
                "ports": [53, 2049],
                "all_ports": False,
            },
            "internal": {
                "deny_cidrs": ["10.0.0.0/8", "192.168.0.0/16", "fc00::/7"],
                "allow": [
                    {"ip": "10.0.0.25", "ports": [443], "all_ports": False},
                    {"ip": "10.0.0.26", "ports": [], "all_ports": True},
                    {"ip": "fc00::25", "ports": [22], "all_ports": False},
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

    def test_host_ports_are_allowed_per_ip_then_other_host_traffic_is_dropped(self):
        accepts = [rule for rule in self.rules(action="accept", priority=100)
                   if rule[0].tag in {"tcp", "udp"}]
        destinations = {
            (rule[0].get("dstportstart"), rule[0].tag)
            for rule in accepts
            if rule[0].get("dstipaddr") == "192.168.124.1"
        }
        self.assertEqual(destinations, {
            ("53", "tcp"), ("53", "udp"),
            ("2049", "tcp"), ("2049", "udp"),
        })
        host_drop = next(
            rule for rule in self.rules(action="drop", priority=800, element="all")
            if rule.find("all").get("dstipaddr") == "192.168.124.1"
        )
        self.assertLess(self.filter.findall("rule").index(accepts[0]),
                        self.filter.findall("rule").index(host_drop))

    def test_internal_allow_rules_are_scoped_to_each_ip_and_tcp_udp_port(self):
        ipv4 = [
            rule[0]
            for rule in self.rules(action="accept", priority=100)
            if rule[0].tag in {"tcp", "udp"} and rule[0].get("dstipaddr") == "10.0.0.25"
        ]
        self.assertEqual({(item.get("dstportstart"), item.tag) for item in ipv4},
                         {("443", "tcp"), ("443", "udp")})
        ipv6 = [
            rule[0]
            for rule in self.rules(action="accept", priority=100)
            if rule[0].tag == "ipv6"
            and rule[0].get("dstipaddr") == "fc00::25"
        ]
        self.assertEqual({(item.get("dstportstart"), item.get("protocol")) for item in ipv6},
                         {("22", "tcp"), ("22", "udp")})

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

    def test_dhcp_client_requests_are_allowed_before_broadcast_denies(self):
        rules = self.filter.findall("rule")
        dhcp = [rule for rule in rules
                if rule.get("action") == "accept" and rule.get("priority") == "75"
                and rule.find("udp") is not None]
        self.assertEqual(len(dhcp), 1)
        self.assertEqual(dhcp[0].find("udp").attrib, {
            "srcportstart": "68", "srcportend": "68",
            "dstportstart": "67", "dstportend": "67",
        })
        self.assertLess(rules.index(dhcp[0]), min(
            rules.index(rule) for rule in self.rules(action="drop", priority=900)
        ))

    def test_nixvm_firewall_test_gateway_is_covered_by_an_internal_deny_rule(self):
        config = json.loads(os.environ.get("NIXVM_FIREWALL_TEST_CONFIG", "{}"))
        network = config.get("network", {
            "name": "nixvm-firewall-test-subnet", "uri": "qemu:///system",
            "cidr": "192.168.123.0/24", "gateway": "192.168.123.1", "prefix": 24,
        })
        gateway = ipaddress.ip_address(network["gateway"])
        access = config.get("filter_access", self.access)
        self.assertEqual(network["name"], "nixvm-firewall-test-subnet")
        gateway_allows = [rule for rule in access["internal"]["allow"]
                          if ipaddress.ip_address(rule["ip"]) == gateway]
        if config:
            self.assertEqual(gateway_allows, [{
                "ip": gateway.compressed,
                "ports": [network["allow_test_port"]],
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

    def test_connectivity_probe_exception_allows_only_its_configured_port(self):
        gateway = "192.168.123.1"
        allow_port = 49231
        access = {
            "host": {"ips": [], "ports": [], "all_ports": False},
            "internal": {
                "deny_cidrs": ["192.168.0.0/16"],
                "allow": [{"ip": gateway, "ports": [allow_port], "all_ports": False}],
            },
        }
        rules = nixvm.build_network_filter("nixvm-test-access", access).findall("rule")
        allowed = [rule[0] for rule in rules
                   if rule.get("action") == "accept" and rule[0].tag in {"tcp", "udp"}
                   and rule[0].get("dstipaddr") == gateway]
        self.assertEqual({(rule.get("dstportstart"), rule.tag)
                          for rule in allowed}, {(str(allow_port), "tcp"),
                                                 (str(allow_port), "udp")})
        self.assertTrue(any(rule.get("action") == "drop"
                            and rule.find("all") is not None
                            and rule.find("all").get("dstipaddr") == "192.168.0.0"
                            for rule in rules))

    def test_internal_allow_ip_must_be_in_a_denied_cidr(self):
        access = {
            "host": {"ips": [], "ports": [], "all_ports": False},
            "internal": {
                "deny_cidrs": ["10.0.0.0/8"],
                "allow": [{"ip": "192.168.1.25", "ports": [443], "all_ports": False}],
            },
        }
        with self.assertRaisesRegex(ValueError, "outside internal_network_deny"):
            nixvm.build_network_filter("nixvm-access", access)

    def test_host_and_internal_allow_lists_cannot_overlap(self):
        access = {
            "host": {"ips": ["10.0.0.25"], "ports": [53], "all_ports": False},
            "internal": {
                "deny_cidrs": ["10.0.0.0/8"],
                "allow": [{"ip": "10.0.0.25", "ports": [443], "all_ports": False}],
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
             patch.object(nixvm_wrapper, "firewall_tests",
                          side_effect=lambda *_: events.append("firewall-tests")), \
             patch.object(nixvm_wrapper, "internal",
                          side_effect=lambda *args: events.append(("internal", *args))), \
             patch.object(nixvm_wrapper, "tofu",
                          side_effect=lambda *args: events.append(("tofu", *args))):
            nixvm_wrapper.main(["up"])
        self.assertEqual(events, [
            ("internal", "setup-host-nfs", []),
            ("tofu", "apply", []),
            "firewall-tests",
            ("internal", "setup-hostname"),
        ])

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

        self.assertEqual([signature(rule) for rule in actual.findall("rule")],
                         [signature(rule) for rule in expected.findall("rule")])
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
                             capture=True, timeout=30)
            except subprocess.CalledProcessError as error:
                self.fail(f"guest connectivity probe failed (status {error.returncode}); "
                          f"stdout={error.stdout!r}; stderr={error.stderr!r}")
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
