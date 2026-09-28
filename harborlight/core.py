"""Harborlight 灯塔夜班值守核心逻辑。

纯标准库实现。每个夜班通过 :meth:`NightWatch.settle_night` 恰好结算一次：
重复调用幂等，停电回合的电池扣减只发生一次。状态以 JSON 存档保存，
随机天气支持固定种子以便离线、可重复的演示。
"""

from __future__ import annotations

import json
import random
from copy import deepcopy
from typing import Any, Iterable, Optional

__all__ = [
    "HarborError",
    "UnknownBeaconError",
    "AlreadyReportedError",
    "ChannelBusyError",
    "InsufficientMaterialsError",
    "NothingToUndoError",
    "Beacon",
    "NightWatch",
    "WEATHER_CLEAR",
    "WEATHER_FOG",
    "WEATHER_STORM",
    "WEATHERS",
    "SAVE_VERSION",
    "BATTERY_MIN",
    "BATTERY_MAX",
    "BLACKOUT_THRESHOLD",
    "FOG_DRAIN",
    "STORM_DRAIN",
    "save_game",
    "load_game",
]

WEATHER_CLEAR = "clear"
WEATHER_FOG = "fog"
WEATHER_STORM = "storm"
WEATHERS = (WEATHER_CLEAR, WEATHER_FOG, WEATHER_STORM)

BATTERY_MIN = 0
BATTERY_MAX = 100
BLACKOUT_THRESHOLD = 20
FOG_DRAIN = 6
STORM_DRAIN = 12
NIGHT_RECHARGE = 5

SAVE_VERSION = 1


class HarborError(Exception):
    """所有值守业务错误的基类。"""


class UnknownBeaconError(HarborError):
    """引用了不存在的航标。"""


class AlreadyReportedError(HarborError):
    """同一航标在同一夜班重复上报告警。"""


class ChannelBusyError(HarborError):
    """船只申请通过时航道锁仍被占用。"""


class InsufficientMaterialsError(HarborError):
    """维修材料不足。"""


class NothingToUndoError(HarborError):
    """没有可以撤销的巡检。"""


def _clamp_battery(value: Any) -> int:
    battery = int(value)
    if battery < BATTERY_MIN:
        return BATTERY_MIN
    if battery > BATTERY_MAX:
        return BATTERY_MAX
    return battery


class Beacon:
    """单个航标（灯塔/雾号浮标）的运行状态。"""

    def __init__(self, name: str, battery: int = BATTERY_MAX, damaged: bool = False):
        self.name = str(name)
        self._battery = _clamp_battery(battery)
        self.damaged = bool(damaged)
        self.last_reading: Optional[float] = None
        self.reported = False
        self.history: list[float] = []

    @property
    def battery(self) -> int:
        return self._battery

    @battery.setter
    def battery(self, value: Any) -> None:
        self._battery = _clamp_battery(value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "battery": self.battery,
            "damaged": self.damaged,
            "last_reading": self.last_reading,
            "reported": self.reported,
            "history": list(self.history),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Beacon":
        if not isinstance(data, dict) or "name" not in data:
            raise HarborError("存档中的航标数据非法")
        beacon = cls.__new__(cls)
        beacon.name = str(data["name"])
        beacon._battery = _clamp_battery(data.get("battery", BATTERY_MAX))
        beacon.damaged = bool(data.get("damaged", False))
        reading = data.get("last_reading")
        beacon.last_reading = float(reading) if reading is not None else None
        beacon.reported = bool(data.get("reported", False))
        beacon.history = [float(value) for value in data.get("history", [])]
        return beacon


class NightWatch:
    """一次正在进行的灯塔夜班值守。"""

    def __init__(
        self,
        beacon_names: Optional[Iterable[str]] = None,
        materials: int = 3,
        seed: Optional[int] = None,
    ):
        names = list(beacon_names) if beacon_names is not None else ["north-tower", "east-bell"]
        if not names:
            raise HarborError("至少需要一个航标")

        self.beacons: dict[str, Beacon] = {}
        for name in names:
            key = str(name)
            if key in self.beacons:
                raise HarborError(f"航标名称重复: {key}")
            self.beacons[key] = Beacon(key)

        materials = int(materials)
        if materials < 0:
            raise HarborError("维修材料不能为负")
        self.materials = materials

        self.night_number = 1
        self.weather = WEATHER_CLEAR
        self.fog_lamp_on = False
        self.vessel_visible = True
        self.channel_lock: Optional[str] = None
        self.alarm_count = 0
        self.danger = False
        self.blackout = False
        self.settled = False
        self.log: list[str] = []
        self.seed = seed
        self._rng = random.Random(seed)

    # ------------------------------------------------------------------ 工具

    def _beacon(self, name: str) -> Beacon:
        try:
            return self.beacons[str(name)]
        except KeyError:
            raise UnknownBeaconError(f"未知航标: {name}") from None

    @property
    def night_settled(self) -> bool:
        return self.settled

    def set_weather(self, weather: str) -> str:
        if weather not in WEATHERS:
            raise HarborError(f"非法天气: {weather}")
        self.weather = weather
        self._refresh_visibility()
        # 注意：切换天气本身不清除危险标志，危险只能在结算点按条件解除。
        return weather

    def roll_weather(self) -> str:
        """按固定权重推进天气（随机流由种子控制，结果可重复）。"""
        roll = self._rng.random()
        if roll < 0.6:
            weather = WEATHER_CLEAR
        elif roll < 0.85:
            weather = WEATHER_FOG
        else:
            weather = WEATHER_STORM
        return self.set_weather(weather)

    # ------------------------------------------------------------------ 设备

    def set_fog_lamp(self, on: bool) -> bool:
        self.fog_lamp_on = bool(on)
        self._refresh_visibility()
        return self.fog_lamp_on

    def _refresh_visibility(self) -> bool:
        # 缺陷 2：雾天默认不可见，但雾灯开启后必须立即恢复可见。
        self.vessel_visible = self.weather != WEATHER_FOG or self.fog_lamp_on
        return self.vessel_visible

    # ------------------------------------------------------------------ 事件

    def report_alarm(self, name: str) -> int:
        beacon = self._beacon(name)
        # 缺陷 3：同一航标同一夜班只计一次告警。
        if beacon.reported:
            raise AlreadyReportedError(f"{name} 的告警已在本班记录")
        beacon.reported = True
        beacon.damaged = True
        self.alarm_count += 1
        self.danger = True
        return self.alarm_count

    def request_passage(self, vessel: str) -> str:
        vessel = str(vessel)
        if not vessel:
            raise HarborError("船名不能为空")
        # 缺陷 4：航道锁被占用时拒绝后续船只。
        if self.channel_lock is not None:
            raise ChannelBusyError(f"航道锁被 {self.channel_lock} 占用")
        if not self.vessel_visible:
            raise HarborError("船只不可见，无法安全通过")
        self.channel_lock = vessel
        return self.channel_lock

    def release_channel(self) -> Optional[str]:
        """船只通过后释放航道锁；未加锁时返回 None。"""
        previous = self.channel_lock
        self.channel_lock = None
        return previous

    def repair_beacon(self, name: str, cost: int = 1) -> int:
        beacon = self._beacon(name)
        cost = int(cost)
        if cost <= 0:
            raise HarborError("维修消耗必须为正数")
        # 缺陷 5：材料不足必须在扣减前拒绝，状态不发生任何变化。
        if self.materials < cost:
            raise InsufficientMaterialsError(
                f"材料不足: 需要 {cost}, 剩余 {self.materials}"
            )
        self.materials -= cost
        beacon.damaged = False
        beacon.reported = False
        return self.materials

    def inspect_beacon(self, name: str, reading: float) -> float:
        beacon = self._beacon(name)
        reading = float(reading)
        beacon.history.append(reading)
        beacon.last_reading = reading
        return reading

    def undo_inspection(self, name: str) -> Optional[float]:
        beacon = self._beacon(name)
        if not beacon.history:
            raise NothingToUndoError(f"{name} 没有巡检记录可撤销")
        beacon.history.pop()
        # 缺陷 6：撤销后 last_reading 回退为上一条读数，而不是直接丢失。
        beacon.last_reading = beacon.history[-1] if beacon.history else None
        return beacon.last_reading

    def charge_battery(self, name: str, amount: int = 10) -> int:
        beacon = self._beacon(name)
        amount = int(amount)
        if amount < 0:
            raise HarborError("充电量不能为负")
        beacon.battery = beacon.battery + amount
        return beacon.battery

    # ------------------------------------------------------------------ 结算

    def _drainage_for(self, weather: str) -> int:
        if weather == WEATHER_FOG:
            return FOG_DRAIN
        if weather == WEATHER_STORM:
            return STORM_DRAIN
        return 0

    def settle_night(self) -> dict[str, Any]:
        """结算当前夜班。

        幂等：每个夜班只结算一次。重复调用返回与首次完全一致的结果，
        不会再次扣减电池，因此停电回合不会被重复结算。
        """
        if self.settled:
            return self.last_settlement()

        drainage = self._drainage_for(self.weather)
        # 缺陷 1：全夜电池扣减只在此处统一发生一次。
        for beacon in self.beacons.values():
            beacon.battery = beacon.battery - drainage

        self.blackout = any(
            beacon.battery < BLACKOUT_THRESHOLD for beacon in self.beacons.values()
        )

        damaged = [name for name, beacon in self.beacons.items() if beacon.damaged]
        # 缺陷 7：危险只能在结算点、天气晴好且无受损航标时解除。
        self.danger = bool(damaged) or self.weather != WEATHER_CLEAR

        result = {
            "night_number": self.night_number,
            "weather": self.weather,
            "drainage": drainage,
            "blackout": self.blackout,
            "alarms": self.alarm_count,
            "damaged": damaged,
            "danger": self.danger,
            "materials": self.materials,
        }
        self.log.append(f"night {self.night_number} settled: {result}")
        self.settled = True
        return deepcopy(result)

    def last_settlement(self) -> dict[str, Any]:
        """按当前状态重建结算结果；未真正结算时不产生副作用。"""
        return {
            "night_number": self.night_number,
            "weather": self.weather,
            "drainage": self._drainage_for(self.weather),
            "blackout": self.blackout,
            "alarms": self.alarm_count,
            "damaged": [name for name, beacon in self.beacons.items() if beacon.damaged],
            "danger": self.danger,
            "materials": self.materials,
        }

    def start_night(self) -> int:
        """进入下一个夜班：必要时先结算，随后夜班编号恰好增加一次。"""
        if not self.settled:
            self.settle_night()
        self.night_number += 1
        self.settled = False
        for beacon in self.beacons.values():
            beacon.reported = False
            beacon.battery = beacon.battery + NIGHT_RECHARGE
        self.release_channel()
        self.fog_lamp_on = False
        self._refresh_visibility()
        self.alarm_count = 0
        self.blackout = False
        return self.night_number

    # ------------------------------------------------------------------ 存档

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": SAVE_VERSION,
            "night_number": self.night_number,
            "materials": self.materials,
            "weather": self.weather,
            "fog_lamp_on": self.fog_lamp_on,
            "vessel_visible": self.vessel_visible,
            "channel_lock": self.channel_lock,
            "alarm_count": self.alarm_count,
            "danger": self.danger,
            "blackout": self.blackout,
            "settled": self.settled,
            "seed": self.seed,
            "beacons": [beacon.to_dict() for beacon in self.beacons.values()],
            "log": list(self.log),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NightWatch":
        if not isinstance(data, dict):
            raise HarborError("存档必须是 JSON 对象")
        beacon_data = data.get("beacons")
        if not isinstance(beacon_data, list) or not beacon_data:
            raise HarborError("存档缺少航标数据")

        watch = cls.__new__(cls)
        watch.beacons = {}
        for item in beacon_data:
            beacon = Beacon.from_dict(item)
            if beacon.name in watch.beacons:
                raise HarborError(f"存档中航标名称重复: {beacon.name}")
            watch.beacons[beacon.name] = beacon

        watch.materials = max(0, int(data.get("materials", 0)))
        # 缺陷 8：读档只恢复编号，绝不推进夜班。
        watch.night_number = max(1, int(data.get("night_number", 1)))
        weather = data.get("weather", WEATHER_CLEAR)
        if weather not in WEATHERS:
            raise HarborError(f"存档天气非法: {weather}")
        watch.weather = weather
        watch.fog_lamp_on = bool(data.get("fog_lamp_on", False))
        watch.vessel_visible = bool(
            data.get(
                "vessel_visible",
                weather != WEATHER_FOG or watch.fog_lamp_on,
            )
        )
        watch.channel_lock = data.get("channel_lock")
        watch.alarm_count = max(0, int(data.get("alarm_count", 0)))
        watch.danger = bool(data.get("danger", False))
        watch.blackout = bool(data.get("blackout", False))
        watch.settled = bool(data.get("settled", False))
        watch.log = [str(line) for line in data.get("log", [])]
        watch.seed = data.get("seed")
        watch._rng = random.Random(watch.seed)
        return watch


def save_game(watch: NightWatch, path: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(watch.to_dict(), handle, ensure_ascii=False, indent=2, sort_keys=True)


def load_game(path: str) -> NightWatch:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        raise
    except (json.JSONDecodeError, ValueError) as exc:
        raise HarborError(f"存档无法解析: {exc}") from exc
    return NightWatch.from_dict(data)
