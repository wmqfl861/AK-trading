from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest
from ak_trading.v2.contracts import Horizon, SessionWindow
from ak_trading.v2.validation.splits import MINUTE, SampleInterval, SplitSpec, assign_splits, embargo_end


def calendar(count=100):
    result = []
    for i in range(count):
        day = date(2026, 1, 1) + timedelta(days=i)
        start = i * 1440 * MINUTE
        for product in ("SHFE.RB", "SHFE.CU"):
            for j, (lo, hi) in enumerate(((0, 150), (270, 360))):
                result.append(SessionWindow(f"{product}-{i}-{j}", product, day, start+lo*MINUTE,
                                             start+hi*MINUTE, "DAY", "synthetic", "SCENARIO"))
    return tuple(result)


def split():
    return SplitSpec("fold", 0, 2*1440*MINUTE, 2*1440*MINUTE, 40*1440*MINUTE,
                     40*1440*MINUTE, 80*1440*MINUTE, 60, 20, False)


def sample(name, day, start, end, unit="TRADING_MINUTE", h=1):
    return SampleInterval(name, "SHFE.RB", date(2026, 1, 1)+timedelta(days=day),
                          start*MINUTE, end*MINUTE, end*MINUTE, end*MINUTE, Horizon(unit, h))


@pytest.mark.parametrize("end", [15, 30, 60])
def test_closed_influence_purged_at_boundary(end):
    s = SplitSpec("tiny", 0, 15*MINUTE, 15*MINUTE, 2*1440*MINUTE,
                  2*1440*MINUTE, 90*1440*MINUTE, 60, 20, False)
    actual = assign_splits((sample("a", 0, 0, end),), s, calendar())
    assert actual[0].partition == "PURGED"


def test_holding_and_availability_not_just_label_control_purge():
    a = sample("a", 1, 1440+100, 1440+101)
    a = replace(a, holding_end_nano=2*1440*MINUTE)
    assert assign_splits((a,), split(), calendar())[0].partition == "PURGED"
    a = replace(a, holding_end_nano=a.label_end_nano, available_at_nano=2*1440*MINUTE)
    assert assign_splits((a,), split(), calendar())[0].partition == "PURGED"


def test_sixty_trading_minutes_skip_lunch():
    a = sample("a", 0, 100, 101)
    assert embargo_end(120*MINUTE, (a,), split(), calendar()) == 300*MINUTE


def test_mixed_daily_horizon_uses_twenty_trading_days():
    a = sample("minute", 0, 100, 101)
    d = sample("daily", 0, 100, 19*1440+360, "TRADING_DAY", 20)
    end = embargo_end(2*1440*MINUTE, (a, d), split(), calendar())
    assert end >= (21*1440+360)*MINUTE
    probe = sample("probe", 3, 3*1440+100, 3*1440+101)
    assigned = {r.sample_id: r for r in assign_splits((a,d,probe), split(), calendar())}
    assert assigned["probe"].partition == "EMBARGOED"


def test_same_date_across_contracts_not_split_by_intraday_embargo():
    a = sample("a", 2, 2*1440+1, 2*1440+2)
    b = replace(sample("b", 2, 2*1440+100, 2*1440+101), product="SHFE.CU")
    assert {r.partition for r in assign_splits((a,b), split(), calendar())} == {"EMBARGOED"}


def test_night_calendar_uses_trading_day_not_utc_day():
    sessions = (SessionWindow("night", "SHFE.RB", date(2026,1,5), 0, 120*MINUTE,
                              "NIGHT", "synthetic", "SCENARIO"),)
    a = SampleInterval("night", "SHFE.RB", date(2026,1,5), MINUTE, 2*MINUTE, 2*MINUTE, 2*MINUTE,
                       Horizon("TRADING_MINUTE",1))
    s = SplitSpec("night",0,121*MINUTE,122*MINUTE,200*MINUTE,201*MINUTE,300*MINUTE,60,20,False)
    assert assign_splits((a,),s,sessions)[0].partition == "TRAIN"
    with pytest.raises(ValueError, match="trading_day"):
        assign_splits((replace(a,trading_day=date(2026,1,4)),),s,sessions)


def test_unmatured_tail_not_zero_label_and_duplicate_rejected():
    a=sample("a",79,79*1440+1,110*1440)
    assert assign_splits((a,),split(),calendar())[0].partition == "UNMATURED"
    with pytest.raises(ValueError, match="duplicate"):
        assign_splits((a,a),split(),calendar())


def test_after_close_daily_publication_keeps_explicit_trading_day():
    s=split();a=sample("daily-close",1,1440+361,1440+362,"TRADING_DAY",1)
    assigned=assign_splits((a,),s,calendar())
    assert assigned[0].partition=="TRAIN"
    with pytest.raises(ValueError,match="trading_day"):
        assign_splits((replace(a,trading_day=date(2026,1,3)),),s,calendar())


def test_future_values_never_enter_partition_inputs():
    a = sample("a",1,1441,1442)
    first = assign_splits((a,),split(),calendar())
    assert first == assign_splits((a,),split(),calendar(120))
    assert first[0].partition == "TRAIN"
