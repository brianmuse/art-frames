"""How people reach Art Frames on the home network: the trusted Host names, the LAN addresses,
the start-up banner and the mDNS name (art-frames.local).

mDNS works only with host networking. In a Docker bridge network, multicast stays inside the
Docker network, so phones on the LAN never see the name. Art Frames then skips it and logs why.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket

from . import qr

log = logging.getLogger(__name__)

DEFAULT_NAME = "art-frames"
SERVICE_TYPE = "_http._tcp.local."
INSTANCE = "Art Frames"
# Interfaces that Docker, VMs and VPN tools add on the host. A phone can't reach these addresses.
VIRTUAL = re.compile(r"^(docker|br-|veth|virbr|cni|flannel|vmnet|vboxnet|lxc|podman|utun|awdl|llw|bridge|zt|tailscale|wg)")
DOCKER_NETWORK = ipaddress.ip_network("172.16.0.0/12")
LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


# Host names. ART_FRAMES_HOSTS lists the names Art Frames answers to. "*" means any home network
# name: a private or loopback IP address, a name that ends in .local, or a name with no dots.
# A public web page can't send these through a visitor's browser (DNS rebinding), because
# public DNS names always have a dot and never end in .local.

def _split_host(host):
    host = (host or "").strip().lower()
    if host.startswith("["):
        return host[1:host.find("]")] if "]" in host else ""
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def lan_host(host):
    name = _split_host(host)
    if not name:
        return False
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        labels = name.rstrip(".").split(".")
        if not all(LABEL.match(label) for label in labels):
            return False
        return len(labels) == 1 or labels[-1] == "local"
    if address.version == 6 and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_private or address.is_loopback


def host_allowed(host, names):
    """Whether a request's Host header is one Art Frames answers to. `names` comes from ART_FRAMES_HOSTS."""
    name = _split_host(host)
    if not name:
        return False
    listed = {_split_host(item) for item in names if item and item != "*"}
    return name in listed or "*" in names and lan_host(host)


# Addresses.

def interfaces():
    """[(interface name, IPv4 address)] of this machine (or of this container)."""
    try:
        import ifaddr  # zeroconf depends on it
    except ImportError:  # pragma: no cover - zeroconf always installs it
        return []
    found = []
    for adapter in ifaddr.get_adapters():
        for item in adapter.ips:
            if isinstance(item.ip, str):
                found.append((adapter.nice_name or adapter.name, item.ip))
    return found


def lan_addresses(found=None):
    """The private IPv4 addresses that other devices on the home network can use, best first."""
    found = interfaces() if found is None else found
    result = []
    for name, ip in found:
        address = ipaddress.ip_address(ip)
        if address.is_loopback or address.is_link_local or not address.is_private or VIRTUAL.match(name):
            continue
        if ip not in result:
            result.append(ip)
    # 192.168.x.x and 10.x.x.x are home networks more often than 172.16-31.x.x.
    return sorted(result, key=lambda ip: ipaddress.ip_address(ip) in DOCKER_NETWORK)


def in_bridge_network(found=None, *, dockerenv="/.dockerenv"):
    """Whether this runs inside a Docker bridge network (not host networking).

    Inside a bridge network the container sees only its own loopback and one 172.16.0.0/12
    address. With host networking it sees the host's interfaces, including Docker's own bridge.
    """
    if not os.path.exists(dockerenv):
        return False
    found = interfaces() if found is None else found
    names = {name for name, _ in found}
    if any(VIRTUAL.match(name) for name in names):
        return False
    addresses = [ipaddress.ip_address(ip) for _, ip in found if not ipaddress.ip_address(ip).is_loopback]
    return bool(addresses) and all(address in DOCKER_NETWORK for address in addresses)


class Reach:
    """Where Art Frames listens, and the addresses it shows in the log and on the Server page."""

    def __init__(self, bind, port, *, mdns_name=None, found=None, bridge=None):
        self.bind, self.port = bind, int(port)
        self.mdns_name = mdns_name  # set once the mDNS name is registered
        self.found = interfaces() if found is None else found
        self.bridge = in_bridge_network(self.found) if bridge is None else bridge
        self.advertiser = None

    @property
    def local_only(self):
        return ipaddress.ip_address(self.bind).is_loopback

    def url(self, host):
        return f"http://{host}" + ("" if self.port == 80 else f":{self.port}")

    def urls(self):
        """Addresses for other devices. Empty when Art Frames accepts only local connections, or when it
        runs in a bridge network, where it can't see the host's address."""
        if self.local_only or self.bridge:
            return []
        hosts = [f"{self.mdns_name}.local"] if self.mdns_name else []
        if ipaddress.ip_address(self.bind).is_unspecified:
            hosts += lan_addresses(self.found)
        else:
            hosts.append(self.bind)
        return [self.url(host) for host in hosts]

    def banner(self, setup_code=None, *, qr_code=True):
        """The text Art Frames prints when it starts: the address, the setup code and link if needed, and a QR
        code. `qr_code` is False when the log is not a terminal: a log viewer shows the blocks as noise."""
        urls = self.urls()
        lines = ["", "Art Frames is running."]
        if self.local_only:
            lines.append(f"It accepts connections only from this computer: {self.url(self.bind)}")
        elif self.bridge:
            lines.append(f"Open http://<this computer's address>:{self.port} in a browser.")
            lines.append(f"On the computer that runs Docker, use http://localhost:{self.port}")
        else:
            lines.append("Open one of these addresses on a phone or computer on your home network:")
            lines += [f"  {url}" for url in urls]
        target = next((url for url in urls if not url.startswith(f"http://{self.mdns_name}.")), urls[0] if urls else None)
        if setup_code:
            lines += ["", f"Setup code: {setup_code}  (it works for 1 hour; a restart makes a new one)"]
            if target:
                lines.append(f"Or open this address to finish setup: {target}/setup?code={setup_code}")
        if qr_code and target and not self.local_only:
            link = f"{target}/setup?code={setup_code}" if setup_code else target
            lines += ["", "Scan this with your phone's camera:", qr.terminal(link)]
        return "\n".join(lines) + "\n"


# mDNS.

def mdns_wanted(setting, reach):
    """(advertise?, the reason for the log). `setting` is ART_FRAMES_MDNS: "auto" (the default), "1" or "0"."""
    setting = (setting or "auto").strip().lower()
    if setting in ("0", "off", "false", "no"):
        return False, "mDNS is off (ART_FRAMES_MDNS=0)"
    if reach.local_only:
        return False, "mDNS is off: Art Frames accepts connections only from this computer"
    if setting in ("1", "on", "true", "yes"):
        return True, ""
    if reach.bridge:
        return False, "mDNS is off: a Docker bridge network keeps it from the home network. Use host networking"
    if not lan_addresses(reach.found) and ipaddress.ip_address(reach.bind).is_unspecified:
        return False, "mDNS is off: no home network address found"
    return True, ""


def mdns_name(value):
    name = (value or DEFAULT_NAME).strip().lower().removesuffix(".local")
    if not LABEL.match(name):
        raise ValueError("ART_FRAMES_MDNS_NAME must be letters, numbers and dashes, such as art-frames")
    return name


class Advertiser:
    """Registers <name>.local and an "Art Frames" web service with python-zeroconf."""

    def __init__(self, name, port, addresses, *, factory=None):
        self.name, self.port, self.addresses = name, port, addresses
        self.factory = factory
        self.zeroconf = self.info = None
        self.instance = None  # the service name that zeroconf registered, after start()

    def start(self):
        from zeroconf import IPVersion, ServiceInfo, Zeroconf

        factory = self.factory or (lambda: Zeroconf(interfaces=self.addresses, ip_version=IPVersion.V4Only))
        self.info = ServiceInfo(
            SERVICE_TYPE,
            f"{INSTANCE}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(ip) for ip in self.addresses],
            port=self.port,
            server=f"{self.name}.local.",
            properties={"path": "/"},
        )
        self.zeroconf = factory()
        # On a name clash, zeroconf renames the service (for example "Art Frames (2)"). It does not check the
        # host name: two installs with the same ART_FRAMES_MDNS_NAME both answer to it.
        self.zeroconf.register_service(self.info, allow_name_change=True)
        self.instance = self.info.name.removesuffix("." + SERVICE_TYPE)
        return f"{self.name}.local"

    def stop(self):
        if self.zeroconf is None:
            return
        try:
            self.zeroconf.unregister_service(self.info)
        finally:
            self.zeroconf.close()
            self.zeroconf = None


def advertise(reach, setting, name_setting, *, factory=None):
    """Start mDNS when it can work. Sets reach.mdns_name on success. Never stops Art Frames on failure."""
    wanted, reason = mdns_wanted(setting, reach)
    if not wanted:
        log.info(reason)
        return None
    try:
        name = mdns_name(name_setting)
        addresses = lan_addresses(reach.found) if ipaddress.ip_address(reach.bind).is_unspecified else [reach.bind]
        advertiser = Advertiser(name, reach.port, addresses, factory=factory)
        reach.mdns_name = advertiser.start().removesuffix(".local")
    except Exception as error:  # a busy port 5353, no multicast, a bad name
        log.warning("mDNS could not start (%s). Use the IP address instead", error)
        return None
    reach.advertiser = advertiser
    log.info("mDNS: Art Frames answers at %s.local, as the web service \"%s\"", reach.mdns_name, advertiser.instance)
    if advertiser.instance != INSTANCE:
        log.warning("mDNS: another device on the network already uses the service name \"%s\", so this one is "
                    "\"%s\". If it is a second Art Frames, give each one its own ART_FRAMES_MDNS_NAME, or both "
                    "answer to %s.local", INSTANCE, advertiser.instance, reach.mdns_name)
    return advertiser
