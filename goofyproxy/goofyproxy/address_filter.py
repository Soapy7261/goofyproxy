import ipaddress
from enum import IntEnum


ADDRESS_FILTER_HELP = """
an address filter is a semicolon-separated list of patterns
of the form <host-pattern>:<port-pattern>.

host patterns:
  *                   : any host
  *.example.com       : example.com and all its subdomains
  example.com         : exactly example.com
  1.2.3.4             : exact IPv4 address
  10.0.0.0/8          : IPv4 CIDR network
  [::1]               : exact IPv6 address (must be in
                        brackets because it contains colons)
  [fc00::]/7          : IPv6 CIDR network (also in brackets)

port patterns:
  *                   : any port
  1080                : exact port
  80,443              : multiple ports
  0-442,444-65535     : comma-separated ranges

examples:
  *.example.com:*     : example.com and all its subdomains
                        on any port
  example.com:*       : exactly example.com on any port
  example.com:22      : exactly example.com on port 22
  10.0.0.0/8:*        : IPv4 CIDR network
  1.2.3.4:*           : 1.2.3.4 on any port
  *:1080-1100         : any host on ports 1080 to 1100
  *:443,22-80         : any host on port 443 or range 22-80
  [2001:db8:3333:4444:5555:6666:7777:8888]:* : IPv6 host
  *:0-442,444-65535   : any port except 443
  *:80;*.org:*        : two semicolon-separated patterns

bad examples:
  example.com         : missing a port pattern!
  *example.com:*      : matches exactly '*example.com', not
                        any host ending with 'example.com'.
  exam*.com:*         : matches exactly 'exam*.com', not any
                        host with something between 'exam'
                        and '.com'.
  *:80 *.org:*        : multiple patterns must be separated
                        by semicolons, not whitespaces.
""".strip()


ADDRESS_FILTER_LAN = (
    "localhost:*;"
    "10.0.0.0/8:*;"
    "172.16.0.0/12:*;"
    "192.168.0.0/16:*;"
    "127.0.0.0/8:*;"
    "169.254.0.0/16:*;"
    "[::1]:*;"
    "[fc00::]/7:*;"
    "[fe80::]/10:*;"
)


class AddressFilterType(IntEnum):
    Block = 0
    Allow = 1


def _match_port(port: str, port_pattern: str) -> bool:
    """
    returns True if port matches given port pattern. examples:
      `*`               -> any port
      `80`              -> exact port
      `80,443`          -> multiple ports
      `0-442,444-65535` -> comma-separated ranges
    """

    if not isinstance(port_pattern, str):
        raise ValueError(
            f"port_pattern must be a str, not {type(port_pattern)}"
        )

    if port_pattern == '*':
        return True

    try:
        port_int = int(port)
    except ValueError:
        # port is not numeric
        return False

    # split by comma, strip whitespace
    for token in port_pattern.split(','):
        token = token.strip()
        if not token:
            continue
        if '-' in token:
            # range "low-high"
            low_str, high_str = token.split('-', 1)
            try:
                low = int(low_str.strip())
                high = int(high_str.strip())
            except ValueError:
                continue
            if low <= port_int <= high:
                return True
        else:
            # exact port
            try:
                if int(token) == port_int:
                    return True
            except ValueError:
                continue
    return False


def match_address(address: str, filter: str) -> bool:
    """
    returns True if address matches filter. see constant `ADDRESS_FILTER_HELP`
    for more details on the format.
    """

    if not isinstance(address, str):
        raise ValueError(
            f"address must be a str, not {type(address)}"
        )
    if not isinstance(filter, str):
        raise ValueError(
            f"filter must be a str, not {type(filter)}"
        )

    if not filter.strip():
        return False

    # normalise address: host, port (last colon is the separator)
    try:
        host, port = address.rsplit(':', 1)
    except Exception:
        return False

    # add brackets to IPv6 addresses that aren't already bracketed
    if ':' in host and not host.startswith('['):
        host_canonical = f'[{host}]'
    else:
        host_canonical = host

    for pattern in filter.split(';'):
        pattern = pattern.strip()
        if not pattern:
            continue

        try:
            host_pattern, port_pattern = pattern.rsplit(':', 1)
        except Exception:
            continue
        host_pattern = host_pattern.strip()
        port_pattern = port_pattern.strip()

        # match port
        if not _match_port(port, port_pattern):
            continue

        # match host

        # wildcard: any host (*)
        if host_pattern == '*':
            return True

        # wildcard: subdomain (*.suffix)
        if host_pattern.startswith('*.'):
            suffix = host_pattern[2:]  # e.g., ".example.com"
            if host == suffix[1:] or host.endswith(suffix):
                return True
            continue

        # IP network (CIDR or single IP), strip brackets if IPv6
        net_str = host_pattern
        if net_str.startswith('[') and net_str.endswith(']'):
            net_str = net_str[1:-1]

        try:
            net = ipaddress.ip_network(net_str, strict=False)
            addr_ip_str = host_canonical.strip('[]')
            addr_ip = ipaddress.ip_address(addr_ip_str)
            if addr_ip in net:
                return True
        except ValueError:
            # not a network, treat as exact host name / literal IP string
            if host == host_pattern:
                return True

    # no pattern matched
    return False


def is_address_allowed(
    address: str,
    filter: str,
    filter_type: AddressFilterType
) -> bool:
    """
    if filter_type is Allow, returns True if address matches filter. if
    filter_type is Block, returns False if address matches filter. see constant
    `ADDRESS_FILTER_HELP` for more details on the format.
    """

    if not isinstance(address, str):
        raise ValueError(
            f"address must be a str, not {type(address)}"
        )
    if not isinstance(filter, str):
        raise ValueError(
            f"filter must be a str, not {type(filter)}"
        )

    matched = match_address(address, filter)
    return matched if filter_type == AddressFilterType.Allow else not matched
