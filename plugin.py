"""DeepSeek V4 峰谷價格播報插件。

此插件根据北京时间判断 DeepSeek API 当前处于高峰（梁文峰）还是空闲（梁文谷）时段，
将价格形象化为「五梁液」（高峰）与「梁白开」（空闲）。

v1.1.0 起，价目表不再写死，而是启动与定时轮询时从官方定价页动态解析并缓存，
当官方模型版本号或价格发生变化时，自动向配置的群聊播报变更。
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase

BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
# 数据目录在插件实例初始化时解析：优先使用 MaiBot 提供的统一持久化目录
# （self.ctx.paths.data_dir），无该能力时回退到源码目录下的 data/。
_LEGACY_DATA_DIR = Path(__file__).resolve().parent / "data"

# 官方定价页地址（服务端预渲染，价格与版本号直接内联在 HTML 中）。
PRICING_URL = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing"
PRICING_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# 周末（周六、周日）全天不再区分峰谷、统一按低谷价计费的生效日期（北京时间）。
WEEKEND_OFF_PEAK_START = datetime(2026, 8, 23, 0, 0, 0, tzinfo=BEIJING_TZ)
# 工作日的峰谷切换钟点（进入高峰为 True）。
_WEEKDAY_TRANSITIONS = ((9, True), (12, False), (14, True), (18, False))

# 抓取/解析失败后回退的默认价目表（与 v1.0.0 写死值一致，保证极端情况仍可播报）。
# 单位：每百万 tokens，人民币。
DEFAULT_OFF_PEAK = {"flash": (0.05, 1.5, 4.5), "pro": (0.15, 4.5, 13.5)}
DEFAULT_PEAK = {"flash": (0.10, 3.0, 9.0), "pro": (0.30, 9.0, 27.0)}


class PluginSectionConfig(PluginConfigBase):
    enabled: bool = Field(default=True, description="是否啟用插件")
    config_version: str = Field(default="1.1.0", description="配置版本")


class ScheduleConfig(PluginConfigBase):
    enabled: bool = Field(default=True, description="是否在峰谷切換時主動播報")
    target_group_ids: list[str] = Field(default_factory=list, description="主動播報的 QQ 群號")
    grace_seconds: int = Field(default=120, description="切換後允許補發的秒數")


class FetchConfig(PluginConfigBase):
    # 官方定价页抓取相关配置。
    enabled: bool = Field(default=True, description="是否動態解析官方定價頁")
    poll_interval_seconds: int = Field(default=43200, description="定時輪詢抓取間隔（秒）")
    fetch_timeout_seconds: int = Field(default=10, description="單次抓取超時（秒）")
    # 连续失败达到该次数（或跨越 schedule 轮询）才向群聊发送一次告警，避免刷屏。
    alert_after_failures: int = Field(default=3, description="连续失败多少次後推送告警")


class RateLimitConfig(PluginConfigBase):
    user_cooldown_seconds: int = Field(default=30, description="同一使用者查詢冷卻秒數")
    stream_cooldown_seconds: int = Field(default=5, description="同一聊天查詢冷卻秒數")
    manual_check_cooldown_seconds: int = Field(default=60, description="/checkdsapiupdate 手動檢查官方更新的冷卻秒數")


class PeakValleyConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    fetch: FetchConfig = Field(default_factory=FetchConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)


@dataclass
class PricingSnapshot:
    """一次解析出的官方定价快照。"""

    # 版本号字典：{模型名: 版本串}，如 {"V4-Flash": "DeepSeek-V4-Flash-0731"}
    versions: dict[str, str] = field(default_factory=dict)
    # 价格表结构：
    #   price["peak"]["flash"] = (cache_hit, cache_miss, output)
    #   price["off_peak"]["pro"]  同理
    price: dict[str, dict[str, tuple[float, float, float]]] = field(default_factory=dict)

    def identity(self) -> str:
        """返回本次快照的稳定指纹，用于变更比对。"""
        return json.dumps(
            {"versions": {k: v for k, v in sorted(self.versions.items())}, "price": self.price},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )


@dataclass
class PricingCache:
    """持久化的定价缓存与抓取健康状态。"""

    last_snapshot: Optional[PricingSnapshot] = None
    fetched_at: float = 0.0
    consecutive_failures: int = 0
    last_alert_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        if self.last_snapshot is None:
            return {"fetched_at": self.fetched_at, "consecutive_failures": self.consecutive_failures,
                    "last_alert_at": self.last_alert_at}
        return {
            "versions": self.last_snapshot.versions,
            "price": _serialize_price(self.last_snapshot.price),
            "fetched_at": self.fetched_at,
            "consecutive_failures": self.consecutive_failures,
            "last_alert_at": self.last_alert_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PricingCache":
        obj = cls()
        try:
            versions = {str(k): str(v) for k, v in (payload.get("versions") or {}).items()}
            price_dump = payload.get("price") or {}
            price = _deserialize_price(price_dump)
            if versions or price:
                obj.last_snapshot = PricingSnapshot(versions=versions, price=price)
        except Exception:
            # 缓存损坏时忽略，回退到重新抓取。
            pass
        obj.fetched_at = float(payload.get("fetched_at") or 0.0)
        obj.consecutive_failures = int(payload.get("consecutive_failures") or 0)
        obj.last_alert_at = float(payload.get("last_alert_at") or 0.0)
        return obj


def _serialize_price(price: dict[str, dict[str, tuple[float, float, float]]]) -> dict[str, Any]:
    return {
        zone: {model: [float(x) for x in trio] for model, trio in models.items()}
        for zone, models in price.items()
    }


def _deserialize_price(data: dict[str, Any]) -> dict[str, dict[str, tuple[float, float, float]]]:
    result: dict[str, dict[str, tuple[float, float, float]]] = {}
    for zone, models in (data or {}).items():
        result[str(zone)] = {
            str(model): tuple(float(x) for x in trio) for model, trio in models.items()
        }
    return result


def _strip_cell(value: str) -> str:
    """清理单个表格单元格内的可见文本。

    官方页表格单元格夹杂 <code>、&nbsp; 及空字符，统一去除标签与空白后取可见文本。
    """
    cleaned = re.sub(r"<[^>]+>", "", value)
    cleaned = cleaned.replace("&nbsp;", " ").replace("\x00", "")
    return cleaned.strip()


def _parse_price_cell(value: str) -> Optional[float]:
    """将形如 "3.0元" / "0.05元" 的价格单元格解析为浮点数。

    仅接受以「元」结尾的数字，避免误抓表格中的 `(1)`、`(2)`、`2500` 等非价格内容。
    """
    stripped = value.strip().rstrip("元").strip()
    stripped = re.sub(r"\s+", "", stripped)
    # 整体是一个十进制数字（可含 .），且原串以「元」结尾才算价格。
    if not value.strip().endswith("元"):
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", stripped):
        return float(stripped)
    return None


def parse_official_pricing(html: str) -> Optional[PricingSnapshot]:
    """从官方定价页 HTML 中解析模型版本号与价格表。

    页面由 Docusaurus 服务端预渲染，价格与版本号内联于 HTML。
    解析基于固定的表格行列结构，官方改版后需同步本函数的定位逻辑。

    Args:
        html: 官方定价页的原始 HTML 文本。

    Returns:
        PricingSnapshot: 解析成功时返回快照；页面结构发生变化无法解析时返回 ``None``。
    """
    # 1) 模型版本号
    versions: dict[str, str] = {}
    for model_key, version_token in re.findall(r"DeepSeek-(V4-[A-Za-z-]+?)-([0-9]{4})", html):
        # model_key 可能是 "V4-Flash" / "V4-Pro" / "V4-Flash-Vision-Exp"
        normalized = model_key.lower().replace("-", "_")
        if normalized in {"v4_flash", "v4_pro", "v4_flash_vision_exp"}:
            versions[normalized] = f"DeepSeek-{model_key}-{version_token}"

    # 2) 价格表格
    tables = re.findall(r"<table.*?</table>", html, flags=re.S)
    if not tables:
        return None
    table_rows: list[list[str]] = []
    for row_match in re.findall(r"<tr.*?</tr>", tables[0], flags=re.S):
        cells = [_strip_cell(c) for c in re.findall(r"<(?:td|th)[^>]*>(.*?)</(?:td|th)>", row_match, flags=re.S)]
        if cells:
            table_rows.append(cells)

    # 定位「价格」区段
    price_start = None
    for i, row in enumerate(table_rows):
        # 表头首格通常为「价格」或「价格(1)(2)」等。
        if row and str(row[0]).startswith("价格"):
            price_start = i
            break
    if price_start is None:
        if versions:
            # 版本号可解析但价格结构变化时，仅返回版本的快照（价格回退默认）。
            return PricingSnapshot(versions=versions)
        return None

    price_rows = table_rows[price_start:]
    # 官方表按「计费项 × 时段」组织，通常两行一组：
    #   行A: ['…缓存命中…', '空闲时段', 价flash, 价pro, 价vision]
    #   行B: ['高峰时段', 价flash, 价pro, 价vision]   ← 计费项继承前一行
    #   …缓存未命中和输出的结构与此一致。
    model_order = ("flash", "pro", "vision")
    zone_builder: dict[str, dict[str, dict[str, float]]] = {
        "off_peak": {m: {} for m in model_order},
        "peak": {m: {} for m in model_order},
    }
    # 当前继承的计费项（高峰/空闲单独成行时沿用前一行）。
    current_kind: str | None = None
    assigned_rows = 0
    for row in price_rows:
        joined = "".join(row)
        if "缓存命中" in joined:
            current_kind = "cache_hit"
        elif "缓存未命中" in joined:
            current_kind = "cache_miss"
        elif "输出" in joined and "缓存" not in joined:
            current_kind = "output"
        # 判断时段：空闲时段 / 高峰时段
        if "空闲时段" in joined:
            zone = "off_peak"
        elif "高峰时段" in joined:
            zone = "peak"
        else:
            continue
        if current_kind is None:
            continue
        # 提取该行的三个价格（缓存命中/未命中/输出的价列）。
        # 若行首是时段名（如高峰单独成行），价格从 [1:] 起；否则从 [0] 起取末尾三个数值。
        nums = [_parse_price_cell(c) for c in row]
        vals = [v for v in nums if v is not None]
        if len(vals) < 3:
            continue
        for idx in range(3):
            zone_builder[zone][model_order[idx]][current_kind] = vals[idx]  # type: ignore[index]
        assigned_rows += 1

    # 只有当三种计费项在空闲与高峰各解析出完整行时才可信。
    # 校验：三个 kind × 两个 zone 都应有数据。
    required_kinds = {"cache_hit", "cache_miss", "output"}
    valid = True
    for zone in ("off_peak", "peak"):
        if not all(required_kinds <= set(fields) for fields in zone_builder[zone].values()):
            valid = False
            break
    if not valid:
        if versions:
            return PricingSnapshot(versions=versions)
        return None

    # 组装 price[zone][model] = (cache_hit, cache_miss, output)
    zone_price: dict[str, dict[str, tuple[float, float, float]]] = {}
    for zone in ("off_peak", "peak"):
        models: dict[str, tuple[float, float, float]] = {}
        for model in model_order:
            fields = zone_builder[zone][model]
            if all(k in fields for k in required_kinds):
                models[model] = (fields["cache_hit"], fields["cache_miss"], fields["output"])
        zone_price[zone] = models
    return PricingSnapshot(versions=versions, price=zone_price)


# ---------------------------------------------------------------- 时段函数

def _is_weekend_off_peak(now: datetime) -> bool:
    """判断当前是否已启用「周末全天低谷价」规则。

    自 WEEKEND_OFF_PEAK_START 起，周六（weekday=5）与周日（weekday=6）全天按低谷价。
    """
    if now < WEEKEND_OFF_PEAK_START:
        return False
    return now.weekday() >= 5


def is_peak(now: datetime) -> bool:
    """判斷北京時間是否為官方高峰時段。

    工作日内为 9:00-12:00、14:00-18:00；周末（生效日起）全天为低谷，返回 ``False``。
    """
    if _is_weekend_off_peak(now):
        return False
    minute = now.hour * 60 + now.minute
    return 9 * 60 <= minute < 12 * 60 or 14 * 60 <= minute < 18 * 60


def _iter_transition_after(now: datetime) -> Any:
    """从 now 之后开始，逐日产生未来的价格切换时刻及其进入时段。

    Yields:
        (candidate, enters_peak)：candidate 为切换时刻，enters_peak 表示切换后是否进入高峰。
        周末（生效日起）全天按低谷价，与邻近低谷之间价格无变化，
        因此周末不产生任何切换点；下一个价格切换点会在后续工作日出现。
    """
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    scan_days = 0
    while scan_days < 8:
        if not _is_weekend_off_peak(day):
            for hour, enters_peak in _WEEKDAY_TRANSITIONS:
                candidate = day.replace(hour=hour, minute=0, second=0, microsecond=0)
                if candidate > now:
                    yield candidate, enters_peak
        day += timedelta(days=1)
        scan_days += 1


def next_transition(now: datetime) -> tuple[datetime, bool]:
    """回傳下一個切換時間，以及切換後是否進入高峰。

    基于自然日扫描，自动跳过周末的高峰切换点：
    - 工作日 09:00/14:00 进入高峰，12:00/18:00 进入低谷；
    - 周末（生效日起）全天为低谷，不存在高峰切换点。
    """
    for candidate, enters_peak in _iter_transition_after(now):
        return candidate, enters_peak
    # 理论上扫描 8 天必然有结果；此处仅为防御，返回下下个工作日的 09:00。
    tomorrow = now + timedelta(days=1)
    base = tomorrow
    while _is_weekend_off_peak(base):
        base += timedelta(days=1)
    return base.replace(hour=9, minute=0, second=0, microsecond=0), True


def previous_transition(now: datetime) -> datetime:
    """回传当前时刻之前最近一次需要播报的峰谷切换时间。

    工作日的峰谷/谷峰切换点都会被考虑；周末仅当从非周末首次进入周末（周六 00:00）
    时产生一次「周末低谷」播报点，避免每日 00:00 重复提示。
    """
    # 从当天零点起扫描，向前回退最多 8 天，只收集 <= now 的切换点。
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    found: list[datetime] = []
    for _ in range(8):
        # 工作日（含周五）的正常峰谷切换点。
        if not _is_weekend_off_peak(day):
            for hour, _enters_peak in _WEEKDAY_TRANSITIONS:
                candidate = day.replace(hour=hour, minute=0, second=0, microsecond=0)
                if candidate <= now:
                    found.append(candidate)
        # 周五 → 次日周六 00:00：首次切入周末，播报「周末全天低谷」。
        # 判断依据是「day 是周五」且下一个周末已生效。
        if day.weekday() == 4 and _is_weekend_off_peak(day + timedelta(hours=24)):
            candidate = (day + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            if candidate <= now:
                found.append(candidate)
        day -= timedelta(days=1)
    if not found:
        return now - timedelta(days=1)
    return max(found)


def human_duration(delta: timedelta) -> str:
    minutes = max(1, math.ceil(delta.total_seconds() / 60))
    hours, rest = divmod(minutes, 60)
    if hours and rest:
        return f"{hours}小时{rest}分钟后"
    if hours:
        return f"{hours}小时后"
    return f"{rest}分钟后"


def build_report(now: datetime, snapshot: Optional[PricingSnapshot] = None) -> str:
    """构造峰谷价格播报文本（原 v1.0.0 展示格式，缓存价优先）。

    - 价格展示格式与 v1.0.0 一致：`输入X输出Y缓存Z`
      （X=缓存未命中输入价、Y=输出价、Z=缓存命中输入价）。
    - 价格数值优先取缓存中的动态官方价（snapshot），缺失时回退内置静态默认价。
    - 时段判断沿用 `is_peak`（含周末全天低谷规则）。

    Args:
        now: 北京时间的当前时刻。
        snapshot: 可选的官方价格缓存快照。

    Returns:
        str: 格式化的峰谷播报文本。
    """
    peak = is_peak(now)
    following, enters_peak = next_transition(now)

    def _fmt(model: str, zone: str) -> str:
        cache_miss, output, cache_hit = _price_components(snapshot, model, zone)
        return f"输入{_fmt_num(cache_miss)}输出{_fmt_num(output)}缓存{_fmt_num(cache_hit)}"

    zone_prices = "peak" if peak else "off_peak"
    prices = (
        f"V4 Flash:{_fmt('flash', zone_prices)}\n"
        f"V4 Pro:{_fmt('pro', zone_prices)}"
    )
    if peak:
        bottle = "饮料瓶指标:五梁液"
    else:
        bottle = "饮料瓶评估:梁白开"
    current_name = "梁文峰" if peak else "梁文谷"
    next_name = "梁文峰" if enters_peak else "梁文谷"
    weekend_note = ""
    if _is_weekend_off_peak(now):
        weekend_note = "\n今日为周末，全天按低谷价计费"
    return (
        f"当前时间是 {now:%H:%M}（{_weekday_cn(now)}）\n"
        f"处于「{current_name}」时段\n"
        f"{prices}\n"
        f"下一次「{next_name}」时间在{human_duration(following - now)}"
        f"{weekend_note}\n"
        f"{bottle}"
    )


def _price_components(
    snapshot: Optional[PricingSnapshot], model: str, zone: str
) -> tuple[float, float, float]:
    """返回某模型在指定时段的 (缓存未命中输入, 输出, 缓存命中输入)。

    优先取 snapshot 中的动态官方价，缺失时回退内置静态默认价。
    """
    trio = None
    if snapshot is not None:
        trio = snapshot.price.get(zone, {}).get(model)
    if not trio:
        fallback = DEFAULT_PEAK if zone == "peak" else DEFAULT_OFF_PEAK
        trio = fallback.get(model)
    cache_hit, cache_miss, output = trio
    return cache_miss, output, cache_hit


def _fmt_num(value: float) -> str:
    """将价格数值格式化为紧凑字符串：整数显示不带小数点，非整数去掉多余尾零。"""
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


def _weekday_cn(now: datetime) -> str:
    """返回星期几的中文（周一至周日）。"""
    return "周" + "一二三四五六日"[now.weekday()]


def build_change_report(now: datetime, old: PricingSnapshot, new: PricingSnapshot) -> str:
    """构造官方定价/版本变更播报。

    Args:
        now: 北京时间的当前时刻。
        old: 上一次解析到的快照。
        new: 本次解析到的快照。

    Returns:
        str: 面向群聊的变更说明文本。
    """
    lines = [
        f"【DeepSeek 官方定价更新】{now:%Y-%m-%d %H:%M}",
        "",
    ]
    # 版本号变化
    all_models = sorted(set(list(old.versions) + list(new.versions)))
    version_changed = False
    for model in all_models:
        old_v = old.versions.get(model)
        new_v = new.versions.get(model)
        if old_v != new_v:
            version_changed = True
            display = _display_model(model)
            lines.append(f"· {display}: {old_v or '?'} → {new_v or '?'}")
    price_changed = False
    # 价格变化：逐模型、逐时段展示
    for zone in ("peak", "off_peak"):
        label = "高峰" if zone == "peak" else "空闲"
        for model in sorted(set(list(old.price.get(zone, {})) + list(new.price.get(zone, {})))):
            old_trio = old.price.get(zone, {}).get(model)
            new_trio = new.price.get(zone, {}).get(model)
            if old_trio != new_trio:
                price_changed = True
                display = _display_model(model)
                lines.append(f"· {label} {display}: {_trio_str(old_trio)} → {_trio_str(new_trio)}")
    if version_changed:
        lines.append("")
    if not version_changed and not price_changed:
        lines.append("· （无明细变化，仅页面刷新）")
    lines.append("")
    lines.append("详情见 https://api-docs.deepseek.com/zh-cn/quick_start/pricing")
    return "\n".join(lines)


def _trio_str(trio: Optional[tuple[float, float, float]]) -> str:
    if not trio:
        return "?"
    return f"输入缓存{trio[0]}/未命中{trio[1]}/输出{trio[2]}"


def _display_model(model: str) -> str:
    """将内部模型键（如 v4_flash / v4_pro / v4_flash_vision_exp）转为展示名。"""
    if model == "v4_flash":
        return "V4-Flash"
    if model == "v4_pro":
        return "V4-Pro"
    if model == "v4_flash_vision_exp":
        return "V4-Flash-Vision-Exp"
    return model.replace("_", "-")


# ---------------------------------------------------------------- 抓取与缓存


def fetch_pricing_page_plain(http_url: str = PRICING_URL, timeout: float = 10.0, ua: str = PRICING_UA) -> str:
    """抓取官方定价页 HTML；超时或非 200 时抛异常。"""
    req = Request(http_url, headers={"User-Agent": ua, "Accept": "text/html,application/xhtml+xml"})
    with urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    return body.decode("utf-8", errors="ignore")


# ---------------------------------------------------------------- 插件主体

class DeepSeekPeakValleyPlugin(MaiBotPlugin):
    config_model = PeakValleyConfig

    def __init__(self) -> None:
        super().__init__()
        self._schedule_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._last_user_query: dict[str, float] = {}
        self._last_stream_query: dict[str, float] = {}
        self._last_check_query: dict[str, float] = {}  # /checkdsapiupdate 独立冷却
        self._sent_transition_keys: set[str] = set()
        self._pricing_cache = PricingCache()
        self._last_priced_identity: str | None = None
        self._data_dir: Path | None = None

    def _data_file(self, name: str) -> Path:
        """返回数据文件路径，目录优先使用 MaiBot 统一持久化目录。

        首次调用时解析 self.ctx.paths.data_dir；若无该能力则回退到源码目录 data/。
        """
        if self._data_dir is None:
            data_dir: Path | None = None
            paths = getattr(getattr(self, "ctx", None), "paths", None)
            if paths is not None and getattr(paths, "data_dir", None):
                data_dir = Path(paths.data_dir)
            if data_dir is None:
                data_dir = _LEGACY_DATA_DIR
            self._data_dir = data_dir
        return self._data_dir / name

    async def on_load(self) -> None:
        self._load_state()
        self._load_pricing_cache()
        self._stop_event.clear()
        # 加载时若已有缓存，先做一次快速抓取刷新（避免重启后长期使用陈旧价表）。
        if self.config.fetch.enabled:
            try:
                await self._refresh_pricing(notify_on_change=False)
            except Exception:
                self.ctx.logger.exception("加载时抓取官方定价失败，将使用缓存/默认价")
        if self.config.schedule.enabled:
            self._schedule_task = asyncio.create_task(self._schedule_loop(), name="deepseek-peak-valley")
        self.ctx.logger.info(
            "DeepSeek 峰谷插件已載入，主動播報群=%s",
            list(self.config.schedule.target_group_ids),
        )

    async def on_unload(self) -> None:
        self._stop_event.set()
        if self._schedule_task is not None:
            self._schedule_task.cancel()
            try:
                await self._schedule_task
            except asyncio.CancelledError:
                pass
            self._schedule_task = None

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        del scope, config_data, version
        await self.on_unload()
        self._stop_event = asyncio.Event()
        if self.config.fetch.enabled:
            try:
                await self._refresh_pricing(notify_on_change=False)
            except Exception:
                self.ctx.logger.exception("配置更新后刷新定价失败")
        if self.config.schedule.enabled:
            self._schedule_task = asyncio.create_task(self._schedule_loop(), name="deepseek-peak-valley")

    @Command("deepseek_peak_valley", description="查詢 DeepSeek V4 當前峰谷時段與價格", pattern=r"^/(?:峰谷|fenggu)\s*$")
    async def handle_peak_valley(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ):
        del kwargs
        if not self.config.plugin.enabled:
            return False, "插件未啟用", True

        monotonic_now = time.monotonic()
        user_key = f"{group_id or stream_id}:{user_id or 'unknown'}"
        user_cd = max(1, int(self.config.rate_limit.user_cooldown_seconds))
        stream_cd = max(1, int(self.config.rate_limit.stream_cooldown_seconds))

        last_user = self._last_user_query.get(user_key, 0.0)
        last_stream = self._last_stream_query.get(stream_id, 0.0)
        if monotonic_now - last_user < user_cd or monotonic_now - last_stream < stream_cd:
            return True, "峰谷查詢處於冷卻中", True

        self._last_user_query[user_key] = monotonic_now
        self._last_stream_query[stream_id] = monotonic_now
        report = build_report(datetime.now(BEIJING_TZ), self._pricing_cache.last_snapshot)
        await self.ctx.send.text(report, stream_id)
        self._prune_rate_limits(monotonic_now, max(user_cd, stream_cd) * 4)
        return True, "已顯示 DeepSeek 峰谷價格", True

    @Command("deepseek_peak_valley_query", description="查詢 DeepSeek 當前峰谷價格", pattern=r"^/查询ds价格\s*$")
    async def handle_query_price(self, stream_id: str = "", user_id: str = "", group_id: str = "", **kwargs: Any):
        """手动查询价格的第二个入口（与 /峰谷 等价，使用缓存价格，不额外抓取）。"""
        del kwargs
        if not self.config.plugin.enabled:
            return False, "插件未啟用", True
        monotonic_now = time.monotonic()
        user_key = f"{group_id or stream_id}:{user_id or 'unknown'}"
        user_cd = max(1, int(self.config.rate_limit.user_cooldown_seconds))
        stream_cd = max(1, int(self.config.rate_limit.stream_cooldown_seconds))
        if monotonic_now - self._last_user_query.get(user_key, 0.0) < user_cd or \
           monotonic_now - self._last_stream_query.get(stream_id, 0.0) < stream_cd:
            return True, "查询处于冷却中", True
        self._last_user_query[user_key] = monotonic_now
        self._last_stream_query[stream_id] = monotonic_now
        report = build_report(datetime.now(BEIJING_TZ), self._pricing_cache.last_snapshot)
        await self.ctx.send.text(report, stream_id)
        return True, "已查詢 DeepSeek 峰谷價格", True

    @Command("deepseek_peak_valley_check", description="手动检查 DeepSeek 官方是否有定价/版本更新", pattern=r"^/checkdsapiupdate\s*$")
    async def handle_check_update(self, stream_id: str = "", user_id: str = "", group_id: str = "", **kwargs: Any):
        """手动触发一次官方定价页抓取。

        - 抓取/解析失败：直接回复失败原因，不调用麦麦（明确反馈，绝不静默）。
        - 抓取成功：把当前官方价格/版本 + 与上次缓存的差异线索喂给麦麦，
          由麦麦判断是否有值得播报的更新并回应（麦麦总会回复一条结果）。
        """
        del kwargs
        if not self.config.plugin.enabled:
            return False, "插件未啟用", True

        monotonic_now = time.monotonic()
        user_key = f"{group_id or stream_id}:{user_id or 'unknown'}"
        check_cd = max(1, int(self.config.rate_limit.manual_check_cooldown_seconds))
        last_check = self._last_check_query.get(user_key, 0.0)
        if monotonic_now - last_check < check_cd:
            return True, "检查太频繁，请稍后再试", True
        self._last_check_query[user_key] = monotonic_now
        self._prune_rate_limits(monotonic_now, max(check_cd, 60) * 4)

        # 实时抓取并解析官方页。
        try:
            html = await asyncio.to_thread(
                fetch_pricing_page_plain,
                PRICING_URL,
                float(self.config.fetch.fetch_timeout_seconds),
            )
            new_snapshot = await asyncio.to_thread(parse_official_pricing, html)
        except Exception as exc:
            self.ctx.logger.warning("手动检查官方更新抓取失败: %s", exc)
            await self.ctx.send.text(f"【DeepSeek 官方更新检查失败】\n无法获取官方定价页：{exc}", stream_id)
            return True, "已回覆檢查失敗", True

        if new_snapshot is None or not new_snapshot.versions:
            await self.ctx.send.text(
                "【DeepSeek 官方更新检查失败】\n已获取页面但无法解析（官方结构可能已改版）。",
                stream_id,
            )
            return True, "已回覆檢查失敗", True

        # 刷新缓存（写入统一数据目录），并构造给麦麦的完整上下文，让其自行判断是否播报。
        previous = self._pricing_cache.last_snapshot
        self._pricing_cache.last_snapshot = new_snapshot
        self._pricing_cache.fetched_at = time.time()
        if self._pricing_cache.consecutive_failures:
            self._pricing_cache.consecutive_failures = 0
        self._save_pricing_cache()

        # 构造差异/当前内容文本：若有上一次缓存则给出对比线索，否则给出动态当前价。
        diff_text = build_change_report(datetime.now(BEIJING_TZ), previous, new_snapshot) if previous is not None \
            else build_report(datetime.now(BEIJING_TZ), new_snapshot)
        instruction = (
            "用户手动请求检查 DeepSeek 官方定价页。请根据刚才追加的上下文，"
            "判断当前官方价格/模型版本相比之前是否有值得播报的更新；"
            "若有则简短说明变化，若无则礼貌回复『没有明显更新』。无论哪种情况，都请面向群成员简洁回复。"
        )
        delivered = await self._notify_maisaka_summary(stream_id, diff_text, instruction)
        if not delivered:
            # 麦麦总结不可用时，回退为直接输出当前状态，保证总是有明确反馈。
            await self.ctx.send.text(diff_text, stream_id)
        return True, "已触發官方更新檢查", True

    async def _schedule_loop(self) -> None:
        """等待切换点，周期性刷新定价，并在价格变更/失败告警窗口内执行动作。"""
        while not self._stop_event.is_set():
            now = datetime.now(BEIJING_TZ)
            transition = previous_transition(now)
            transition_key = transition.strftime("%Y-%m-%dT%H:%M")
            grace = max(0, int(self.config.schedule.grace_seconds))
            elapsed = (now - transition).total_seconds()

            if 0 <= elapsed <= grace and not self._transition_complete(transition_key):
                await self._broadcast(build_report(now, self._pricing_cache.last_snapshot), transition_key)

            # 周期性定价抓取：仅当缓存过期且当前处于时间阈内（避免重复抓取）。
            if self.config.fetch.enabled:
                await self._maybe_fetch_on_schedule()

            now = datetime.now(BEIJING_TZ)
            transition = previous_transition(now)
            transition_key = transition.strftime("%Y-%m-%dT%H:%M")
            elapsed = (now - transition).total_seconds()
            retry_pending = 0 <= elapsed <= grace and not self._transition_complete(transition_key)
            if retry_pending:
                delay = 10.0
            else:
                upcoming, _ = next_transition(now)
                delay = max(0.2, (upcoming - now).total_seconds())
                # goloop 内把下次休眠缩短为轮询间隔，保证定价会随 poll_interval 到来。
                poll_interval = max(60, int(self.config.fetch.poll_interval_seconds))
                if self.config.fetch.enabled and delay > poll_interval:
                    delay = poll_interval

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                return
            except asyncio.TimeoutError:
                pass

    async def _maybe_fetch_on_schedule(self) -> None:
        """按固定轮询间隔决定是否重新抓取并刷新缓存。"""
        poll_interval = max(60, int(self.config.fetch.poll_interval_seconds))
        # fetched_at<=0 表示从未成功抓取过（如启动时网络失败），也应在轮询周期内补抓。
        if self._pricing_cache.fetched_at <= 0 or time.time() - self._pricing_cache.fetched_at >= poll_interval:
            try:
                await self._refresh_pricing(notify_on_change=True)
            except Exception:
                self.ctx.logger.exception("定时刷新定价失敗")
                self._pricing_cache.consecutive_failures += 1
                self._save_pricing_cache()
                await self._maybe_alert_failures()

    async def _refresh_pricing(self, notify_on_change: bool) -> None:
        """抓取官方定价页、解析并刷新缓存；出现版本/价格变化时可选发送变更播报。

        Args:
            notify_on_change: 解析结果与上次不同时是否推送变更播报。
        """
        html = await asyncio.to_thread(
            fetch_pricing_page_plain,
            PRICING_URL,
            float(self.config.fetch.fetch_timeout_seconds),
        )
        snapshot = await asyncio.to_thread(parse_official_pricing, html)
        if snapshot is None or not snapshot.versions:
            raise ValueError("官方定价页无法解析（结构可能已变更）")

        previous = self._pricing_cache.last_snapshot
        changed = False
        if notify_on_change:
            old_identity = self._last_priced_identity
            new_identity = snapshot.identity()
            changed = old_identity is not None and old_identity != new_identity
            self._last_priced_identity = new_identity

        self._pricing_cache.last_snapshot = snapshot
        self._pricing_cache.fetched_at = time.time()
        # 抓取成功即清零失败计数
        if self._pricing_cache.consecutive_failures:
            self._pricing_cache.consecutive_failures = 0
            self._save_pricing_cache()
        self._save_pricing_cache()

        self.ctx.logger.info(
            "已刷新 DeepSeek 官方定价: 版本=%s, 变更=%s", snapshot.versions, changed
        )
        if changed and previous is not None:
            await self._broadcast_change(previous, snapshot)

    async def _broadcast_change(self, old: PricingSnapshot, new: PricingSnapshot) -> None:
        """将 DeepSeek 官方定价/版本变化交给麦麦总结后播报到目标群。

        流程：
        1. 先生成差异说明（build_change_report），
        2. 通过 `maisaka.context.append` 把差异作为上下文消息喂给目标群的麦麦，
        3. 通过 `maisaka.proactive.trigger` 触发麦麦主动处理一轮，让麦麦用自然语言
           总结这段差异并播报到该群。

        若请求麦麦失败（能力不可用等），回退为直接发送差异文本，保证信息不丢失。
        """
        group_ids = [str(g).strip() for g in self.config.schedule.target_group_ids if str(g).strip()]
        if not group_ids:
            return
        diff_text = build_change_report(datetime.now(BEIJING_TZ), old, new)
        for group_id in group_ids:
            try:
                stream_id = await self._resolve_group_stream(group_id)
                if not stream_id:
                    self.ctx.logger.warning("变更播报找不到群聊 stream_id: %s", group_id)
                    continue
                delivered = await self._notify_maisaka_summary(stream_id, diff_text)
                if not delivered:
                    # 麦麦总结不可用时，回退为直接发送差异文本，避免信息丢失。
                    await self.ctx.send.text(diff_text, stream_id)
            except Exception:
                self.ctx.logger.exception("变更播报发送失败: group=%s", group_id)

    async def _notify_maisaka_summary(self, stream_id: str, diff_text: str, instruction: str = "") -> bool:
        """将差异喂给麦麦并触发其总结回应；成功返回 True，能力不可用返回 False。

        Args:
            stream_id: 目标聊天流 ID。
            diff_text: 追加进上下文的差异/当前内容文本。
            instruction: 对麦麦的行为指示；为空时使用默认的「检测到变化请播报」语义。
        """
        try:
            # 1) 把差异作为上下文消息注入目标群聊天流，供麦麦读取。
            append_result = await self.ctx.maisaka.context.append(
                stream_id,
                [{"type": "text", "content": diff_text}],
                visible_text=diff_text,
                source_kind="plugin:deepseek-peak-valley",
            )
            # 2) 触发麦麦主动总结并回应。
            if not instruction:
                instruction = (
                    "检测到 DeepSeek 官方定价或模型版本发生变化，"
                    "请根据刚才追加的上下文，用简短自然的中文总结这次变化，并面向群成员播报。"
                )
            trigger_result = await self.ctx.maisaka.proactive.trigger(
                stream_id,
                instruction,
                reason="deepseek 官方定价/版本变更",
                metadata={"plugin": "deepseek-peak-valley", "diff": diff_text},
            )
            ok = bool(
                (isinstance(trigger_result, dict) and trigger_result.get("success"))
                or (isinstance(trigger_result, bool) and trigger_result)
                or trigger_result is not None
            )
            return ok
        except Exception as exc:
            self.ctx.logger.warning("触发麦麦总结失败，将回退为直发: %s", exc)
            return False

    async def _maybe_alert_failures(self) -> None:
        """连续抓取失败达到阈值后向群聊推送一次告警，避免刷屏。"""
        threshold = max(1, int(self.config.fetch.alert_after_failures))
        if self._pricing_cache.consecutive_failures < threshold:
            return
        # 距上次告警 6 小时内不再重复提醒。
        if time.time() - self._pricing_cache.last_alert_at < 6 * 3600:
            return
        self._pricing_cache.last_alert_at = time.time()
        self._save_pricing_cache()
        group_ids = [str(g).strip() for g in self.config.schedule.target_group_ids if str(g).strip()]
        text = (
            "【DeepSeek 定价获取异常】\n"
            f"连续 {self._pricing_cache.consecutive_failures} 次无法从官方页解析最新价格，"
            "当前使用缓存/默认价。请检查网络或官方页面是否改版。"
        )
        for group_id in group_ids:
            try:
                stream_id = await self._resolve_group_stream(group_id)
                if stream_id:
                    await self.ctx.send.text(text, stream_id)
            except Exception:
                self.ctx.logger.exception("失败告警发送失败: group=%s", group_id)

    async def _broadcast(self, text: str, transition_key: str) -> None:
        success_count = 0
        changed = False
        for raw_group_id in self.config.schedule.target_group_ids:
            group_id = str(raw_group_id).strip()
            if not group_id:
                continue
            target_key = f"{transition_key}@qq:{group_id}"
            if target_key in self._sent_transition_keys:
                continue
            try:
                stream_id = await self._resolve_group_stream(group_id)
                if not stream_id:
                    self.ctx.logger.warning("峰谷播报找不到群聊 stream_id: %s", group_id)
                    continue
                sent = await self.ctx.send.text(text, stream_id)
                if sent:
                    self._sent_transition_keys.add(target_key)
                    success_count += 1
                    changed = True
                else:
                    self.ctx.logger.warning("峰谷播报发送失败: group=%s stream=%s", group_id, stream_id)
            except Exception:
                self.ctx.logger.exception("峰谷播报异常: group=%s", group_id)

        if changed:
            self._save_state()
        self.ctx.logger.info("峰谷切换播报完成: %s，本轮成功 %d 群", transition_key, success_count)

    def _transition_complete(self, transition_key: str) -> bool:
        group_ids = {str(item).strip() for item in self.config.schedule.target_group_ids if str(item).strip()}
        if not group_ids:
            return True
        return all(f"{transition_key}@qq:{group_id}" in self._sent_transition_keys for group_id in group_ids)

    async def _resolve_group_stream(self, group_id: str) -> str:
        try:
            stream = await self.ctx.chat.get_stream_by_group_id(group_id, platform="qq")
        except Exception:
            self.ctx.logger.exception("查詢群聊 stream 失敗: %s", group_id)
            stream = None
        if isinstance(stream, dict):
            stream_id = str(stream.get("session_id") or stream.get("stream_id") or "")
            if stream_id:
                return stream_id

        try:
            result = await self.ctx.chat.open_session(
                platform="qq", chat_type="group", group_id=group_id, user_id=""
            )
        except Exception:
            self.ctx.logger.exception("開啟群聊 session 失敗: %s", group_id)
            return ""
        if isinstance(result, dict) and result.get("success", True):
            return str(result.get("session_id") or result.get("stream_id") or "")
        return ""

    def _load_state(self) -> None:
        try:
            payload = json.loads(self._data_file("state.json").read_text(encoding="utf-8"))
            keys = payload.get("sent_transition_keys", [])
            if isinstance(keys, list):
                self._sent_transition_keys = {str(item) for item in keys}
        except FileNotFoundError:
            return
        except Exception:
            self.ctx.logger.exception("讀取峰谷插件狀態失敗")

    def _save_state(self) -> None:
        try:
            state_file = self._data_file("state.json")
            state_file.parent.mkdir(parents=True, exist_ok=True)
            recent = sorted(self._sent_transition_keys)[-40:]
            temp = state_file.with_suffix(".tmp")
            temp.write_text(json.dumps({"sent_transition_keys": recent}, ensure_ascii=False), encoding="utf-8")
            temp.replace(state_file)
        except Exception:
            self.ctx.logger.exception("儲存峰谷插件狀態失敗")

    def _load_pricing_cache(self) -> None:
        try:
            payload = json.loads(self._data_file("pricing.json").read_text(encoding="utf-8"))
            self._pricing_cache = PricingCache.from_dict(payload)
        except FileNotFoundError:
            return
        except Exception:
            self.ctx.logger.exception("讀取定價緩存失敗")

    def _save_pricing_cache(self) -> None:
        try:
            pricing_file = self._data_file("pricing.json")
            pricing_file.parent.mkdir(parents=True, exist_ok=True)
            temp = pricing_file.with_suffix(".tmp")
            temp.write_text(json.dumps(self._pricing_cache.to_dict(), ensure_ascii=False), encoding="utf-8")
            temp.replace(pricing_file)
        except Exception:
            self.ctx.logger.exception("儲存定價緩存失敗")

    def _prune_rate_limits(self, now: float, horizon: int) -> None:
        cutoff = now - max(60, horizon)
        self._last_user_query = {key: value for key, value in self._last_user_query.items() if value >= cutoff}
        self._last_stream_query = {key: value for key, value in self._last_stream_query.items() if value >= cutoff}
        self._last_check_query = {key: value for key, value in self._last_check_query.items() if value >= cutoff}


def create_plugin() -> DeepSeekPeakValleyPlugin:
    return DeepSeekPeakValleyPlugin()
