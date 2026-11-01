"""mitmproxy addon used by the host-side nixvm Podman container."""

from __future__ import annotations

import ipaddress
import json
import os
import socket

from mitmproxy import ctx, http


class NixvmPolicy:
    def __init__(self):
        policy_path = os.environ.get("NIXVM_MITMPROXY_POLICY", "/etc/mitmproxy/policy.json")
        with open(policy_path, encoding="utf-8") as source:
            policy = json.load(source)
        self.blocked_domains = tuple(
            self.normalize_domain(domain) for domain in policy.get("blocked_domains", [])
        )
        self.denied_networks = tuple(
            ipaddress.ip_network(cidr) for cidr in policy.get("deny_cidrs", [])
        )

    @staticmethod
    def normalize_domain(domain: str) -> str:
        return domain.strip().rstrip(".").lower()

    def domain_is_blocked(self, host: str) -> bool:
        host = self.normalize_domain(host)
        try:
            host = ipaddress.ip_address(host).compressed
        except ValueError:
            pass
        return any(host == domain or host.endswith("." + domain)
                   for domain in self.blocked_domains)

    def resolve_and_pin(self, host: str, port: int):
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
            if any(parsed.version == network.version and parsed in network
                   for network in self.denied_networks):
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
        data.server.address = (resolved, port)
        if data.server.tls:
            try:
                ipaddress.ip_address(host)
            except ValueError:
                data.server.sni = host

    def http_connect(self, flow: http.HTTPFlow):
        host = flow.request.host
        if self.domain_is_blocked(host):
            flow.response = http.Response.make(
                403, b"Blocked by nixvm hostname policy.\n",
                {"content-type": "text/plain; charset=utf-8"},
            )

    def request(self, flow: http.HTTPFlow):
        if self.domain_is_blocked(flow.request.host):
            flow.response = http.Response.make(
                403, b"Blocked by nixvm hostname policy.\n",
                {"content-type": "text/plain; charset=utf-8"},
            )


addons = [NixvmPolicy()]
