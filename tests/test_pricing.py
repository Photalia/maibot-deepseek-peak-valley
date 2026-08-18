"""不安装 MaiBot SDK 时也能运行的纯时间／价格回归测试。"""

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
spec.loader.exec_module(plugin)

TZ = timezone(timedelta(hours=8))


def dt(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 8, 18, hour, minute, second, tzinfo=TZ)


def main() -> None:
    cases = [
        (dt(8, 59), False, "1分钟后"),
        (dt(9), True, "3小时后"),
        (dt(11, 30), True, "30分钟后"),
        (dt(12), False, "2小时后"),
        (dt(14), True, "4小时后"),
        (dt(18), False, "15小时后"),
    ]
    for now, expected_peak, duration in cases:
        assert plugin.is_peak(now) is expected_peak
        assert duration in plugin.build_report(now)

    assert plugin.previous_transition(dt(8, 59)) == datetime(2026, 8, 17, 18, tzinfo=TZ)
    assert plugin.previous_transition(dt(9)) == dt(9)
    assert plugin.previous_transition(dt(13)) == dt(12)
    assert plugin.previous_transition(dt(17)) == dt(14)
    assert plugin.previous_transition(dt(23)) == dt(18)

    assert plugin.next_transition(dt(8, 59))[0] == dt(9)
    assert plugin.next_transition(dt(12))[0] == dt(14)
    assert plugin.next_transition(dt(18))[0] == datetime(2026, 8, 19, 9, tzinfo=TZ)

    report = plugin.build_report(dt(12))
    assert "V4 Flash:输入1.5输出4.5缓存0.05" in report
    assert "饮料瓶评估:梁白开" in report
    print("all pricing and transition tests passed")


if __name__ == "__main__":
    main()
