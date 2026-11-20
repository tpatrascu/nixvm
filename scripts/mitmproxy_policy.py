"""mitmproxy addon used by the host-side nixvm Podman container."""

from __future__ import annotations

import fnmatch
import ipaddress
import json
import os
from pathlib import Path
import re
import socket

from mitmproxy import ctx, http


class NixvmPolicy:
    def __init__(self):
        policy_path = os.environ.get("NIXVM_MITMPROXY_POLICY", "/etc/mitmproxy/policy.json")
        with open(policy_path, encoding="utf-8") as source:
            policy = json.load(source)
        self.blocked_domains = self.compile_domain_patterns(policy.get("blocked_domains", []))
        self.allowed_domains = self.compile_domain_patterns(policy.get("allowed_domains", []))
        self.denied_networks = tuple(
            ipaddress.ip_network(cidr) for cidr in policy.get("deny_cidrs", [])
        )
        self.allowed_internal_hosts = {}
        self.server_authorizations = {}
        for entry in policy.get("allowed_internal_hosts", []):
            hostname = self.normalize_domain(entry["hostname"])
            if hostname in self.allowed_internal_hosts:
                raise ValueError(f"duplicate internal hostname rule: {hostname}")
            self.allowed_internal_hosts[hostname] = {
                "ips": frozenset(ipaddress.ip_address(address).compressed for address in entry["ips"]),
                "ports": frozenset(int(port) for port in entry.get("ports", [443])),
            }

    @staticmethod
    def normalize_domain(domain: str) -> str:
        return domain.strip().rstrip(".").lower()

    @classmethod
    def compile_domain_patterns(cls, patterns):
        compiled = []
        for pattern in patterns:
            if pattern.startswith("glob:"):
                compiled.append(("glob", cls.normalize_domain(pattern[5:])))
            elif pattern.startswith("regex:"):
                compiled.append(("regex", re.compile(pattern[6:], re.IGNORECASE)))
            else:
                compiled.append(("domain", cls.normalize_domain(pattern)))
        return tuple(compiled)

    @classmethod
    def domain_matches(cls, host: str, patterns) -> bool:
        host = cls.normalize_domain(host)
        try:
            host = ipaddress.ip_address(host).compressed
        except ValueError:
            pass
        for kind, pattern in patterns:
            if kind == "domain" and (host == pattern or host.endswith("." + pattern)):
                return True
            if kind == "glob" and fnmatch.fnmatchcase(host, pattern):
                return True
            if kind == "regex" and pattern.fullmatch(host):
                return True
        return False

    def domain_is_blocked(self, host: str) -> bool:
        if self.domain_matches(host, self.allowed_domains):
            return False
        return self.domain_matches(host, self.blocked_domains)

    def address_is_denied(self, address: str) -> bool:
        parsed = ipaddress.ip_address(address.split("%", 1)[0])
        return any(
            parsed.version == network.version and parsed in network
            for network in self.denied_networks
        )

    @staticmethod
    def host_interface_addresses():
        """Read current host addresses from the shared Linux network namespace."""
        addresses = set()
        try:
            ipv4_routes = Path("/proc/net/fib_trie").read_text(encoding="ascii").splitlines()
        except OSError as error:
            return None, f"could not inspect local IPv4 addresses: {error}"

        for index, line in enumerate(ipv4_routes):
            if line.strip() != "/32 host LOCAL":
                continue
            for candidate in reversed(ipv4_routes[max(0, index - 2):index]):
                match = re.search(r"\|--\s+(\d+(?:\.\d+){3})\s*$", candidate)
                if match:
                    addresses.add(ipaddress.IPv4Address(match.group(1)).compressed)
                    break

        try:
            ipv6_interfaces = Path("/proc/net/if_inet6").read_text(encoding="ascii")
        except FileNotFoundError:
            ipv6_interfaces = ""
        except OSError as error:
            return None, f"could not inspect local IPv6 addresses: {error}"

        for line in ipv6_interfaces.splitlines():
            fields = line.split()
            if fields and re.fullmatch(r"[0-9a-fA-F]{32}", fields[0]):
                addresses.add(ipaddress.IPv6Address(bytes.fromhex(fields[0])).compressed)
        return addresses, None

    def internal_rule_allows(self, host: str, port: int, address: str) -> bool:
        rule = self.allowed_internal_hosts.get(self.normalize_domain(host))
        if not rule or port not in rule["ports"]:
            return False
        try:
            normalized_address = ipaddress.ip_address(address.split("%", 1)[0]).compressed
        except ValueError:
            return False
        return normalized_address in rule["ips"]

    def resolve_and_pin(self, host: str, port: int):
        hostname = self.normalize_domain(host)
        internal_rule = self.allowed_internal_hosts.get(hostname)
        if internal_rule and port not in internal_rule["ports"]:
            return None, f"port {port} is not allowed for internal destination {hostname}"

        local_addresses, error = self.host_interface_addresses()
        if error:
            return None, error

        try:
            literal = ipaddress.ip_address(host)
            candidates = [literal.compressed]
        except ValueError:
            try:
                candidates = [
                    item[4][0]
                    for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
                ]
            except OSError as error:
                return None, f"destination name could not be resolved: {error}"

        if not candidates:
            return None, "destination name returned no addresses"

        addresses = []
        for address in candidates:
            try:
                parsed = ipaddress.ip_address(address.split("%", 1)[0])
            except ValueError:
                return None, "resolver returned an invalid destination address"
            if parsed.compressed in local_addresses:
                return None, f"destination address {parsed} is assigned to this host"
            denied = self.address_is_denied(parsed.compressed)
            if internal_rule and parsed.compressed not in internal_rule["ips"]:
                return None, f"destination address {parsed} is not allowed for internal hostname {hostname}"
            if denied and not internal_rule:
                return None, f"destination resolves to denied internal address {parsed}"
            addresses.append(parsed.compressed)

        # Prefer IPv4 when available for compatibility with user-mode container
        # networking; every resolved address has already passed the deny check.
        # Keep IPv6 when it is the only address returned.
        chosen = next((address for address in addresses
                       if ipaddress.ip_address(address).version == 4), addresses[0])
        return chosen, None

    def server_connect(self, data):
        if not data.server.address:
            data.server.error = "destination address is missing"
            return

        host, port = data.server.address
        if self.domain_is_blocked(host):
            data.server.error = f"blocked by nixvm hostname policy: {host}"
            ctx.log.warn(data.server.error)
            return

        resolved, error = self.resolve_and_pin(host, port)
        if error:
            data.server.error = error
            ctx.log.warn(f"Blocked proxy connection to {host}:{port}: {error}")
            return

        # Pin the upstream socket to an address that passed the CIDR check.
        # Keep the original hostname as SNI for inspected HTTPS connections.
        self.server_authorizations[data.server.id] = {
            "hostname": self.normalize_domain(host),
            "ip": resolved,
            "port": port,
        }
        data.server.address = (resolved, port)
        if data.server.tls:
            try:
                ipaddress.ip_address(host)
            except ValueError:
                data.server.sni = host

    def tls_clienthello(self, data):
        """Bind TLS SNI to the authorized CONNECT destination before pass-through."""
        sni = data.client_hello.sni
        authorization = self.server_authorizations.get(data.context.server.id)
        reason = None
        if sni and self.domain_is_blocked(sni):
            reason = f"blocked by nixvm hostname policy: {self.normalize_domain(sni)}"
        elif authorization and self.address_is_denied(authorization["ip"]):
            normalized_sni = self.normalize_domain(sni) if sni else None
            if normalized_sni != authorization["hostname"]:
                reason = (
                    f"TLS SNI {normalized_sni or '<missing>'} does not match authorized "
                    f"internal hostname {authorization['hostname']}"
                )
        elif not authorization and data.context.server.address:
            destination, port = data.context.server.address
            resolved, error = self.resolve_and_pin(destination, port)
            if error:
                reason = error
            elif self.address_is_denied(resolved) and not (
                sni and self.internal_rule_allows(sni, port, resolved)
            ):
                reason = f"TLS SNI {sni or '<missing>'} is not authorized for internal destination {resolved}"

        if reason:
            data.context.client.error = reason
            ctx.log.warn(f"Blocked proxy TLS connection: {reason}")

    def server_disconnected(self, data):
        self.server_authorizations.pop(data.server.id, None)

    def server_connect_error(self, data):
        self.server_authorizations.pop(data.server.id, None)

    def http_connect(self, flow: http.HTTPFlow):
        host = flow.request.host
        if self.domain_is_blocked(host):
            flow.response = http.Response.make(
                403, b"Blocked by nixvm hostname policy.\n",
                {"content-type": "text/plain; charset=utf-8"},
            )

    def request(self, flow: http.HTTPFlow):
        host = self.normalize_domain(flow.request.host)
        reason = None
        if self.domain_is_blocked(host):
            reason = f"blocked by nixvm hostname policy: {host}"
        else:
            sni = getattr(flow.client_conn, "sni", None)
            authorization = self.server_authorizations.get(flow.server_conn.id)
            if authorization and self.address_is_denied(authorization["ip"]):
                try:
                    host_ip = ipaddress.ip_address(host).compressed
                except ValueError:
                    host_ip = None
                host_matches = (
                    host == authorization["hostname"] or host_ip == authorization["ip"]
                )
                if not host_matches:
                    reason = (
                        f"HTTP hostname {host} does not match authorized internal endpoint "
                        f"{authorization['hostname']}"
                    )
                elif not sni or self.normalize_domain(sni) != authorization["hostname"]:
                    reason = (
                        f"TLS SNI {self.normalize_domain(sni) if sni else '<missing>'} "
                        "does not match authorized "
                        f"internal hostname {authorization['hostname']}"
                    )
            else:
                resolved, resolve_error = self.resolve_and_pin(host, flow.request.port)
                if resolve_error:
                    reason = resolve_error
                elif sni and self.normalize_domain(sni) != host:
                    reason = f"HTTP hostname {host} does not match TLS SNI {self.normalize_domain(sni)}"
                else:
                    address = flow.server_conn.address
                    actual = address[0] if address else resolved
                    try:
                        actual_ip = ipaddress.ip_address(actual.split("%", 1)[0]).compressed
                    except ValueError:
                        actual_ip = resolved
                    if self.address_is_denied(actual_ip) and not self.internal_rule_allows(
                        host, flow.request.port, actual_ip
                    ):
                        reason = (
                            f"HTTP hostname {host} is not allowed for internal destination "
                            f"{actual_ip}:{flow.request.port}"
                        )
        if reason:
            ctx.log.warn(f"Blocked proxy request: {reason}")
            flow.response = http.Response.make(
                403, b"Blocked by nixvm destination policy.\n",
                {"content-type": "text/plain; charset=utf-8"},
            )


addons = [NixvmPolicy()]
