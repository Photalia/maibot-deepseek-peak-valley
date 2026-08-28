"""不安装 MaiBot SDK 时也能运行的纯时间／价格／解析回归测试。"""

from __future__ import annotations

import importlib.util
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path


# plugin.py 的价格函数本身不依赖 SDK；这里只提供导入阶段所需的最小桩。
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
# dataclass 会依赖 sys.modules 中注册的模块名，须先注册再执行。
sys.modules["deepseek_peak_valley_plugin"] = plugin
spec.loader.exec_module(plugin)

TZ = plugin.BEIJING_TZ


def dt(day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    """构造 2026-08 中某一天的北京时间时刻。

    2026-08-18 为周二，便于对 weekday 做推导。
    """
    base = datetime(2026, 8, 18, 0, 0, 0, tzinfo=TZ)  # 周二
    return base + timedelta(days=day - 18, hours=hour, minutes=minute, seconds=second)


# 2026-08 日历：18周二 19周三 20周四 21周五 22周六 23周日 24周一
#           29周六 30周日 31周一
# 周末低谷规则生效日 = 2026-08-23（周日）00:00。
# 因此 08-22（周六）在生效日之前，按旧规则仍可能高峰；08-29/08-30 才适用周末低谷。
WEEKDAY_CASES = [
    # (日期, 时刻, 是否高峰)
    (19, 9, True),    # 周三 09:00 高峰
    (19, 12, False),  # 周三 12:00 低谷
    (19, 14, True),   # 周三 14:00 高峰
    (19, 18, False),  # 周三 18:00 低谷
    (21, 10, True),   # 周五 10:00 高峰
    (21, 21, False),  # 周五 21:00 低谷
]
# 生效日（08-23 周日）之后的周末全天低谷。
WEEKEND_CASES = [
    (23, 10, False),  # 周日 10:00 低谷（生效日当天）
    (23, 14, False),  # 周日 14:00 低谷
    (29, 9, False),   # 下周六 09:00 低谷
    (29, 15, False),  # 下周六 15:00 低谷
    (30, 10, False),  # 下周日 10:00 低谷
]
# 生效日（08-22 周六）之前的周末按旧规则，仍可出现高峰。
PRE_START_WEEKEND_CASES = [
    (22, 9, True),
    (22, 15, True),
]


def main() -> None:
    # --- 工作日峰谷基本判断 ---
    for day, hour, expected_peak in WEEKDAY_CASES:
        assert plugin.is_peak(dt(day, hour)) is expected_peak, f"{day}日{hour}时 应 peak={expected_peak}"

    # --- 周末全天低谷 ---
    for day, hour, expected_peak in WEEKEND_CASES:
        assert plugin.is_peak(dt(day, hour)) is expected_peak, f"{day}日{hour}时 应 peak={expected_peak}"

    # --- 生效日（8-22 周六）前的周末按旧规则，仍可出现高峰 ---
    for day, hour, expected_peak in PRE_START_WEEKEND_CASES:
        assert plugin.is_peak(dt(day, hour)) is expected_peak, f"{day}日{hour}时 应 peak={expected_peak}"

    # --- 切换点（工作日） ---
    assert plugin.next_transition(dt(19, 11))[0] == dt(19, 12)
    assert plugin.next_transition(dt(19, 12))[0] == dt(19, 14)
    assert plugin.next_transition(dt(19, 18))[0] == dt(20, 9)  # 次日 09:00 进入高峰
    assert plugin.next_transition(dt(19, 8))[0] == dt(19, 9)
    assert plugin.previous_transition(dt(19, 12)) == dt(19, 12)
    assert plugin.previous_transition(dt(19, 13)) == dt(19, 12)
    assert plugin.previous_transition(dt(19, 8)) == dt(18, 18)

    # --- 周末切换点（生效日 8-23 后）：8-28 周五 / 8-29 周六 / 8-30 周日 / 8-31 周一 ---
    # 周五 21:00 之后，下一高峰是下周一 09:00（跳过周末）。
    assert plugin.next_transition(dt(28, 21))[0] == dt(31, 9)  # 下周一 09:00
    # 周六 10:00 之后仍在下周一 09:00 进入高峰。
    assert plugin.next_transition(dt(29, 10))[0] == dt(31, 9)
    # 周六凌晨：上一进入周末的播报点是周六 00:00。
    assert plugin.previous_transition(dt(29, 10)) == dt(29, 0)
    # 周日 12:00：上一播报点仍是周六 00:00（周末不重复提示）。
    assert plugin.previous_transition(dt(30, 12)) == dt(29, 0)
    # 周五 23:00：上一播报点是周五 18:00（进入低谷）。
    assert plugin.previous_transition(dt(28, 23)) == dt(28, 18)

    # --- 播报文本：工作日高峰 / 周末低谷 ---
    report_peak = plugin.build_report(dt(19, 9))
    assert "处于「梁文峰」时段" in report_peak
    assert "五梁液" in report_peak
    assert "周末" not in report_peak

    report_weekend = plugin.build_report(dt(29, 10))
    assert "处于「梁文谷」时段" in report_weekend
    assert "梁白开" in report_weekend
    assert "周末，全天按低谷价计费" in report_weekend

    # --- 动态高峰时段窗口解析 ---
    sample_html_peak = (
        "<div>(1) 空闲时段价格为高峰时段价格的一半。高峰时段为北京时间 "
        "9:00 - 12:00、14:00 - 18:00（其余为空闲时段）。"
        "我们将于北京时间2026年8月23日（周日）00:00起，对峰谷计费规则做出调整。</div>"
        "<div>图片宽 1024 高 768 之类不应被误抓</div>"
    )
    windows = plugin.parse_peak_windows(sample_html_peak)
    assert (540, 720) in windows  # 9:00-12:00
    assert (840, 1080) in windows  # 14:00-18:00
    # 不应误抓页面其它数字-数字（如 1024 之类），且窗口起点不含凌晨杂散值。
    assert all(start >= 0 for start, _end in windows)

    # 基于动态窗口的 is_peak：工作日 9:30 高峰、12:30 低谷。
    dyn_windows = ((9 * 60, 12 * 60), (14 * 60, 18 * 60))
    assert plugin.is_peak(dt(19, 9, 30), dyn_windows) is True
    assert plugin.is_peak(dt(19, 12, 30), dyn_windows) is False
    assert plugin.is_peak(dt(19, 15), dyn_windows) is True
    # 自定义动态窗口：改为 10:00-13:00 单一窗口。
    custom_windows = ((10 * 60, 13 * 60),)
    assert plugin.is_peak(dt(19, 10, 30), custom_windows) is True
    assert plugin.is_peak(dt(19, 13, 30), custom_windows) is False

    # 无法解析窗口时回退默认。
    assert plugin.parse_peak_windows("<div>无高峰时段说明</div>") == plugin.DEFAULT_PEAK_WINDOWS

    # --- 动态价格快照构建的播报 ---
    snap = plugin.PricingSnapshot(
        versions={"v4_flash": "DeepSeek-V4-Flash-0731", "v4_pro": "DeepSeek-V4-Pro-0813"},
        price={
            "peak": {"flash": (0.20, 6.0, 18.0), "pro": (0.60, 18.0, 54.0)},
            "off_peak": {"flash": (0.10, 3.0, 9.0), "pro": (0.30, 9.0, 27.0)},
        },
    )
    report_dyn = plugin.build_report(dt(19, 9), snap)
    # 格式恢复 v1.0.0 原样：输入X输出Y缓存Z，整数不带 .0
    assert "输入6输出18缓存0.2" in report_dyn

    # --- 官方定价 HTML 解析（结构与真实官方表格一致：计费项×时段两行一组）---
    sample_html = (
        '<table><tr><th>模型版本</th><th>DeepSeek-V4-Flash-0731</th>'
        '<th>DeepSeek-V4-Pro-0813</th><th>DeepSeek-V4-Flash-Vision-Exp</th></tr>'
        '<tr><td>价格(1)(2)</td><td>百万tokens输入（缓存命中）</td><td>空闲时段</td>'
        '<td>0.05元</td><td>0.15元</td><td>0.05元</td></tr>'
        '<tr><td>高峰时段</td><td>0.10元</td><td>0.30元</td><td>0.10元</td></tr>'
        '<tr><td>百万tokens输入（缓存未命中）</td><td>空闲时段</td>'
        '<td>1.5元</td><td>4.5元</td><td>1.5元</td></tr>'
        '<tr><td>高峰时段</td><td>3.0元</td><td>9.0元</td><td>3.0元</td></tr>'
        '<tr><td>百万tokens输出</td><td>空闲时段</td>'
        '<td>4.5元</td><td>13.5元</td><td>4.5元</td></tr>'
        '<tr><td>高峰时段</td><td>9.0元</td><td>27.0元</td><td>9.0元</td></tr></table>'
    )
    parsed = plugin.parse_official_pricing(sample_html)
    assert parsed is not None
    assert parsed.versions["v4_flash"] == "DeepSeek-V4-Flash-0731"
    assert parsed.versions["v4_pro"] == "DeepSeek-V4-Pro-0813"
    assert parsed.price["off_peak"]["flash"][0] == 0.05
    assert parsed.price["peak"]["flash"][2] == 9.0
    assert parsed.price["peak"]["pro"][2] == 27.0
    assert parsed.price["off_peak"]["pro"][1] == 4.5

    # identity 指纹稳定
    assert parsed.identity() == parsed.identity()

    # --- 变更播报文本 ---
    old_snap = plugin.PricingSnapshot(
        versions={"v4_flash": "DeepSeek-V4-Flash-0731"},
        price={"peak": {"flash": (0.10, 3.0, 9.0)}, "off_peak": {"flash": (0.05, 1.5, 4.5)}},
    )
    change = plugin.build_change_report(dt(20, 12), old_snap, snap)
    assert "DeepSeek 官方定价更新" in change
    assert "V4-Pro" in change  # pro 版本由无到有
    assert "高峰 flash" in change  # flash 高峰价格变化
    assert "详情见 https://api-docs.deepseek.com" in change

    # 无变化时
    no_change = plugin.build_change_report(dt(20, 12), snap, snap)
    assert "无明细变化" in no_change

    print("all pricing, transition, weekend, and parsing tests passed")


if __name__ == "__main__":
    main()
