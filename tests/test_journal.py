import os
import tempfile

from predictionedge.journal import Journal, JournalEntry


def _j():
    return Journal(os.path.join(tempfile.mkdtemp(), "j.jsonl"))


def _e(jid="m:Yes", status="taken", cost=40.0, contracts=100, conviction=0.6):
    return JournalEntry(id=jid, ts=1.0, title="T", side_label="YES on: T",
                        entry_price=0.40, contracts=contracts, cost=cost,
                        conviction=conviction, status=status)


def test_empty_journal_summary():
    s = _j().summary()
    assert s["logged"] == 0 and s["hit_rate"] is None and s["pnl"] == 0.0


def test_record_and_read_back():
    j = _j()
    j.record(_e())
    assert j.latest()["m:Yes"]["status"] == "taken"
    assert j.summary()["taken"] == 1


def test_last_write_wins_per_id():
    j = _j()
    j.record(_e(status="taken"))
    j.record(_e(status="won"))
    assert j.latest()["m:Yes"]["status"] == "won"
    assert j.summary()["logged"] == 1


def test_hit_rate_and_pnl_over_settled_rows():
    j = _j()
    # two winners at $40 for 100 contracts ($100 back each), one loser at $40
    j.record(_e(jid="a", status="won"))
    j.record(_e(jid="b", status="won"))
    j.record(_e(jid="c", status="lost"))
    s = j.summary()
    assert s["won"] == 2 and s["lost"] == 1
    assert s["hit_rate"] == 0.667          # rounded for display
    assert abs(s["staked"] - 120.0) < 1e-9
    assert abs(s["pnl"] - 80.0) < 1e-9        # 200 back - 120 staked


def test_skipped_rows_do_not_count_as_taken():
    j = _j()
    j.record(_e(jid="a", status="skipped"))
    s = j.summary()
    assert s["skipped"] == 1 and s["taken"] == 0 and s["open"] == 0


def test_open_is_taken_minus_settled():
    j = _j()
    j.record(_e(jid="a", status="taken"))
    j.record(_e(jid="b", status="won"))
    assert j.summary()["open"] == 1


def test_avg_conviction_of_taken():
    j = _j()
    j.record(_e(jid="a", status="taken", conviction=0.4))
    j.record(_e(jid="b", status="won", conviction=0.8))
    j.record(_e(jid="c", status="skipped", conviction=0.1))
    assert abs(j.summary()["avg_conviction_taken"] - 0.6) < 1e-9


def test_torn_line_is_skipped_not_fatal():
    j = _j()
    j.record(_e())
    with j.path.open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")
    assert j.summary()["logged"] == 1
