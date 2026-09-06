"""不安裝 MaiBot SDK 時也能執行的價格抓取與峰谷回歸測試。"""

from __future__ import annotations

import importlib.util
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

fake_sdk = types.ModuleType("maibot_sdk")


class _Base:
    pass


class _Plugin:
    def __init__(self) -> None:
        pass


def _field(*, default=None, default_factory=None, **kwargs):
    del kwargs
    return default_factory() if default_factory is not None else default


def _command(*args, **kwargs):
    del args, kwargs

    def decorator(func):
        return func

    return decorator


fake_sdk.Field = _field
fake_sdk.PluginConfigBase = _Base
fake_sdk.MaiBotPlugin = _Plugin
fake_sdk.Command = _command
sys.modules.setdefault("maibot_sdk", fake_sdk)

plugin_path = Path(__file__).resolve().parents[1] / "plugin.py"
spec = importlib.util.spec_from_file_location("deepseek_peak_valley_plugin", plugin_path)
assert spec and spec.loader
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)

TZ = timezone(timedelta(hours=8))
FIXTURE = """
# 模型 & 价格
| 模型 | | | deepseek-v4-flash | deepseek-v4-pro | deepseek-v4-flash-vision-exp |
| --- | --- | --- | --- | --- | --- |
| 价格(1)(2) | 百万tokens输入（缓存命中） | 空闲时段 | 0.05元 | 0.15元 | 0.05元 |
| | | 高峰时段 | 0.10元 | 0.30元 | 0.10元 |
| | 百万tokens输入（缓存未命中） | 空闲时段 | 1.5元 | 4.5元 | 1.5元 |
| | | 高峰时段 | 3.0元 | 9.0元 | 3.0元 |
| | 百万tokens输出 | 空闲时段 | 4.5元 | 13.5元 | 4.5元 |
| | | 高峰时段 | 9.0元 | 27.0元 | 9.0元 |

(1) 空闲时段价格为高峰时段价格的一半。高峰时段为北京时间周一至周五 9:00 - 12:00、14:00 - 18:00（其余为空闲时段）。
"""


def dt(day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 8, day, hour, minute, second, tzinfo=TZ)


def main() -> None:
    snapshot = plugin.parse_pricing_markdown(FIXTURE, dt(23, 0))
    modern_fixture = FIXTURE.replace(
        "高峰时段为北京时间周一至周五 9:00 - 12:00、14:00 - 18:00（其余为空闲时段）。",
        "高峰时段为北京时间 9:00 - 12:00、14:00 - 18:00（其余为空闲时段）。"
        "我们将于北京时间2026年8月23日（周日）00:00起，对峰谷计费规则做出调整，"
        "周末（周六、周日）全天不再区分峰谷时段，统一按照低谷时段价格收取调用费用。",
    )
    modern_snapshot = plugin.parse_pricing_markdown(modern_fixture, dt(23, 0))
    assert modern_snapshot.peak_weekdays == (0, 1, 2, 3, 4)
    assert modern_snapshot.peak_windows == ((540, 720), (840, 1080))

    snapshot = plugin.PriceSnapshot(
        effective_date=snapshot.effective_date,
        fetched_at=snapshot.fetched_at,
        source_url=snapshot.source_url,
        source_method=snapshot.source_method,
        source_digest=snapshot.source_digest,
        peak_weekdays=snapshot.peak_weekdays,
        peak_windows=snapshot.peak_windows,
        prices=snapshot.prices,
        critiques={"peak": "峰測試", "off_peak": "谷測試"},
    )

    assert snapshot.peak_weekdays == (0, 1, 2, 3, 4)
    assert snapshot.peak_windows == ((540, 720), (840, 1080))
    assert snapshot.prices["flash"]["peak"] == {"cache": "0.1", "input": "3", "output": "9"}
    assert snapshot.prices["pro"]["off_peak"] == {"cache": "0.15", "input": "4.5", "output": "13.5"}

    # 2026-08-17 是週一，2026-08-21 是週五，22/23 是週末。
    cases = [
        (dt(17, 8, 59), False, "1分鐘後"),
        (dt(17, 9), True, "3小時後"),
        (dt(17, 12), False, "2小時後"),
        (dt(17, 14), True, "4小時後"),
        (dt(21, 18), False, "2天15小時後"),
        (dt(22, 10), False, "1天23小時後"),
        (dt(23, 14, 47), False, "18小時13分鐘後"),
    ]
    for now, expected_peak, duration in cases:
        assert plugin.is_peak(now, snapshot) is expected_peak
        assert duration in plugin.build_report(now, snapshot)

    monday_transition, monday_peak = plugin.next_transition(dt(23, 14, 47), snapshot)
    assert monday_transition == dt(24, 9)
    assert monday_peak is True
    assert plugin.previous_transition(dt(23, 14, 47), snapshot) == dt(21, 18)

    critiques = plugin.parse_replyer_critiques(
        "先分析一下。\n```json\n"
        '{"peak":"梁文峰站上山頂，風景很好，帳單也很有存在感。",'
        '"off_peak":"梁文谷今天很安靜，錢包終於可以喘口氣。"}\n```'
    )
    assert critiques["peak"].startswith("梁文峰")
    assert critiques["off_peak"].startswith("梁文谷")
    try:
        plugin.parse_replyer_critiques('{"peak":"輸出二十七元", "off_peak":"梁文谷休息"}')
    except ValueError:
        pass
    else:
        raise AssertionError("帶價格事實的銳評應被拒絕")

    report = plugin.build_report(dt(23, 14, 47), snapshot)
    assert "時間：2026/08/23/14/47 週日" in report
    assert "當前時段：「梁文谷」" in report
    assert "V4 Flash價格：輸入1.5／輸出4.5／緩存0.05" in report
    assert "V4 Pro價格：輸入4.5／輸出13.5／緩存0.15" in report
    assert "下次「梁文峰」在18小時13分鐘後" in report
    assert "銳評：谷測試" in report
    print("all pricing fetch, weekday and transition tests passed")


if __name__ == "__main__":
    main()
