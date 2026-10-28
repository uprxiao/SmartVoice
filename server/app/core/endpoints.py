"""Static endpoint policy; no DNS resolution or network access in config."""
import ipaddress
from urllib.parse import urlsplit


def is_local_endpoint(base_url):
    try:
        url = urlsplit(base_url)
        if url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password:
            return False
        if url.hostname == 'localhost':
            return True
        address = ipaddress.ip_address(url.hostname)
        ranges = ('127.0.0.0/8', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '::1/128', 'fc00::/7')
        return any(address in ipaddress.ip_network(n) for n in ranges)
    except ValueError:
        return False
