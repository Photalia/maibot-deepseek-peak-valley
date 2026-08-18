"""DeepSeek V4 峰谷價格播報插件。"""

from __future__ import annotations

import asyncio
import json
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase

BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
TRANSITION_HOURS = (9, 12, 14, 18)
DATA_DIR = Path(__file__).resolve().parent / "data"
STATE_FILE = DATA_DIR / "state.json"


class PluginSectionConfig(PluginConfigBase):
    enabled: bool = Field(default=True, description="是否啟用插件")
    config_version: str = Field(default="1.0.0", description="配置版本")


class ScheduleConfig(PluginConfigBase):
    enabled: bool = Field(default=True, description="是否在峰谷切換時主動播報")
    target_group_ids: list[str] = Field(default_factory=list, description="主動播報的 QQ 群號")
    grace_seconds: int = Field(default=120, description="切換後允許補發的秒數")


class RateLimitConfig(PluginConfigBase):
    user_cooldown_seconds: int = Field(default=30, description="同一使用者查詢冷卻秒數")
    stream_cooldown_seconds: int = Field(default=5, description="同一聊天查詢冷卻秒數")


class PeakValleyConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)


def is_peak(now: datetime) -> bool:
    """判斷北京時間是否為官方高峰時段。"""
    minute = now.hour * 60 + now.minute
    return 9 * 60 <= minute < 12 * 60 or 14 * 60 <= minute < 18 * 60


def next_transition(now: datetime) -> tuple[datetime, bool]:
    """回傳下一個切換時間，以及切換後是否進入高峰。"""
    transitions = ((9, True), (12, False), (14, True), (18, False))
    for hour, enters_peak in transitions:
        candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if candidate > now:
            return candidate, enters_peak
    tomorrow = now + timedelta(days=1)
    return tomorrow.replace(hour=9, minute=0, second=0, microsecond=0), True


def previous_transition(now: datetime) -> datetime:
    """回传当前时刻之前最近一次峰谷切换时间。"""
    for hour in reversed(TRANSITION_HOURS):
        candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if candidate <= now:
            return candidate
    yesterday = now - timedelta(days=1)
    return yesterday.replace(hour=18, minute=0, second=0, microsecond=0)


def human_duration(delta: timedelta) -> str:
    minutes = max(1, math.ceil(delta.total_seconds() / 60))
    hours, rest = divmod(minutes, 60)
    if hours and rest:
        return f"{hours}小时{rest}分钟后"
    if hours:
        return f"{hours}小时后"
    return f"{rest}分钟后"


def build_report(now: datetime) -> str:
    peak = is_peak(now)
    following, enters_peak = next_transition(now)
    if peak:
        prices = (
            "V4 Flash:输入3输出9缓存0.1\n"
            "V4 Pro:输入9输出27缓存0.3"
        )
        bottle = "饮料瓶指标:五梁液"
    else:
        prices = (
            "V4 Flash:输入1.5输出4.5缓存0.05\n"
            "V4 Pro:输入4.5输出13.5缓存0.15"
        )
        bottle = "饮料瓶评估:梁白开"
    current_name = "梁文峰" if peak else "梁文谷"
    next_name = "梁文峰" if enters_peak else "梁文谷"
    return (
        f"当前时间是 {now:%H:%M}\n"
        f"处于「{current_name}」时段\n"
        f"{prices}\n"
        f"下一次「{next_name}」时间在{human_duration(following - now)}\n"
        f"{bottle}"
    )


class DeepSeekPeakValleyPlugin(MaiBotPlugin):
    config_model = PeakValleyConfig

    def __init__(self) -> None:
        super().__init__()
        self._schedule_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._last_user_query: dict[str, float] = {}
        self._last_stream_query: dict[str, float] = {}
        self._sent_transition_keys: set[str] = set()

    async def on_load(self) -> None:
        self._load_state()
        self._stop_event.clear()
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
            # 靜默吞掉冷卻期內的重複命令，避免「限流提示」本身被刷屏。
            return True, "峰谷查詢處於冷卻中", True

        self._last_user_query[user_key] = monotonic_now
        self._last_stream_query[stream_id] = monotonic_now
        await self.ctx.send.text(build_report(datetime.now(BEIJING_TZ)), stream_id)
        self._prune_rate_limits(monotonic_now, max(user_cd, stream_cd) * 4)
        return True, "已顯示 DeepSeek 峰谷價格", True

    async def _schedule_loop(self) -> None:
        """等待切换点，并在 grace 窗口内对失败群聊进行独立重试。"""
        while not self._stop_event.is_set():
            now = datetime.now(BEIJING_TZ)
            transition = previous_transition(now)
            transition_key = transition.strftime("%Y-%m-%dT%H:%M")
            grace = max(0, int(self.config.schedule.grace_seconds))
            elapsed = (now - transition).total_seconds()

            if 0 <= elapsed <= grace and not self._transition_complete(transition_key):
                await self._broadcast(build_report(now), transition_key)

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

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                return
            except asyncio.TimeoutError:
                pass

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
            payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            keys = payload.get("sent_transition_keys", [])
            if isinstance(keys, list):
                self._sent_transition_keys = {str(item) for item in keys}
        except FileNotFoundError:
            return
        except Exception:
            self.ctx.logger.exception("讀取峰谷插件狀態失敗")

    def _save_state(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            # 只保留最近資料，避免狀態檔無限增長。
            recent = sorted(self._sent_transition_keys)[-40:]
            temp = STATE_FILE.with_suffix(".tmp")
            temp.write_text(json.dumps({"sent_transition_keys": recent}, ensure_ascii=False), encoding="utf-8")
            temp.replace(STATE_FILE)
        except Exception:
            self.ctx.logger.exception("儲存峰谷插件狀態失敗")

    def _prune_rate_limits(self, now: float, horizon: int) -> None:
        cutoff = now - max(60, horizon)
        self._last_user_query = {key: value for key, value in self._last_user_query.items() if value >= cutoff}
        self._last_stream_query = {key: value for key, value in self._last_stream_query.items() if value >= cutoff}


def create_plugin() -> DeepSeekPeakValleyPlugin:
    return DeepSeekPeakValleyPlugin()
