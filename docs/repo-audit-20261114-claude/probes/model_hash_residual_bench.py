#!/usr/bin/env python3
"""K14 之后还剩什么：每个决策上下文仍然随模型大小增长的开销。

K14 让“本进程里刚拟合出来的预测模型集”的套用和哈希不再随训练样本量增长
（上一轮的探针 empirical_apply_scaling_bench.py 退出码已由 1 变为 0）。
这个探针量三条 K14 没有覆盖的路径，都在 v2 的决策管线上，K45 不经过它们：

1. 从 JSON 读回来的预测模型集。``validation/experiments.load_component_models``
   和 ``validation/forward_paper`` 都是这样得到模型集的：样本是普通列表，
   走的是 K14 保留的回退路径。套用仍随样本量增长；内容哈希每次调用都要先把全部
   样本走一遍来确认内容没变，这一遍比直接重算哈希还慢。
2. 收益模型的内容哈希。``decision/pipeline.model_hashes_for`` 每个上下文
   调用两次（``identity_matches`` 一次，取哈希一次），每次把全部观测重新序列化。
3. 行情状态参考的内容哈希。同一个函数里每个上下文也是两次。

“直接重算”是 K14 之前的做法：把同一份内容序列化后取 SHA-256。探针里按
``ForecastModelSet.content_hash`` 的写法重建了这一步，并核对结果与生产函数相同。

只读、合成数据、只用标准库。在仓库根目录运行：
    python docs/repo-audit-20261114-claude/probes/model_hash_residual_bench.py

退出码 1 = 问题存在，下面任一条成立：
            读回的模型集上，训练样本多 100 倍、一次预测慢 20 倍以上；
            读回的模型集的内容哈希，每次调用比直接重算慢 1.5 倍以上；
            收益模型或行情状态参考的第二次哈希超过第一次的 5%。
退出码 0 = 都不再成立。
退出码 2 = 读回的模型集与原模型集的哈希或预测结果不同（探针本身有问题，耗时不可用）。
"""
from __future__ import annotations

import json
import random
import sys
import time
from decimal import Decimal
from pathlib import Path

for parent in (*Path(__file__).resolve().parents, Path.cwd()):
    if (parent / "src" / "ak_trading").is_dir():
        sys.path.insert(0, str(parent / "src"))
        break

from ak_trading.v2.contracts import Horizon  # noqa: E402
from ak_trading.v2.decision import payoffs  # noqa: E402
from ak_trading.v2.probability import models  # noqa: E402
from ak_trading.v2.probability.regimes import RegimeReference  # noqa: E402
from ak_trading.v2.artifacts import sha256_hex  # noqa: E402
from ak_trading.v2.schemas import canonical_json_bytes, encode_record  # noqa: E402

NS = 1_000_000_000
LEVELS = ("SYN|DAY|TREND", "SYN|DAY", "SYN")
HORIZON = 60


def best_ms(function, repeats: int) -> float:
    best = None
    for _ in range(3):
        started = time.perf_counter()
        for _ in range(repeats):
            function()
        elapsed = (time.perf_counter() - started) * 1000.0 / repeats
        best = elapsed if best is None else min(best, elapsed)
    return best


def once_ms(function) -> float:
    started = time.perf_counter()
    function()
    return (time.perf_counter() - started) * 1000.0


def model_set(built):
    return models.ForecastModelSet(
        "set", "reference", (), NS, 2 * NS, 1000 * NS,
        models.default_fit_config((), NS, 2 * NS, 1000 * NS), built, (), {})


def plain_hash(ms) -> str:
    """K14 之前的做法：直接序列化后取哈希（探针里的模型集没有校准证据）。"""
    payload = {
        "regime_reference_id": ms.regime_reference_id,
        "feature_keys": list(ms.feature_keys),
        "training_end_nano": ms.training_end_nano,
        "calibration_end_nano": ms.calibration_end_nano,
        "valid_until_nano": ms.valid_until_nano,
        "fit_config": dict(ms.fit_config),
        "models": {key: dict(value) for key, value in sorted(ms.models.items())},
        "source_hashes": dict(sorted(ms.source_hashes.items())),
        "evidence": [],
    }
    return sha256_hex(canonical_json_bytes(payload))


def main() -> int:
    rng = random.Random(20261114)
    mismatch = False

    print("1. 预测模型集：本进程拟合的，对比从 JSON 读回的（4 个模型，期限 60）")
    print(f"{'每个模型的':>10} {'一次时间类预测 ms':^24} {'内容哈希，读回的模型集 ms':^36}")
    print(f"{'训练样本':>10} {'拟合后直接用':>12} {'读回后':>10} {'每次调用':>12} {'直接重算':>10} {'倍数':>8}")
    reloaded_ms = {}
    hash_ratio = 0.0
    for size in (1_000, 10_000, 100_000):
        built = {}
        for k in range(4):
            steps = [rng.choice((-1, 0, *range(1, HORIZON + 1))) for _ in range(size)]
            built[f"SYN|MINUTE|{k}"] = models._fit_empirical_cdf(steps, [LEVELS] * size, [], [], [])
        fitted = model_set(built)
        # 与 load_component_models 相同：模型字典来自 JSON，样本是普通列表
        parsed = {key: json.loads(canonical_json_bytes(dict(value)))
                  for key, value in built.items()}
        reloaded = model_set(parsed)
        reloaded.content_hash()                               # 第一次：建立缓存
        cached = best_ms(reloaded.content_hash, 1)            # 之后每次调用
        plain = best_ms(lambda: plain_hash(reloaded), 1)
        if not (reloaded.content_hash() == fitted.content_hash() == plain_hash(reloaded)):
            mismatch = True
        owned, listed = built["SYN|MINUTE|0"], parsed["SYN|MINUTE|0"]
        if models._apply_empirical_cdf(owned, LEVELS, HORIZON) != \
                models._apply_empirical_cdf(listed, LEVELS, HORIZON):
            mismatch = True
        fast = best_ms(lambda: models._apply_empirical_cdf(owned, LEVELS, HORIZON), 2_000)
        slow = best_ms(lambda: models._apply_empirical_cdf(listed, LEVELS, HORIZON),
                       max(3, 200_000 // size))
        reloaded_ms[size] = slow
        hash_ratio = cached / plain
        print(f"{size:>10,} {fast:>12.4f} {slow:>10.3f} {cached:>12.1f} {plain:>10.1f} {hash_ratio:>7.1f}x")
    growth = reloaded_ms[100_000] / reloaded_ms[1_000]

    print("\n2. 收益模型的内容哈希（每个上下文两次）")
    print(f"{'观测数':>9} {'第一次 ms':>10} {'第二次 ms':>10}")
    horizon = Horizon("TRADING_MINUTE", 15)
    values = ["SYN", "S", "x", "x", horizon.unit, horizon.value, "x", "x", "x", "x", "x",
              "ATR_1", "G", encode_record(horizon), "x", "100", "a" * 64, "OPEN_INITIAL"]
    key = canonical_json_bytes(values).decode().rstrip("\n")
    payoff_share = 0.0
    for size in (1_000, 10_000):
        observations = tuple(payoffs.PayoffObservation(
            f"obs-{i:07d}", "S", "C", "SYN", key, "a" * 64, "OPEN_INITIAL", horizon, "ATR_1", "G",
            i * 10 + 1, i * 10 + 2, i * 10 + 3, f"2025-01-{1 + i % 28:02d}", "UP_FIRST", True,
            Decimal("80"), Decimal(100), Decimal(0), None, Decimal("100"), "b" * 64)
            for i in range(size))
        model = payoffs.fit_path_payoff_model(observations, 10 ** 12, "policy")
        first = once_ms(model.content_hash)
        second = once_ms(model.content_hash)
        payoff_share = second / first
        print(f"{size:>9,} {first:>10.1f} {second:>10.1f}")

    print("\n3. 行情状态参考的内容哈希（每个上下文两次）")
    print(f"{'波动样本数':>9} {'第一次 ms':>10} {'第二次 ms':>10}")
    regime_share = 0.0
    for size in (1_000, 10_000, 100_000):
        samples = tuple(i / size for i in range(size))
        reference = RegimeReference("regime_reference:x", 10, 5, {"SYN|MINUTE": samples},
                                    {"source": "c" * 64})
        first = once_ms(reference.content_hash)
        second = once_ms(reference.content_hash)
        regime_share = second / first
        print(f"{size:>9,} {first:>10.2f} {second:>10.2f}")

    if mismatch:
        print("\n读回的模型集与原模型集不一致：耗时不可用")
        return 2
    print(f"\n读回的模型集：训练样本多 100 倍，一次预测慢 {growth:.0f} 倍；"
          f"内容哈希每次调用是直接重算的 {hash_ratio:.1f} 倍")
    print(f"收益模型：第二次哈希是第一次的 {payoff_share:.0%}；"
          f"行情状态参考：第二次是第一次的 {regime_share:.0%}")
    return 1 if (growth > 20 or hash_ratio > 1.5 or payoff_share > 0.05
                 or regime_share > 0.05) else 0


if __name__ == "__main__":
    raise SystemExit(main())
