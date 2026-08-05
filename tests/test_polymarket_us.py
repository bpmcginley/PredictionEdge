import base64
import os
import tempfile

from predictionedge.polymarket_us import (
    MockPolymarketUSClient,
    PMUSMarket,
    PolymarketUSClient,
)


def _write_ed25519_pem() -> tuple[str, object]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    sk = Ed25519PrivateKey.generate()
    pem = sk.private_bytes(serialization.Encoding.PEM,
                           serialization.PrivateFormat.PKCS8,
                           serialization.NoEncryption())
    path = os.path.join(tempfile.mkdtemp(), "ed25519.pem")
    with open(path, "wb") as f:
        f.write(pem)
    return path, sk


def test_ed25519_signature_verifies_pem():
    path, sk = _write_ed25519_pem()
    client = PolymarketUSClient(key_id="k1", private_key_path=path)
    headers = client._headers("POST", "/v1/orders")
    assert headers["X-PM-Access-Key"] == "k1"
    # Verified contract: signed string is {ts}{METHOD}{path}, NO body.
    sig = base64.b64decode(headers["X-PM-Signature"])
    msg = (headers["X-PM-Timestamp"] + "POST" + "/v1/orders").encode()
    sk.public_key().verify(sig, msg)


def test_base64_raw_ed25519_secret_loads_and_signs():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    sk = Ed25519PrivateKey.generate()
    seed = sk.private_bytes(serialization.Encoding.Raw,
                            serialization.PrivateFormat.Raw,
                            serialization.NoEncryption())
    path = os.path.join(tempfile.mkdtemp(), "secret.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(base64.b64encode(seed).decode())
    client = PolymarketUSClient(key_id="k1", private_key_path=path)
    headers = client._headers("GET", "/v1/account/balances")
    sig = base64.b64decode(headers["X-PM-Signature"])
    msg = (headers["X-PM-Timestamp"] + "GET" + "/v1/account/balances").encode()
    sk.public_key().verify(sig, msg)


def test_headers_empty_without_creds():
    assert PolymarketUSClient()._headers("GET", "/v1/markets") == {}


def test_mock_client_market_and_order():
    m = PMUSMarket("chiefs-win", yes_bid=0.50, yes_ask=0.52, last_px=0.51)
    c = MockPolymarketUSClient(markets={"chiefs-win": m}, balance=123.45)
    assert c.market("chiefs-win").yes_ask == 0.52
    assert c.market("missing") is None
    o = c.create_order(market_slug="chiefs-win", intent="ORDER_INTENT_BUY_LONG",
                       price=0.52, quantity=10)
    assert o["state"] == "OPEN" and len(c.placed) == 1
