import os
import tempfile
from dataclasses import replace

from predictionedge.config import Config
from predictionedge.doctor import ready_for_live_data, run_checks


def test_no_creds_not_ready():
    checks = run_checks(Config(), network=False)
    assert not ready_for_live_data(checks)
    names = {n for n, _, _ in checks}
    assert {"kalshi creds", "odds api key", "trading mode"} <= names


def test_killswitch_flagged():
    d = tempfile.mkdtemp()
    ks = os.path.join(d, "STOP")
    open(ks, "w").close()
    checks = run_checks(replace(Config(), killswitch_path=ks), network=False)
    status = {n: ok for n, ok, _ in checks}
    assert status["kill switch"] is False


def test_creds_present_but_missing_key_file():
    cfg = replace(Config(), kalshi_key_id="k", kalshi_private_key_path="does-not-exist.pem",
                  odds_api_key="o")
    status = {n: ok for n, ok, _ in run_checks(cfg, network=False)}
    assert status["kalshi creds"] is True
    assert status["kalshi private key"] is False
