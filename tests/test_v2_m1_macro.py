"""M1-05 macro: async refresh, publication states, revisions, expiry, tags.

Async coroutines are driven by a tiny explicit coroutine driver with a
controllable gate instead of an asyncio event loop: the offline guard
forbids the loop's internal socketpair, while the transport protocol
itself stays async/await exactly as the production runtime uses it.
"""
from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import date
from typing import Any

import pytest

from ak_trading.v2 import contracts as c
from ak_trading.v2 import macro as m
from ak_trading.v2 import schemas as s

M = 60_000_000_000
H = 3_600_000_000_000


def quality() -> c.DataQuality:
    return c.DataQuality(status="READY", reasons=(), temporal_basis="observed",
                         input_hash=hashlib.sha256(b"macro-test").hexdigest())


def fact(item_id: str, text: str, *, published: int, first_seen: int | None = None,
         retrieved: int | None = None, available: int | None = None,
         valid_until: int | None = None, products: tuple[str, ...] = (),
         urls: tuple[str, ...] = ("https://example.invalid/x",),
         meeting_id: str | None = None, kind: str = "FACT",
         publication_state: str = "published", degradation: str = "normal",
         unit: str | None = None) -> c.MacroItem:
    first_seen = published + 4 * M if first_seen is None else first_seen
    retrieved = first_seen if retrieved is None else retrieved
    available = max(first_seen, retrieved) if available is None else available
    valid_until = published + H if valid_until is None else valid_until
    return c.MacroItem(
        item_id=item_id, meeting_id=meeting_id, revision="initial", kind=kind,
        derived_tag=None, derivation_version=None, published_at_nano=published,
        first_seen_nano=first_seen, retrieved_at_nano=retrieved, available_at_nano=available,
        valid_from_nano=published, valid_until_nano=valid_until,
        publication_state=publication_state, degradation_state=degradation,
        assets=(), products=products, unit=unit, source_urls=urls, text=text,
        parent_ids=(), raw_sha256=hashlib.sha256(item_id.encode()).hexdigest(),
        quality=quality())


def hand_times() -> dict[str, int]:
    """Doc §9.7: published 09:00, first seen 09:04, fetched 09:05, verified 09:06."""
    base = 9 * H
    return {"published": base, "first_seen": base + 4 * M, "retrieved": base + 5 * M,
            "verified": base + 6 * M, "expires": base + H}


def test_macro_hand_times_and_states() -> None:
    times = hand_times()
    item = fact("HAND", "美联储宣布加息", published=times["published"],
                first_seen=times["first_seen"], retrieved=times["retrieved"],
                available=times["verified"], valid_until=times["expires"])
    cache = m.MacroCache((item,))
    # 09:05 snapshot: not yet verified/available -> blocked for a required decision.
    early = cache.snapshot(times["retrieved"], "SHFE.rb", required=True,
                           price_only_validated=False)
    assert early.mode == "BLOCKED"
    assert "macro_required_missing" in early.quality.reasons
    # 09:06 snapshot: usable.
    ready = cache.snapshot(times["verified"], "SHFE.rb", required=True,
                           price_only_validated=False)
    assert ready.mode == "WITH_MACRO"
    assert [entry.item_id for entry in ready.items] == ["HAND"]
    # Expiry at 10:00 is inclusive: the equality instant is already expired.
    expiring = cache.snapshot(times["expires"] - 1, "SHFE.rb", required=True,
                              price_only_validated=False)
    assert expiring.mode == "WITH_MACRO"
    expired = cache.snapshot(times["expires"], "SHFE.rb", required=True,
                             price_only_validated=False)
    assert expired.mode == "BLOCKED"
    assert "macro_item_expired" in expired.quality.reasons
    # Published without a time, degraded, unit-missing opinions: each carries a reason.
    untimed = _rebuild(fact("UNTIMED", "利率决议公布", published=9 * H),
                       published_at_nano=None, valid_until_nano=10 * H)
    degraded = _rebuild(item, item_id="DEGRADED", degradation_state="degraded")
    unitless = fact("OPINION", "RB上行概率0.6", published=8 * H, valid_until=12 * H,
                    meeting_id="meeting-a", kind="ECON_OPINION", unit=None)
    mixed = m.MacroCache((untimed, degraded, unitless))
    snapshot = mixed.snapshot(9 * H + 7 * M, "SHFE.rb", required=True,
                              price_only_validated=False)
    assert snapshot.mode == "BLOCKED"
    assert "macro_item_not_published" in snapshot.quality.reasons
    assert "macro_item_degraded" in snapshot.quality.reasons
    assert "macro_item_unit_missing" in snapshot.quality.reasons
    # Different meeting ids never merge into one item.
    opinion_b = fact("OPINION-B", "RB下行情绪", published=8 * H, valid_until=12 * H,
                     meeting_id="meeting-b", kind="ECON_OPINION", unit="probability")
    opinions = m.MacroCache((unitless, opinion_b))
    kept = opinions.snapshot(9 * H, "SHFE.rb", required=False, price_only_validated=False)
    assert [entry.item_id for entry in kept.items if entry.kind == "ECON_OPINION"] == [
        "OPINION-B"]
    # Optional macro missing: only an explicit price_only_validated degrades, else blocked.
    empty = m.MacroCache(())
    assert empty.snapshot(9 * H, "SHFE.rb", required=False,
                          price_only_validated=True).mode == "PRICE_ONLY"
    assert empty.snapshot(9 * H, "SHFE.rb", required=False,
                          price_only_validated=False).mode == "BLOCKED"
    # Appends only add: published contexts never change afterwards.
    before = cache.snapshot(times["verified"], "SHFE.rb", required=True,
                            price_only_validated=False)
    cache.append((fact("LATER", "新的降息消息", published=10 * H, first_seen=10 * H),))
    after = cache.snapshot(times["verified"], "SHFE.rb", required=True,
                           price_only_validated=False)
    assert before == after
    # Revisions extend the log; conflicting content for one revision is rejected.
    cache.append((_rebuild(item, revision="r2", text="美联储宣布加息50个基点",
                           first_seen_nano=11 * H, retrieved_at_nano=11 * H,
                           available_at_nano=11 * H, valid_until_nano=12 * H),))
    assert len(cache.items) == 3
    with pytest.raises(ValueError):
        cache.append((_rebuild(item, text="篡改内容"),))
    # First-seen is never overwritten by an earlier published_at.
    assert cache.items[0].first_seen_nano == times["first_seen"]
    assert cache.items[0].published_at_nano == times["published"]


def _rebuild(item: c.MacroItem, **overrides: Any) -> c.MacroItem:
    return replace(item, **overrides)


def test_literal_tag_word_table() -> None:
    base = 9 * H
    cases = {
        "GLOBAL_EVENT": "美联储宣布加息25个基点",
        "ENERGY_EVENT": "opec 原油供应 statement",
        "METAL_EVENT": "智利铜矿罢工",
        "AGRI_EVENT": "南美干旱影响大豆",
    }
    for tag, text in cases.items():
        derived = m.derive_macro_tags((fact(f"F-{tag}", text, published=base),), base)
        assert [item.derived_tag for item in derived] == [tag]
        assert derived[0].parent_ids and derived[0].derivation_version == "macro-risk-v2.1.0"
        assert derived[0].products == m.TAG_PRODUCTS[tag]
    # Negations hit on purpose; misses produce nothing; expiry and missing URL exclude.
    assert m.derive_macro_tags((fact("NEG", "不会加息", published=base),), base)
    assert m.derive_macro_tags((fact("MISS", "普通供需新闻", published=base),), base) == ()
    assert m.derive_macro_tags(
        (fact("OLD", "战争升级", published=base - 2 * H, valid_until=base - H),), base) == ()
    assert m.derive_macro_tags(
        (fact("NOURL", "战争升级", published=base, urls=()),), base) == ()
    # ECON_OPINION never triggers direction or risk tags.
    opinion = fact("OP", "原油大涨建议做多", published=base, kind="ECON_OPINION",
                   unit="probability")
    assert m.derive_macro_tags((opinion,), base) == ()
    # Multi-hit parents union; derived inherits earliest cutoff and latest availability.
    first = fact("M1", "制裁导致原油紧张", published=base, first_seen=base + M,
                 available=base + 2 * M, valid_until=base + 10 * M)
    second = fact("M2", "海峡封锁", published=base + M, first_seen=base + 3 * M,
                  available=base + 4 * M, valid_until=base + 6 * M)
    tags = m.derive_macro_tags((first, second), base)
    by_tag = {item.derived_tag: item for item in tags}
    assert set(by_tag) == {"GLOBAL_EVENT", "ENERGY_EVENT"}
    energy = by_tag["ENERGY_EVENT"]
    assert set(energy.parent_ids) == {"M1", "M2"}
    assert energy.valid_until_nano == base + 6 * M
    assert energy.available_at_nano == base + 4 * M
    # Product mapping: ENERGY applies to INE.SC but never to DCE.M; unknown products
    # are flagged MACRO_COVERAGE_UNKNOWN instead of assumed covered.
    cache = m.MacroCache((tags[0],))
    ine = cache.snapshot(base + 5 * M, "INE.sc", required=False, price_only_validated=False)
    assert ine.mode == "WITH_MACRO"
    assert "ENERGY_EVENT" in ine.risk_tags
    dce = cache.snapshot(base + 5 * M, "DCE.m", required=False, price_only_validated=False)
    assert "ENERGY_EVENT" not in dce.risk_tags
    unknown = cache.snapshot(base + 5 * M, "SYN.rb", required=True, price_only_validated=False)
    assert unknown.mode == "BLOCKED"
    assert "MACRO_COVERAGE_UNKNOWN" in unknown.risk_tags


class Gate:
    """Awaitable pause the test controls without any event loop."""

    def __init__(self) -> None:
        self.open = False

    def __await__(self):
        while not self.open:
            yield self
        return None


class CoroutineDriver:
    """Steps a coroutine until it finishes or parks on a closed gate."""

    def __init__(self, coroutine: Any) -> None:
        self.coroutine = coroutine
        self.finished = False
        self.value: Any = None

    def step(self) -> Gate | None:
        if self.finished:
            return None
        try:
            return self.coroutine.send(None)
        except StopIteration as stop:
            self.finished = True
            self.value = stop.value
            return None

    def run(self) -> Any:
        while not self.finished:
            gate = self.step()
            if isinstance(gate, Gate) and not gate.open:
                return None
        return self.value


def run(coroutine: Any) -> Any:
    return CoroutineDriver(coroutine).run()


class HangingTransport:
    def __init__(self, gate: Gate, responses: dict[str, Any]) -> None:
        self.gate = gate
        self.responses = responses
        self.calls: list[str] = []

    async def call_tool(self, name: str, arguments: Any) -> Any:
        self.calls.append(name)
        if self.responses.get(name) == "__hang__":
            await self.gate
        return self.responses.get(name, {"items": []})


def test_async_refresh_and_degradation() -> None:
    base = 9 * H
    iso_published = "2026-01-09T08:00:00+00:00"
    iso_ts = "2026-01-09T08:59:00+00:00"
    responses = {
        "get_pipeline_status": {"checked_at": "2026-01-09T09:00:00+00:00",
                                "active_alerts": [], "kinds": {}},
        "get_recent_meetings": [
            {"meeting_id": "mtg-1", "state": "published",
             "degradation_state": "full_panel", "business_date": "2026-01-09",
             "published_at": iso_published},
            {"meeting_id": "mtg-null", "state": "published",
             "degradation_state": "degraded_two_expert_non_resolution",
             "business_date": "2026-01-09", "published_at": None}],
        "get_frontpage_news": {
            "hero": {"id": "n1", "ts": iso_ts, "title": "美联储加息",
                     "summary": "超级央行周", "url": "https://example.invalid/n1",
                     "importance_score": 90.0},
            "featured": [{"id": "n2", "ts": iso_ts, "title": "no-url story",
                          "summary": "no original url", "url": ""}]},
        "get_investments": [
            {"meeting_id": "mtg-1", "asset": "螺纹钢", "direction": "long",
             "rationale_zh": "方向建议:中性", "probability": 0.5,
             "outcome_status": "open"}],
    }
    clock_values = iter([base + 1, base + 2, base + 3, base + 4, base + 5, base + 6,
                         base + 7, base + 8])
    cache = m.MacroCache(())
    new = run(m.refresh_macro_cache(
        HangingTransport(Gate(), responses), cache, base,
        clock_nano=lambda: next(clock_values)))
    by_tool_kind = [(item.kind, item.degradation_state) for item in new]
    assert ("FACT", "normal") in by_tool_kind
    assert ("ECON_OPINION", "degraded") in by_tool_kind
    # The injected clock stamps receipt and validation: the request-start bound
    # is never used as the response-known time, and first_seen is this round's
    # receipt while published comes from the story ts evidence only.
    news = [item for item in new if item.text == "美联储加息"][0]
    assert news.retrieved_at_nano > base
    assert news.first_seen_nano == news.retrieved_at_nano
    assert news.available_at_nano >= news.retrieved_at_nano
    assert news.valid_until_nano == news.published_at_nano + m.FACT_TTL_NANO
    # A story without an original URL is stored but carries no source URL.
    no_url = [item for item in new if item.text == "no-url story"][0]
    assert no_url.source_urls == ()
    # The degraded meeting (published header without a time) is recorded as
    # unpublished and degraded; it never supplies an investment publication time.
    null_meeting = [item for item in new if item.text.startswith("meeting:mtg-null")][0]
    assert null_meeting.publication_state == "unpublished"
    assert null_meeting.degradation_state == "degraded"
    # Investments join the verified meeting publication time; free-text
    # horizons degrade the opinion; numeric probabilities stay out of fields.
    opinion = [item for item in new if item.text == "方向建议:中性"][0]
    assert opinion.meeting_id == "mtg-1"
    assert opinion.publication_state == "published"
    assert opinion.degradation_state == "degraded"
    assert "opinion_deadline_not_verifiable" in opinion.quality.reasons
    assert opinion.unit == "probability" and opinion.assets == ("螺纹钢",)
    assert cache.items == tuple(new)

    # A hanging long Q&A never blocks minute snapshots: the refresh parks on the
    # gate while three consecutive minute snapshots answer from the cache.
    gate = Gate()
    hanging_cache = m.MacroCache((fact("CACHED", "已有降息事实", published=base,
                                      valid_until=base + 500 * H),))

    refresh = CoroutineDriver(m.refresh_macro_cache(
        HangingTransport(gate, {"get_pipeline_status": "__hang__"}), hanging_cache,
        base + 200 * M, clock_nano=lambda: base + 250 * M))
    parked = refresh.run()
    assert parked is None and not refresh.finished
    modes = [hanging_cache.snapshot(base + 201 * M, "SHFE.rb", required=True,
                                    price_only_validated=False).mode
             for _ in range(3)]
    assert modes == ["WITH_MACRO"] * 3
    gate.open = True
    refresh.run()
    assert refresh.finished

    # Transport exceptions preserve the old cache; no wall-clock refresh of evidence.
    class ExplodingTransport:
        async def call_tool(self, name: str, arguments: Any) -> Any:
            raise RuntimeError("connection lost")

    before = hanging_cache.items
    with pytest.raises(RuntimeError):
        run(m.refresh_macro_cache(ExplodingTransport(), hanging_cache, base + 300 * M,
                                  clock_nano=lambda: base + 400 * M))
    assert hanging_cache.items[:len(before)] == before

    # Unknown response schemas degrade with the raw hash preserved, no guesses.
    clock_two = iter([base + 500 + i for i in range(8)])
    degraded_cache = m.MacroCache(())
    degraded_items = run(m.refresh_macro_cache(
        HangingTransport(Gate(), {"get_frontpage_news": {"unexpected": 1}}),
        degraded_cache, base + 499, clock_nano=lambda: next(clock_two)))
    markers = [item for item in degraded_items if item.degradation_state == "degraded"]
    assert markers and all(len(item.raw_sha256) == 64 for item in markers)
    assert any("response_schema_unknown" in item.quality.reasons for item in markers)

    # A clock running backwards degrades the affected response.
    backwards = iter([base + 600, base + 599] + [base + 601 + i for i in range(6)])
    backwards_cache = m.MacroCache(())
    backwards_items = run(m.refresh_macro_cache(
        HangingTransport(Gate(), {}), backwards_cache, base + 599,
        clock_nano=lambda: next(backwards)))
    assert any("clock_went_backwards" in item.quality.reasons for item in backwards_items)


def test_real_probe_shapes_consumed() -> None:
    """Trimmed from data/maintenance_artifacts/econ-mcp/read-probes-20260913T174223Z.json
    (sanitized offline fixture; no live econ access, no historical backfill)."""
    responses = {
        "get_pipeline_status": {
            "checked_at": "2026-09-13T17:42:22.567224+00:00",
            "active_alerts": [],
            "kinds": {"chair_global_sweep": {"label": "全球新闻扫描", "last_success_at":
                                             "2026-09-13T17:41:19.869540+00:00",
                                             "in_outage": False}},
        },
        "get_recent_meetings": [
            {"meeting_id": "MTG-degraded", "meeting_type": "fixed_daily",
             "state": "published", "degradation_state": "degraded_two_expert_non_resolution",
             "valid_expert_count": 2, "business_date": "2026-09-13",
             "started_at": "2026-09-13T00:02:34.372782+00:00",
             "finalized_at": "2026-09-13T02:15:12.692038+00:00", "published_at": None},
            {"meeting_id": "MTG-full", "meeting_type": "fixed_daily", "state": "published",
             "degradation_state": "full_panel", "valid_expert_count": 3,
             "business_date": "2026-09-12",
             "started_at": "2026-09-12T06:47:02.148448+00:00",
             "finalized_at": "2026-09-12T09:34:54.620108+00:00",
             "published_at": "2026-09-12T09:34:55.412899+00:00"},
        ],
        "get_frontpage_news": {
            "hero": {"id": "s522807d6215dddcb", "source": "东财全球资讯",
                     "ts": "2026-09-13T05:29:02+00:00",
                     "title": "加息25个基点？美联储重磅来袭！高盛最新研判",
                     "summary": "超级央行周，市场普遍预期加息25个基点。",
                     "url": "https://finance.eastmoney.com/a/202609133872799219.html",
                     "lang": "zh", "importance_score": 90.0, "editorial_tier": "featured"},
            "featured": [
                {"id": "oilpb735a5e80d677981", "source": "OilPrice",
                 "ts": "2026-09-12T23:00:00+00:00",
                 "title": "Oil Industry Braces for Years-Long Iran War",
                 "summary": "Bracing for a prolonged war in the Persian Gulf.",
                 "url": "https://oilprice.com/Energy/Oil-Prices/x.html", "lang": "en",
                 "importance_score": 90.0, "editorial_tier": "featured"},
                {"id": "s25b7f3c25e12ce9c", "source": "新浪全球快讯",
                 "ts": "2026-09-13T15:49:03+00:00",
                 "title": "【沙特管道中断，全球石油供应恐损失4%】",
                 "summary": "原油供应紧张推高燃料价格。", "url": "", "lang": "zh",
                 "importance_score": 88.0, "editorial_tier": "featured"},
            ],
        },
        "get_investments": [
            {"meeting_id": "MTG-full", "meeting_date": "2026-09-12",
             "asset": "比特币（BTC）", "direction": "hedge", "conviction": "low",
             "horizon": "嵌套7天窗口（自由文本，非可机核截止）",
             "rationale_zh": "专家组明确拒绝设立纯粹的方向性单元。", "probability": 0.5,
             "outcome_status": "open"},
        ],
    }
    # Received 2026-09-13 06:00 UTC: inside the hero story's 4-hour TTL
    # (published 05:29Z), so the fact is usable and tag-derivable at receipt.
    from datetime import datetime, timezone
    base = int(datetime(2026, 9, 13, 6, 0, 0, tzinfo=timezone.utc).timestamp()) * 10**9
    stamps = iter([base + i for i in range(1, 17)])
    cache = m.MacroCache(())
    items = run(m.refresh_macro_cache(
        HangingTransport(Gate(), responses), cache, base,
        clock_nano=lambda: next(stamps)))
    facts = [item for item in items if item.kind == "FACT"]
    assert len(facts) == 3
    hero = [item for item in facts if "美联储" in item.text][0]
    # Source ts is publication evidence; first_seen is this round's receipt.
    hero_published = int(datetime(2026, 9, 13, 5, 29, 2, tzinfo=timezone.utc)
                         .timestamp()) * 10**9
    assert hero.published_at_nano == hero_published
    assert hero.first_seen_nano > hero.published_at_nano
    assert hero.source_urls and hero.publication_state == "published"
    saudi = [item for item in facts if "沙特" in item.text][0]
    assert saudi.source_urls == ()  # trimmed probe carries an empty url string
    # Tags derive from the real hero text within its TTL.
    derived = m.derive_macro_tags((hero,), hero.first_seen_nano)
    assert [tag.derived_tag for tag in derived] == ["GLOBAL_EVENT"]
    # Meetings: the degraded one records expert degradation and no publication
    # time (state header "published" never becomes a timestamp); the full-panel
    # one publishes at its explicit time.
    degraded_meeting = [item for item in items
                        if item.text == "meeting:MTG-degraded:2026-09-13"][0]
    assert degraded_meeting.publication_state == "unpublished"
    assert degraded_meeting.degradation_state == "degraded"
    full_meeting = [item for item in items
                    if item.text == "meeting:MTG-full:2026-09-12"][0]
    assert full_meeting.publication_state == "published"
    assert full_meeting.degradation_state == "normal"
    # Investments join the verified meeting time; free-text horizons degrade;
    # the remote numeric probability never becomes a market forecast value.
    opinion = [item for item in items
               if item.text == "专家组明确拒绝设立纯粹的方向性单元。"][0]
    assert opinion.published_at_nano == full_meeting.published_at_nano
    assert opinion.degradation_state == "degraded"
    assert "opinion_deadline_not_verifiable" in opinion.quality.reasons
    assert opinion.unit == "probability"
    # No item maps remote probabilities into any MarketForecast structure.
    assert all(item.kind in ("FACT", "ECON_OPINION") for item in items)


def test_opinion_probabilities_never_touch_forecasts() -> None:
    # Remote numeric probabilities stay in opinion text/raw evidence only.
    from pathlib import Path
    fixture = Path(__file__).resolve().parent / "fixtures" / "v2" / "shared.json"
    data = s.decode_shared_fixture(fixture.read_bytes())
    forecast = data["forecasts"][0]
    opinion = data["macro_items"][1]
    assert opinion.kind == "ECON_OPINION"
    assert "0.6" in opinion.text
    assert forecast.direction_probabilities["UP"] == 0.45
    assert date(2026, 1, 9) == forecast.trading_day
