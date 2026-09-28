"""红态缺陷回归测试与边界输入检查。

八个缺陷：
1. 停电回合电池重复扣除
2. 雾灯开启后船只仍显示不可见
3. 同一航标告警重复计数
4. 船只通过后航道锁未释放
5. 维修材料不足仍能完成
6. 撤销巡检丢失上次读数
7. 天气恢复提前清除危险
8. 读档后夜班编号增加两次
"""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from harborlight.cli import DEFAULT_SEED, main, run_demo
from harborlight.core import (
    AlreadyReportedError,
    BATTERY_MAX,
    BLACKOUT_THRESHOLD,
    ChannelBusyError,
    FOG_DRAIN,
    HarborError,
    InsufficientMaterialsError,
    NightWatch,
    NothingToUndoError,
    UnknownBeaconError,
    WEATHER_CLEAR,
    WEATHER_FOG,
    WEATHER_STORM,
    load_game,
    save_game,
)


class NightWatchRedTests(unittest.TestCase):
    def test_blackout_drains_battery_once(self):
        watch = NightWatch(seed=1)
        watch.beacons["north-tower"].battery = 24
        watch.set_weather(WEATHER_FOG)

        first = watch.settle_night()
        expected = 24 - FOG_DRAIN
        self.assertEqual(watch.beacons["north-tower"].battery, expected)
        self.assertTrue(first["blackout"])
        self.assertLess(expected, BLACKOUT_THRESHOLD)

        before = dict((name, beacon.battery) for name, beacon in watch.beacons.items())
        second = watch.settle_night()
        after = dict((name, beacon.battery) for name, beacon in watch.beacons.items())
        self.assertEqual(before, after)
        self.assertEqual(first, second)

    def test_fog_lamp_restores_visibility(self):
        watch = NightWatch(seed=1)
        watch.set_weather(WEATHER_FOG)
        self.assertFalse(watch.vessel_visible)

        watch.set_fog_lamp(True)
        self.assertTrue(watch.vessel_visible)
        watch.set_fog_lamp(False)
        self.assertFalse(watch.vessel_visible)

    def test_duplicate_alarm_counted_once(self):
        watch = NightWatch(seed=1)
        self.assertEqual(watch.report_alarm("north-tower"), 1)
        with self.assertRaises(AlreadyReportedError):
            watch.report_alarm("north-tower")
        self.assertEqual(watch.alarm_count, 1)
        self.assertEqual(watch.report_alarm("east-bell"), 2)

    def test_channel_lock_released_after_passage(self):
        watch = NightWatch(seed=1)
        watch.set_weather(WEATHER_FOG)
        watch.set_fog_lamp(True)

        self.assertEqual(watch.request_passage("MV A"), "MV A")
        with self.assertRaises(ChannelBusyError):
            watch.request_passage("MV B")
        self.assertEqual(watch.release_channel(), "MV A")
        self.assertIsNone(watch.channel_lock)
        self.assertEqual(watch.request_passage("MV B"), "MV B")

    def test_repair_blocked_without_materials(self):
        watch = NightWatch(materials=1, seed=1)
        watch.report_alarm("north-tower")
        self.assertEqual(watch.repair_beacon("north-tower"), 0)
        self.assertFalse(watch.beacons["north-tower"].damaged)

        watch.report_alarm("east-bell")
        with self.assertRaises(InsufficientMaterialsError):
            watch.repair_beacon("east-bell")
        self.assertEqual(watch.materials, 0)
        self.assertTrue(watch.beacons["east-bell"].damaged)

    def test_undo_inspection_keeps_previous_reading(self):
        watch = NightWatch(seed=1)
        watch.inspect_beacon("north-tower", 10.0)
        watch.inspect_beacon("north-tower", 20.0)
        self.assertEqual(watch.undo_inspection("north-tower"), 10.0)
        self.assertEqual(watch.beacons["north-tower"].last_reading, 10.0)
        self.assertIsNone(watch.undo_inspection("north-tower"))
        with self.assertRaises(NothingToUndoError):
            watch.undo_inspection("north-tower")

    def test_danger_clears_only_at_settlement_when_clear(self):
        watch = NightWatch(seed=1)
        watch.set_weather(WEATHER_STORM)
        watch.settle_night()
        self.assertTrue(watch.danger)

        watch.start_night()
        watch.set_weather(WEATHER_CLEAR)
        self.assertTrue(watch.danger, "天气刚恢复不能提前清除危险")
        watch.settle_night()
        self.assertFalse(watch.danger)

    def test_load_does_not_increment_night_twice(self):
        watch = NightWatch(seed=1)
        watch.roll_weather()
        watch.settle_night()
        watch.start_night()
        watch.settle_night()
        self.assertEqual(watch.night_number, 2)

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "save.json")
            save_game(watch, path)
            restored = load_game(path)
            self.assertEqual(restored.night_number, 2)
            restored2 = load_game(path)
            self.assertEqual(restored2.night_number, 2)


class NightNumberProgressionTests(unittest.TestCase):
    def test_each_night_settled_once(self):
        watch = NightWatch(seed=1)
        for expected in range(1, 5):
            self.assertEqual(watch.night_number, expected)
            result = watch.settle_night()
            self.assertEqual(result["night_number"], expected)
            watch.settle_night()
            watch.settle_night()
            self.assertEqual(watch.night_number, expected)
            watch.start_night()
        self.assertEqual(watch.night_number, 5)


class EdgeCaseTests(unittest.TestCase):
    def test_empty_data_defaults(self):
        with self.assertRaises(HarborError):
            NightWatch.from_dict({})
        with self.assertRaises(HarborError):
            NightWatch.from_dict({"beacons": []})
        with self.assertRaises(HarborError):
            NightWatch(beacon_names=[])

        minimal = NightWatch.from_dict(
            {"beacons": [{"name": "solo"}], "night_number": 7, "materials": 2}
        )
        self.assertEqual(minimal.night_number, 7)
        self.assertEqual(minimal.materials, 2)
        self.assertEqual(minimal.weather, WEATHER_CLEAR)
        self.assertEqual(minimal.beacons["solo"].battery, BATTERY_MAX)

    def test_battery_capacity_boundaries(self):
        watch = NightWatch(seed=1)
        beacon = watch.beacons["north-tower"]

        beacon.battery = 105
        self.assertEqual(beacon.battery, BATTERY_MAX)
        beacon.battery = -9
        self.assertEqual(beacon.battery, 0)

        beacon.battery = 98
        self.assertEqual(watch.charge_battery("north-tower", 10), BATTERY_MAX)
        beacon.battery = 0
        self.assertEqual(watch.charge_battery("north-tower", 0), 0)

        watch.set_weather(WEATHER_STORM)
        watch.settle_night()
        self.assertEqual(beacon.battery, 0)

    def test_duplicate_inputs_rejected(self):
        watch = NightWatch(materials=1, seed=1)
        watch.report_alarm("north-tower")
        with self.assertRaises(AlreadyReportedError):
            watch.report_alarm("north-tower")

        watch.request_passage("MV A")
        with self.assertRaises(ChannelBusyError):
            watch.request_passage("MV A")

        with self.assertRaises(UnknownBeaconError):
            watch.inspect_beacon("ghost-buoy", 1.0)

        with self.assertRaises(HarborError):
            watch.set_weather("hurricane")
        with self.assertRaises(HarborError):
            NightWatch(beacon_names=["a", "a"])

    def test_save_roundtrip_preserves_state(self):
        watch = NightWatch(materials=2, seed=99)
        watch.roll_weather()
        watch.set_fog_lamp(True)
        watch.report_alarm("east-bell")
        watch.inspect_beacon("east-bell", 3.14)
        watch.settle_night()

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nested", "save.json")
            with self.assertRaises(FileNotFoundError):
                load_game(path)
        payload = json.loads(json.dumps(watch.to_dict()))
        restored = NightWatch.from_dict(payload)
        self.assertEqual(restored.to_dict(), watch.to_dict())

    def test_corrupt_save_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            with self.assertRaises(HarborError):
                load_game(path)


class CliTests(unittest.TestCase):
    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_illegal_command_and_args(self):
        code, _, err = self.run_cli("--state", "/tmp/hl-unused.json", "explode")
        self.assertNotEqual(code, 0)
        self.assertTrue(err)

        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "save.json")
            code, _, _ = self.run_cli("--state", state, "init")
            self.assertEqual(code, 0)

            code, _, err = self.run_cli("--state", state, "alarm", "ghost")
            self.assertEqual(code, 2)
            self.assertIn("未知航标", err)

            code, _, _ = self.run_cli("--state", state, "alarm", "north-tower")
            self.assertEqual(code, 0)
            code, _, err = self.run_cli("--state", state, "alarm", "north-tower")
            self.assertEqual(code, 2)
            self.assertIn("已在本班记录", err)

    def test_cli_full_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "save.json")
            self.assertEqual(self.run_cli("--state", state, "init", "--seed", "7")[0], 0)
            self.assertEqual(self.run_cli("--state", state, "weather", "--roll")[0], 0)
            self.assertEqual(self.run_cli("--state", state, "foglamp", "--on")[0], 0)
            self.assertEqual(self.run_cli("--state", state, "pass", "MV Cli")[0], 0)
            self.assertEqual(self.run_cli("--state", state, "clear")[0], 0)
            self.assertEqual(self.run_cli("--state", state, "inspect", "north-tower", "5.5")[0], 0)
            self.assertEqual(self.run_cli("--state", state, "undo", "north-tower")[0], 0)
            self.assertEqual(self.run_cli("--state", state, "settle")[0], 0)
            restored = load_game(state)
            self.assertTrue(restored.settled)
            self.assertIsNone(restored.channel_lock)

    def test_fixed_seed_demo_deterministic(self):
        first = run_demo(DEFAULT_SEED)
        second = run_demo(DEFAULT_SEED)
        self.assertEqual(first, second)

        labels = [event["step"] for event in first]
        for required in (
            "fog-without-lamp",
            "fog-lamp-on",
            "passage-locked",
            "passage-cleared",
            "duplicate-alarm-rejected",
            "inspection-undo",
            "settle-once",
            "repair-blocked",
            "storm-still-danger",
            "clear-before-settle",
            "clear-after-settle",
            "save-roundtrip",
        ):
            self.assertIn(required, labels)

        events = {event["step"]: event for event in first}
        self.assertFalse(events["fog-without-lamp"]["vessel_visible"])
        self.assertTrue(events["fog-lamp-on"]["vessel_visible"])
        self.assertIsNone(events["passage-cleared"]["channel_lock"])
        self.assertEqual(events["duplicate-alarm-rejected"]["alarm_count"], 1)
        self.assertEqual(events["inspection-undo"]["last_reading"], 42.5)
        self.assertTrue(events["settle-once"]["second_same"])
        self.assertFalse(events["repair-blocked"]["damaged"])
        self.assertTrue(events["storm-still-danger"]["danger"])
        self.assertTrue(events["clear-before-settle"]["danger"])
        self.assertFalse(events["clear-after-settle"]["danger"])
        self.assertTrue(events["save-roundtrip"]["same"])
        self.assertEqual(events["save-roundtrip"]["night"], 3)


if __name__ == "__main__":
    unittest.main()
