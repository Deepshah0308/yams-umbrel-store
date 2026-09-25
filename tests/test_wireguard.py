import pytest

import server


def test_cleans_a_typical_provider_file():
    conf, host = server.clean_wireguard("""
[Interface]
# made by a VPN provider
PrivateKey = aaaa
Address = 10.2.0.2/32, fd00::2/128
DNS = 10.2.0.1

[Peer]
PublicKey = bbbb
PresharedKey = cccc
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = 185.1.2.3:51820
""")
    assert host == "185.1.2.3"
    assert "Address = 10.2.0.2/32\n" in conf          # IPv6 removed
    assert "Endpoint = 185.1.2.3:51820" in conf
    assert "AllowedIPs = 0.0.0.0/0\n" in conf
    assert "PresharedKey = cccc" in conf
    assert "DNS" not in conf


@pytest.mark.parametrize("text, message", [
    ("hello", "doesn't look like a WireGuard file"),
    ("[Interface]\nPrivateKey = a\n", "both an [Interface] and a [Peer]"),
    ("[Interface]\nAddress = 10.0.0.2/32\n[Peer]\nPublicKey = b\nEndpoint = 1.2.3.4:51820\n", "PrivateKey is missing"),
    ("[Interface]\nPrivateKey = a\nAddress = fd00::2/128\n[Peer]\nPublicKey = b\nEndpoint = 1.2.3.4:51820\n", "IPv4 Address"),
    ("[Interface]\nPrivateKey = a\nAddress = 10.0.0.2/32\n[Peer]\nPublicKey = b\nEndpoint = 1.2.3.4:abc\n", "port should be a number"),
])
def test_explains_what_is_wrong(text, message):
    with pytest.raises(server.VpnError, match=message.replace("[", r"\[").replace("]", r"\]")):
        server.clean_wireguard(text)
