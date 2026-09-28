"""Harborlight 灯塔夜班值守核心逻辑。

纯标准库实现。每个夜班通过 :meth:`NightWatch.settle_night` 恰好结算一次，
状态使用 JSON 存档，随机天气支持固定种子以便可重复演示。
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

SAVE_VERSION = 1


class HarborError(Exception):
    """所有值守操作错误的基类。"""


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


class Beacon:
    """单个航标（灯塔/雾号浮标）的运行状态。"""

    def __init__(self, name: str, battery: int = BATTERY_MAX, damaged: bool = False):
        self.name = str(name)
        self._battery = _clamp_battery(battery)
        self.damaged = bool(damaged)
        self.last_reading: Optional[float] = None
        self.reported: bool = False
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
        beacon = cls.__new__(cls)
        beacon.name = str(data["name"])
        beacon._battery = _clamp_battery(int(data.get("battery", BATTERY_MAX)))
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

        self.materials = int(materials)
        if self.materials < 0:
            raise HarborError("维修材料不能为负")
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
        return weather

    def roll_weather(self) -> str:
        """按固定权重在三种天气间推进（受种子控制）。"""
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
        # 雾天只有雾灯开启时船只才可见；雾灯开启后不应再显示“不可见”。
        self.vessel_visible = self.weather != WEATHER_FOG or self.fog_lamp_on
        return self.vessel_visible

    # ------------------------------------------------------------------ 事件

    def report_alarm(self, name: str) -> int:
        beacon = self._beacon(name)
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
        if self.channel_lock is not None:
            raise ChannelBusyError(f"航道锁被 {self.channel_lock} 占用")
        if not self.vessel_visible:
            raise HarborError("船只不可见，无法安全通过")
        self.channel_lock = vessel
        return self.channel_lock

    def release_channel(self) -> Optional[str]:
        previous = self.channel_lock
        self.channel_lock = None
        return previous

    def repair_beacon(self, name: str, cost: int = 1) -> int:
        beacon = self._beacon(name)
        cost = int(cost)
        if cost <= 0:
            raise HarborError("维修消耗必须为正数")
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
        beacon.last_reading = beacon.history[-1] if beacon.history else None
        return beacon.last_reading

    def charge_battery(self, name: str, amount: int = 10) -> int:
        beacon = self._beacon(name)
        amount = int(amount)
        if amount < 0:
            raise HarborError("充电量不能为负")
        beacon.battery = _clamp_battery(beacon.battery + amount)
        return beacon.battery

    # ------------------------------------------------------------------ 结算

    def settle_night(self) -> dict[str, Any]:
        """结算当前夜班。重复调用是幂等的：每个夜班只结算一次。"""
        if self.settled:
            return self.last_settlement()

        drainage = 0
        if self.weather == WEATHER_FOG:
            drainage = FOG_DRAIN
        elif self.weather == WEATHER_STORM:
            drainage = STORM_DRAIN

        for beacon in self.beacons.values():
            beacon.battery = _clamp_battery(beacon.battery - drainage)

        # 停电回合仅扣一次电池（已包含在上方统一扣减中），只记录停电状态。
        self.blackout = any(beacon.battery < BLACKOUT_THRESHOLD for beacon in self.beacons.values())

        damaged = [name for name, beacon in self.beacons.items() if beacon.damaged]
        # 危险状态只能在天气恢复为晴好且无受损航标时，于结算点清除。
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
        return {
            "night_number": self.night_number,
            "weather": self.weather,
            "drainage": {WEATHER_FOG: FOG_DRAIN, WEATHER_STORM: STORM_DRAIN}.get(self.weather, 0),
            "blackout": self.blackout,
            "alarms": self.alarm_count,
            "damaged": [name for name, beacon in self.beacons.items() if beacon.damaged],
            "danger": self.danger,
            "materials": self.materials,
        }

    def start_night(self) -> int:
        """进入下一个夜班：完成结算后夜班编号只增加一次。"""
        if not self.settled:
            self.settle_night()
        self.night_number += 1
        self.settled = False
        for beacon in self.beacons.values():
            beacon.reported = False
            # 电池在两班之间回充，上限 100，不越界。
            beacon.battery = _clamp_battery(beacon.battery + 5)
        self.release_channel()
        self.fog_lamp_on = False
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
        watch.night_number = max(1, int(data.get("night_number", 1)))
        weather = data.get("weather", WEATHER_CLEAR)
        if weather not in WEATHERS:
            raise HarborError(f"存档天气非法: {weather}")
        watch.weather = weather
        watch.fog_lamp_on = bool(data.get("fog_lamp_on", False))
        watch.vessel_visible = bool(
            data.get("vessel_visible", weather != WEATHER_FOG or watch.fog_lamp_on)
        )
        watch.channel_lock = data.get("channel_lock")
        watch.alarm_count = max(0, int(data.get("alarm_count", 0)))
        watch.danger = bool(data.get("danger", False))
        watch.blackout = bool(data.get("blackout", False))
        watch.settled = bool(data.get("settled", False))
        watch.log = [str(line) for line in data.get("log", [])]
        watch.seed = data.get("seed")
        # 重建随机流；读档本身绝不改变夜班编号。
        watch._rng = random.Random(watch.seed)
        return watch


def _clamp_battery(value: Any) -> int:
    battery = int(value)
    if battery < BATTERY_MIN:
        return BATTERY_MIN
    if battery > BATTERY_MAX:
        return BATTERY_MAX
    return battery


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
