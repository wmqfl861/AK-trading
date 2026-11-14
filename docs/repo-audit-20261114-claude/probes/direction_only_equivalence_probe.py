#!/usr/bin/env python3
"""只拟合方向任务，得到的方向模型与全部任务一起拟合时是否相同。

DECISIONS 的 J34 让 K45 只拟合方向任务，依据是“各任务各自选算法、各自定温度，
互不依赖”。这个探针直接在生产函数上验证这句话：

  甲  调用生产函数 ``fit_forecast_models``，全部任务都拟合；
  乙  同样的输入再调用一次，但给 ``_run_task_selection`` 套一层包装，
      方向以外的任务直接跳过（不登记任何模型）；拟合之后“生成校准证据”那一步也跳过。

为什么要跳过校准证据那一步：它要把每个校准样本拼成一份完整的预测，需要全部任务的
模型。只有方向模型时，生产代码在这里报错
``range_forecast_requires_both_stop_probabilities``。探针先不跳过这一步调用一次，
把这个报错记下来，再跳过它完成比较。方向模型的参数和温度在这一步之前就已经定了。

然后比较两次得到的方向任务的模型：四个候选算法各自的参数、内层验证损失、
选中标记、温度，逐项相等才算相同。

输入借用 tests/test_v2_m3a_models.py 里的合成数据构造函数（180 个训练日、
30 个校准日，日线和分钟两种模式），与该测试文件的 ``fitted`` 夹具相同。
只读，不修改任何产品文件；包装在量完后还原。

这只是验证“互不依赖”这件事，不是 K45 该用的实现方式。怎么实现由计划作者定。

在仓库根目录运行（我这边约 20 秒）：
    python docs/repo-audit-20261114-claude/probes/direction_only_equivalence_probe.py

退出码 0 = 方向模型逐项相同；退出码 1 = 有不同（J34 的依据不成立，先不要只拟合方向）。
"""
from __future__ import annotations

import random
import sys
import time
from pathlib import Path

for parent in (*Path(__file__).resolve().parents, Path.cwd()):
    if (parent / "src" / "ak_trading").is_dir():
        for extra in (parent / "src", parent / "tests"):
            if str(extra) not in sys.path:
                sys.path.insert(0, str(extra))
        break

from ak_trading.v2.probability import models  # noqa: E402
from ak_trading.v2.probability.regimes import fit_regime_reference  # noqa: E402
import test_v2_m3a_models as fixture  # noqa: E402


def build_inputs():
    rng = random.Random(7)
    training = fixture.build_segment(fixture.DAYS[:180], "tr", rng)
    calibration = fixture.build_segment(fixture.DAYS[182:212], "cal", rng)
    training_end = max(label.available_at_nano for label in training[2])
    calibration_end = max(label.available_at_nano for label in calibration[2])
    reference = fit_regime_reference(training[0], training_end, training_end)
    training_regimes = fixture.regimes_for(training[0], "regime_reference:inner-train")
    calibration_regimes = fixture.regimes_for(calibration[0], reference.reference_id)
    config = models.default_fit_config(
        fixture.FEATURE_KEYS, training_end, calibration_end, calibration_end + 30 * fixture.DAYN)
    return (training[0], training_regimes, training[1], training[2],
            calibration[0], calibration_regimes, calibration[1], calibration[2],
            config, reference)


def fit(arguments):
    started = time.perf_counter()
    model_set = models.fit_forecast_models(*arguments, seasonality=None)
    return model_set, time.perf_counter() - started


def direction_models(model_set):
    marker = f"|{models.TASK_DIRECTION}|"
    return {key: value for key, value in model_set.models.items() if marker in key}


def main() -> int:
    arguments = build_inputs()
    full, full_seconds = fit(arguments)

    original = models._run_task_selection
    skipped = [0]

    def only_direction(task, *args, **kwargs):
        if task != models.TASK_DIRECTION:
            skipped[0] += 1
            return None
        return original(task, *args, **kwargs)

    originals = (models._calibration_predictions, models.build_calibration_evidence)
    models._run_task_selection = only_direction
    evidence_error = "没有报错"
    try:
        try:
            fit(arguments)                       # 不跳过校准证据：看生产代码怎么反应
        except Exception as error:               # noqa: BLE001 只记录类型和消息
            evidence_error = f"{type(error).__name__}: {error}"
        skipped[0] = 0
        models._calibration_predictions = lambda *args, **kwargs: []
        models.build_calibration_evidence = lambda *args, **kwargs: []
        partial, partial_seconds = fit(arguments)
    finally:
        models._run_task_selection = original
        models._calibration_predictions, models.build_calibration_evidence = originals

    left, right = direction_models(full), direction_models(partial)
    different = sorted(key for key in set(left) | set(right) if left.get(key) != right.get(key))
    selected = sorted(key for key, value in left.items()
                      if str(value.get("fit_status", "")).startswith("SELECTED"))
    print(f"只跳过其他任务、不跳过校准证据时，生产函数的反应：{evidence_error}")
    print(f"全部任务：{len(full.models)} 个模型，{full_seconds:.1f} 秒")
    print(f"只拟合方向：{len(partial.models)} 个模型（跳过了 {skipped[0]} 次任务拟合和校准证据），"
          f"{partial_seconds:.1f} 秒，是全部任务的 1/{full_seconds / partial_seconds:.0f}")
    print(f"方向任务的模型：{len(left)} 个（其中选中的 {len(selected)} 个），"
          f"逐项比较不同的 {len(different)} 个")
    for key in selected:
        print(f"  选中：{key}，温度 {left[key]['temperature']}")
    for key in different[:10]:
        print("  ✗ 不同：", key)
    if different or not left:
        print("方向模型与全部拟合时不同：J34 的依据不成立")
        return 1
    print("方向模型（参数、内层验证损失、选中标记、温度）与全部任务一起拟合时逐项相同")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
