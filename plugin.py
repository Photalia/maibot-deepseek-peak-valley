"""DeepSeek V4 峰谷價格播報插件。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import asyncio
import hashlib
import json
import math
import re
import time

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase

BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
PRICING_URL = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
DATA_DIR = Path(__file__).resolve().parent / "data"
STATE_FILE = DATA_DIR / "state.json"
WEEKDAY_NAMES = ("週一", "週二", "週三", "週四", "週五", "週六", "週日")
MODEL_KEYS = {
    "deepseek-v4-flash": "flash",
    "deepseek-v4-pro": "pro",
}


class PluginSectionConfig(PluginConfigBase):
    enabled: bool = Field(default=True, description="是否啟用插件")
    config_version: str = Field(default="1.1.0", description="配置版本")


class ScheduleConfig(PluginConfigBase):
    enabled: bool = Field(default=True, description="是否在峰谷切換時主動播報")
    target_group_ids: list[str] = Field(default_factory=list, description="主動播報的 QQ 群號")
    grace_seconds: int = Field(default=120, description="切換後允許補發的秒數")


class PricingConfig(PluginConfigBase):
    source_url: str = Field(default=PRICING_URL, description="DeepSeek 官方價格頁")
    exa_server_name: str = Field(default="ExaSearchMCP", description="MaiBot 中已配置的 Exa MCP 服務名稱")
    refresh_hour: int = Field(default=0, ge=0, le=23, description="每日更新價格的小時（北京時間）")
    refresh_minute: int = Field(default=0, ge=0, le=59, description="每日更新價格的分鐘（北京時間）")
    fetch_max_characters: int = Field(default=20000, ge=4000, le=100000, description="Exa 抓取頁面的最大字元數")


class ReplyerConfig(PluginConfigBase):
    enabled: bool = Field(default=True, description="每天使用 MaiBot Replyer 生成峰／谷銳評")
    temperature: float = Field(default=0.9, ge=0.0, le=2.0, description="銳評生成溫度")
    max_tokens: int = Field(default=240, ge=64, le=1024, description="銳評生成最大 Token")
    prompt: str = Field(
        default="根據今天的 DeepSeek 峰谷價格，各寫一句有趣、尖銳但不惡毒的短評。可以玩梁文峰／梁文谷的諧音梗。",
        description="每日銳評提示詞",
    )


class RateLimitConfig(PluginConfigBase):
    user_cooldown_seconds: int = Field(default=30, description="同一使用者查詢冷卻秒數")
    stream_cooldown_seconds: int = Field(default=5, description="同一聊天查詢冷卻秒數")


class PeakValleyConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    replyer: ReplyerConfig = Field(default_factory=ReplyerConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)


@dataclass(frozen=True)
class PriceSnapshot:
    effective_date: str
    fetched_at: str
    source_url: str
    source_method: str
    source_digest: str
    peak_weekdays: tuple[int, ...]
    peak_windows: tuple[tuple[int, int], ...]
    prices: dict[str, dict[str, dict[str, str]]]
    critiques: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "effective_date": self.effective_date,
            "fetched_at": self.fetched_at,
            "source_url": self.source_url,
            "source_method": self.source_method,
            "source_digest": self.source_digest,
            "peak_weekdays": list(self.peak_weekdays),
            "peak_windows": [list(item) for item in self.peak_windows],
            "prices": self.prices,
            "critiques": self.critiques,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PriceSnapshot":
        return cls(
            effective_date=str(payload["effective_date"]),
            fetched_at=str(payload["fetched_at"]),
            source_url=str(payload["source_url"]),
            source_method=str(payload.get("source_method") or "exa_mcp"),
            source_digest=str(payload.get("source_digest") or ""),
            peak_weekdays=tuple(int(item) for item in payload["peak_weekdays"]),
            peak_windows=tuple((int(item[0]), int(item[1])) for item in payload["peak_windows"]),
            prices={str(key): value for key, value in dict(payload["prices"]).items()},
            critiques={str(key): str(value) for key, value in dict(payload.get("critiques") or {}).items()},
        )


def _split_markdown_row(line: str) -> list[str]:
    return [cell.strip().replace("\x00", "") for cell in line.strip().strip("|").split("|")]


def _price_number(value: str) -> str:
    matched = re.search(r"\d+(?:\.\d+)?", value)
    if not matched:
        raise ValueError(f"無法解析價格：{value!r}")
    number = matched.group(0)
    if "." in number:
        number = number.rstrip("0").rstrip(".")
    return number


def _parse_clock(value: str) -> int:
    hour_text, minute_text = (value.split(":", 1) + ["0"])[:2]
    hour = int(hour_text)
    minute = int(minute_text)
    if not 0 <= hour <= 24 or not 0 <= minute <= 59 or (hour == 24 and minute != 0):
        raise ValueError(f"無效時刻：{value}")
    return hour * 60 + minute


def _parse_peak_weekdays(text: str) -> tuple[int, ...]:
    range_match = re.search(r"周([一二三四五六日天])至周([一二三四五六日天])", text)
    mapping = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
    if range_match:
        start = mapping[range_match.group(1)]
        end = mapping[range_match.group(2)]
        if start <= end:
            return tuple(range(start, end + 1))
        return tuple(list(range(start, 7)) + list(range(0, end + 1)))
    if "工作日" in text:
        return (0, 1, 2, 3, 4)
    raise ValueError("官方內容中找不到高峰星期規則")


def _parse_peak_windows(text: str) -> tuple[tuple[int, int], ...]:
    matches = re.findall(r"(\d{1,2}(?::\d{2})?)\s*[-–—~～至]\s*(\d{1,2}(?::\d{2})?)", text)
    windows = tuple((_parse_clock(start), _parse_clock(end)) for start, end in matches)
    valid = tuple((start, end) for start, end in windows if 0 <= start < end <= 24 * 60)
    if not valid:
        raise ValueError("官方內容中找不到高峰時間範圍")
    return valid


def parse_pricing_markdown(markdown: str, now: datetime, source_url: str = PRICING_URL) -> PriceSnapshot:
    """將 Exa 擷取的 DeepSeek 官方價格頁轉為可驗證的結構化快照。"""
    lines = [line.strip() for line in markdown.replace("\x00", "").splitlines()]
    header_cells: list[str] | None = None
    table_rows: list[list[str]] = []
    in_table = False
    for line in lines:
        if line.startswith("|") and "deepseek-v4-flash" in line and "deepseek-v4-pro" in line:
            header_cells = _split_markdown_row(line)
            in_table = True
            continue
        if in_table and line.startswith("|"):
            cells = _split_markdown_row(line)
            if cells and not all(re.fullmatch(r"[-: ]+", cell or "-") for cell in cells):
                table_rows.append(cells)
            continue
        if in_table:
            break

    if not header_cells:
        raise ValueError("官方內容中找不到 V4 Flash／Pro 價格表")
    model_columns: dict[str, int] = {}
    for column, model_name in enumerate(header_cells):
        if model_name in MODEL_KEYS:
            model_columns[MODEL_KEYS[model_name]] = column
    if set(model_columns) != {"flash", "pro"}:
        raise ValueError("官方價格表缺少 V4 Flash 或 V4 Pro")

    prices: dict[str, dict[str, dict[str, str]]] = {
        "flash": {"peak": {}, "off_peak": {}},
        "pro": {"peak": {}, "off_peak": {}},
    }
    metric = ""
    for cells in table_rows:
        cells += [""] * (len(header_cells) - len(cells))
        joined = " ".join(cells[:3])
        if "缓存命中" in joined or "緩存命中" in joined:
            metric = "cache"
        elif "缓存未命中" in joined or "緩存未命中" in joined:
            metric = "input"
        elif "tokens输出" in joined or "tokens輸出" in joined:
            metric = "output"
        period = "off_peak" if "空闲时段" in joined or "空閒時段" in joined else "peak" if "高峰时段" in joined else ""
        if not metric or not period:
            continue
        for model_key, column in model_columns.items():
            prices[model_key][period][metric] = _price_number(cells[column])

    expected_metrics = {"input", "output", "cache"}
    for model_key in ("flash", "pro"):
        for period in ("peak", "off_peak"):
            if set(prices[model_key][period]) != expected_metrics:
                raise ValueError(f"{model_key}/{period} 價格欄位不完整")

    schedule_text = next((line for line in lines if "高峰时段为北京时间" in line), "")
    if not schedule_text:
        raise ValueError("官方內容中找不到高峰時段說明")
    return PriceSnapshot(
        effective_date=now.strftime("%Y-%m-%d"),
        fetched_at=now.isoformat(),
        source_url=source_url,
        source_method="exa_mcp.web_fetch_exa",
        source_digest=hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        peak_weekdays=_parse_peak_weekdays(schedule_text),
        peak_windows=_parse_peak_windows(schedule_text),
        prices=prices,
        critiques={},
    )


def is_peak(now: datetime, snapshot: PriceSnapshot) -> bool:
    if now.weekday() not in snapshot.peak_weekdays:
        return False
    minute = now.hour * 60 + now.minute
    return any(start <= minute < end for start, end in snapshot.peak_windows)


def next_transition(now: datetime, snapshot: PriceSnapshot) -> tuple[datetime, bool]:
    """尋找未來第一次實際峰谷狀態切換，會自動跳過週末。"""
    current = is_peak(now, snapshot)
    candidates: list[datetime] = []
    for day_offset in range(15):
        day = now + timedelta(days=day_offset)
        if day.weekday() not in snapshot.peak_weekdays:
            continue
        for start, end in snapshot.peak_windows:
            for minute in (start, end):
                candidate = day.replace(
                    hour=minute // 60,
                    minute=minute % 60,
                    second=0,
                    microsecond=0,
                )
                if candidate > now:
                    candidates.append(candidate)
    for candidate in sorted(candidates):
        after = candidate + timedelta(seconds=1)
        if is_peak(after, snapshot) != current:
            return candidate, is_peak(after, snapshot)
    raise RuntimeError("未能在未來 15 天內找到峰谷切換")


def previous_transition(now: datetime, snapshot: PriceSnapshot) -> datetime | None:
    """尋找最近一次實際狀態切換；週末不會虛構每日切換。"""
    candidates: list[datetime] = []
    for day_offset in range(15):
        day = now - timedelta(days=day_offset)
        if day.weekday() not in snapshot.peak_weekdays:
            continue
        for start, end in snapshot.peak_windows:
            for minute in (start, end):
                candidate = day.replace(
                    hour=minute // 60,
                    minute=minute % 60,
                    second=0,
                    microsecond=0,
                )
                if candidate <= now:
                    candidates.append(candidate)
    for candidate in sorted(candidates, reverse=True):
        before = candidate - timedelta(seconds=1)
        after = candidate + timedelta(seconds=1)
        if is_peak(before, snapshot) != is_peak(after, snapshot):
            return candidate
    return None


def human_duration(delta: timedelta) -> str:
    minutes = max(1, math.ceil(delta.total_seconds() / 60))
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}天")
    if hours:
        parts.append(f"{hours}小時")
    if minutes:
        parts.append(f"{minutes}分鐘")
    return "".join(parts) or "1分鐘"


def format_time(now: datetime) -> str:
    return f"{now:%Y/%m/%d/%H/%M} {WEEKDAY_NAMES[now.weekday()]}"


def build_report(now: datetime, snapshot: PriceSnapshot) -> str:
    peak = is_peak(now, snapshot)
    following, enters_peak = next_transition(now, snapshot)
    period_key = "peak" if peak else "off_peak"
    current_name = "梁文峰" if peak else "梁文谷"
    next_name = "梁文峰" if enters_peak else "梁文谷"
    flash = snapshot.prices["flash"][period_key]
    pro = snapshot.prices["pro"][period_key]
    critique = snapshot.critiques.get(period_key) or (
        "五梁液今天也在按 Token 斟酒。" if peak else "梁白開時段，省下來的都是自己的。"
    )
    return (
        f"時間：{format_time(now)}\n"
        f"當前時段：「{current_name}」\n"
        f"V4 Flash價格：輸入{flash['input']}／輸出{flash['output']}／緩存{flash['cache']}\n"
        f"V4 Pro價格：輸入{pro['input']}／輸出{pro['output']}／緩存{pro['cache']}\n"
        f"下次「{next_name}」在{human_duration(following - now)}後（{format_time(following)}）\n"
        f"銳評：{critique}\n"
        "單位：元／百萬 tokens"
    )


def _config_value(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _extract_mcp_text(result: Any) -> str:
    content = _config_value(result, "content", []) or []
    texts = [str(_config_value(item, "text", "")) for item in content if _config_value(item, "text", "")]
    text = "\n".join(texts).strip()
    if not text:
        raise RuntimeError("Exa MCP 沒有返回文字內容")
    if bool(_config_value(result, "isError", False)):
        raise RuntimeError(f"Exa MCP 返回錯誤：{text[:300]}")
    return text


def parse_replyer_critiques(raw: str) -> dict[str, str]:
    """從可能夾帶推理文字的 Replyer 回覆中提取最後一個合法銳評 JSON。"""
    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []
    for index, character in enumerate(raw):
        if character != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(raw[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and {"peak", "off_peak"}.issubset(payload):
            candidates.append(payload)
    if not candidates:
        raise ValueError("Replyer 回覆中找不到 peak／off_peak JSON")

    forbidden = re.compile(
        r"\d|元|美元|美金|人民幣|人民币|input|output|cache|flash|pro|輸入|输入|輸出|输出|緩存|缓存",
        re.IGNORECASE,
    )
    result: dict[str, str] = {}
    for key in ("peak", "off_peak"):
        text = str(candidates[-1][key]).strip().strip("\"'「」『』“”‘’")
        text = next((line.strip() for line in text.splitlines() if line.strip()), "")[:60]
        if not text:
            raise ValueError(f"{key} 銳評為空")
        if forbidden.search(text):
            raise ValueError(f"{key} 銳評包含價格事實或數字")
        result[key] = text
    return result


class DeepSeekPeakValleyPlugin(MaiBotPlugin):
    config_model = PeakValleyConfig

    def __init__(self) -> None:
        super().__init__()
        self._schedule_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._refresh_lock = asyncio.Lock()
        self._next_refresh_attempt_monotonic = 0.0
        self._last_user_query: dict[str, float] = {}
        self._last_stream_query: dict[str, float] = {}
        self._sent_transition_keys: set[str] = set()
        self._snapshot: PriceSnapshot | None = None

    async def on_load(self) -> None:
        self._load_state()
        self._stop_event.clear()
        self._schedule_task = asyncio.create_task(self._schedule_loop(), name="deepseek-peak-valley")
        self.ctx.logger.info(
            "DeepSeek 峰谷插件已載入，快照日期=%s，主動播報群=%s",
            self._snapshot.effective_date if self._snapshot else "尚無",
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
        if (
            monotonic_now - self._last_user_query.get(user_key, 0.0) < user_cd
            or monotonic_now - self._last_stream_query.get(stream_id, 0.0) < stream_cd
        ):
            return True, "峰谷查詢處於冷卻中", True

        self._last_user_query[user_key] = monotonic_now
        self._last_stream_query[stream_id] = monotonic_now
        now = datetime.now(BEIJING_TZ)
        try:
            snapshot = await self._ensure_snapshot(now)
        except Exception as exc:
            self.ctx.logger.exception("峰谷查詢更新官方價格失敗")
            await self.ctx.send.text(f"DeepSeek 官方價格更新失敗，暫時無法保證報價正確：{exc}", stream_id)
            return True, "官方價格更新失敗", True
        await self.ctx.send.text(build_report(now, snapshot), stream_id)
        self._prune_rate_limits(monotonic_now, max(user_cd, stream_cd) * 4)
        return True, "已顯示 DeepSeek 峰谷價格", True

    async def _schedule_loop(self) -> None:
        while not self._stop_event.is_set():
            now = datetime.now(BEIJING_TZ)
            try:
                snapshot = await self._ensure_snapshot(now)
                if self.config.schedule.enabled:
                    transition = previous_transition(now, snapshot)
                    if transition is not None:
                        transition_key = transition.strftime("%Y-%m-%dT%H:%M")
                        elapsed = (now - transition).total_seconds()
                        grace = max(0, int(self.config.schedule.grace_seconds))
                        if 0 <= elapsed <= grace and not self._transition_complete(transition_key):
                            await self._broadcast(build_report(now, snapshot), transition_key)
            except Exception:
                self.ctx.logger.exception("峰谷排程更新／播報失敗")

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=10.0)
                return
            except asyncio.TimeoutError:
                pass

    async def _ensure_snapshot(self, now: datetime) -> PriceSnapshot:
        refresh_at = now.replace(
            hour=int(self.config.pricing.refresh_hour),
            minute=int(self.config.pricing.refresh_minute),
            second=0,
            microsecond=0,
        )
        expected_date = now.strftime("%Y-%m-%d") if now >= refresh_at else (now - timedelta(days=1)).strftime("%Y-%m-%d")
        if self._snapshot is not None and self._snapshot.effective_date >= expected_date:
            return self._snapshot
        if time.monotonic() < self._next_refresh_attempt_monotonic:
            raise RuntimeError("官方價格更新正在等待下一次重試")
        async with self._refresh_lock:
            if self._snapshot is not None and self._snapshot.effective_date >= expected_date:
                return self._snapshot
            if time.monotonic() < self._next_refresh_attempt_monotonic:
                raise RuntimeError("官方價格更新正在等待下一次重試")
            try:
                refreshed = await self._refresh_snapshot(now)
            except Exception:
                # 避免官方頁或 MCP 故障時由十秒排程迴圈持續轟炸外部服務。
                self._next_refresh_attempt_monotonic = time.monotonic() + 300.0
                raise
            self._next_refresh_attempt_monotonic = 0.0
            self._snapshot = refreshed
            self._save_state()
            return self._snapshot

    async def _refresh_snapshot(self, now: datetime) -> PriceSnapshot:
        markdown = await self._fetch_pricing_via_exa()
        snapshot = parse_pricing_markdown(markdown, now, str(self.config.pricing.source_url).strip() or PRICING_URL)
        critiques = await self._generate_daily_critiques(snapshot)
        snapshot = PriceSnapshot(
            effective_date=snapshot.effective_date,
            fetched_at=snapshot.fetched_at,
            source_url=snapshot.source_url,
            source_method=snapshot.source_method,
            source_digest=snapshot.source_digest,
            peak_weekdays=snapshot.peak_weekdays,
            peak_windows=snapshot.peak_windows,
            prices=snapshot.prices,
            critiques=critiques,
        )
        self.ctx.logger.info(
            "DeepSeek 官方價格已更新：日期=%s，星期=%s，時段=%s，digest=%s",
            snapshot.effective_date,
            snapshot.peak_weekdays,
            snapshot.peak_windows,
            snapshot.source_digest[:12],
        )
        return snapshot

    async def _fetch_pricing_via_exa(self) -> str:
        try:
            import httpx
            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client
        except ImportError as exc:
            raise RuntimeError("群克未安裝 MCP Python SDK，無法調用 Exa MCP") from exc

        raw_servers = await self.ctx.config.get("mcp.servers", [])
        servers = raw_servers if isinstance(raw_servers, list) else []
        wanted_name = str(self.config.pricing.exa_server_name).strip().lower()
        server = next(
            (
                item
                for item in servers
                if bool(_config_value(item, "enabled", True))
                and str(_config_value(item, "name", "")).strip().lower() == wanted_name
            ),
            None,
        )
        if server is None:
            raise RuntimeError(f"MaiBot 全局配置中找不到已啟用的 MCP 服務：{self.config.pricing.exa_server_name}")
        if str(_config_value(server, "transport", "")) != "streamable_http":
            raise RuntimeError("Exa MCP 必須使用 streamable_http 傳輸")

        url = str(_config_value(server, "url", "")).strip()
        if not url:
            raise RuntimeError("Exa MCP 未配置 URL")
        base_headers = {str(key): str(value) for key, value in dict(_config_value(server, "headers", {}) or {}).items()}
        authorization = _config_value(server, "authorization", {}) or {}
        if str(_config_value(authorization, "mode", "none")) == "bearer":
            token = str(_config_value(authorization, "bearer_token", "")).strip()
            if token:
                base_headers["Authorization"] = f"Bearer {token}"
        http_timeout = float(_config_value(server, "http_timeout_seconds", 30.0))
        read_timeout = float(_config_value(server, "read_timeout_seconds", 300.0))

        def http_client_factory(headers=None, timeout=None, auth=None):
            del auth
            merged = dict(base_headers)
            merged.update(headers or {})
            return httpx.AsyncClient(headers=merged, timeout=timeout or http_timeout)

        async with streamablehttp_client(
            url,
            headers=base_headers,
            timeout=http_timeout,
            sse_read_timeout=read_timeout,
            httpx_client_factory=http_client_factory,
        ) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(
                    "web_fetch_exa",
                    {
                        "urls": [str(self.config.pricing.source_url).strip() or PRICING_URL],
                        "maxCharacters": int(self.config.pricing.fetch_max_characters),
                    },
                )
                return _extract_mcp_text(result)

    async def _generate_daily_critiques(self, snapshot: PriceSnapshot) -> dict[str, str]:
        defaults = {
            "peak": "五梁液今天也在按 Token 斟酒。",
            "off_peak": "梁白開時段，省下來的都是自己的。",
        }
        if not self.config.replyer.enabled:
            return defaults
        nickname = await self.ctx.config.get("bot.nickname", "麥麥")
        personality = await self.ctx.config.get("personality.personality", "")
        reply_style = await self.ctx.config.get("personality.reply_style", "")
        prompt = [
            {
                "role": "system",
                "content": (
                    "你替 Bot 撰寫每天使用的 DeepSeek 價格銳評。遵循人格與表達風格，"
                    "但不得修改、推測或重述價格事實。只輸出嚴格 JSON，不要 Markdown。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Bot 暱稱：{nickname}\n人格：{personality}\n表達風格：{reply_style}\n"
                    f"自訂要求：{self.config.replyer.prompt}\n"
                    f"官方價格結構：{json.dumps(snapshot.prices, ensure_ascii=False)}\n"
                    f"高峰星期：{snapshot.peak_weekdays}；高峰分鐘區間：{snapshot.peak_windows}\n"
                    "輸出格式：{\"peak\":\"高峰時段短評\",\"off_peak\":\"空閒時段短評\"}。"
                    "兩句都必須可獨立直接接在『銳評：』後，每句最多 45 個漢字，不加引號。"
                    "銳評只負責氣氛和玩梗，禁止包含任何數字、幣種、價格、模型名或輸入輸出緩存資訊。"
                ),
            },
        ]
        result = await self.ctx.llm.generate(
            prompt,
            model="replyer",
            temperature=float(self.config.replyer.temperature),
            max_tokens=int(self.config.replyer.max_tokens),
        )
        if not isinstance(result, dict) or not result.get("success", False):
            detail = str(result.get("error") or result.get("response") or "") if isinstance(result, dict) else ""
            self.ctx.logger.warning("Replyer 每日銳評生成失敗，使用固定銳評：%s", detail)
            return defaults
        raw = str(result.get("content") or result.get("response") or result.get("text") or "").strip()
        try:
            return parse_replyer_critiques(raw)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self.ctx.logger.warning("Replyer 銳評格式無效，使用固定銳評：%s；原文=%s", exc, raw[:300])
            return defaults

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
                    self.ctx.logger.warning("峰谷播報找不到群聊 stream_id: %s", group_id)
                    continue
                sent = await self.ctx.send.text(text, stream_id)
                if sent:
                    self._sent_transition_keys.add(target_key)
                    success_count += 1
                    changed = True
                else:
                    self.ctx.logger.warning("峰谷播報發送失敗: group=%s stream=%s", group_id, stream_id)
            except Exception:
                self.ctx.logger.exception("峰谷播報異常: group=%s", group_id)
        if changed:
            self._save_state()
        self.ctx.logger.info("峰谷切換播報完成: %s，本輪成功 %d 群", transition_key, success_count)

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

    def _prune_rate_limits(self, now: float, max_age: int) -> None:
        self._last_user_query = {key: value for key, value in self._last_user_query.items() if now - value < max_age}
        self._last_stream_query = {
            key: value for key, value in self._last_stream_query.items() if now - value < max_age
        }

    def _load_state(self) -> None:
        try:
            payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            keys = payload.get("sent_transition_keys", [])
            if isinstance(keys, list):
                self._sent_transition_keys = {str(item) for item in keys}
            snapshot = payload.get("pricing_snapshot")
            if isinstance(snapshot, dict):
                self._snapshot = PriceSnapshot.from_dict(snapshot)
        except FileNotFoundError:
            return
        except Exception:
            self.ctx.logger.exception("讀取峰谷插件狀態失敗")

    def _save_state(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            recent = sorted(self._sent_transition_keys)[-80:]
            payload: dict[str, Any] = {"sent_transition_keys": recent}
            if self._snapshot is not None:
                payload["pricing_snapshot"] = self._snapshot.to_dict()
            temp = STATE_FILE.with_suffix(".tmp")
            temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(STATE_FILE)
        except Exception:
            self.ctx.logger.exception("儲存峰谷插件狀態失敗")


def create_plugin() -> DeepSeekPeakValleyPlugin:
    return DeepSeekPeakValleyPlugin()
