import os
import tempfile
from dataclasses import replace

from predictionedge import control
from predictionedge.config import Config


def _cfg():
    return replace(Config(), control_path=os.path.join(tempfile.mkdtemp(), "control.json"))


def test_armed_defaults_false_when_missing():
    assert control.armed(_cfg()) is False


def test_set_and_read_roundtrip():
    cfg = _cfg()
    control.set_armed(cfg, True)
    assert control.armed(cfg) is True
    control.set_armed(cfg, False)
    assert control.armed(cfg) is False


def test_corrupt_file_reads_false():
    cfg = _cfg()
    with open(cfg.control_path, "w", encoding="utf-8") as f:
        f.write("not json")
    assert control.armed(cfg) is False
