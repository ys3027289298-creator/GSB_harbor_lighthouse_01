"""Harborlight 命令行入口（纯标准库 argparse）。

业务错误以中文信息打印到 stderr 并返回退出码 2；
存档缺失同样返回 2；参数/命令错误由 argparse 处理（退出码 2）。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional, Sequence

from .core import (
    HarborError,
    NightWatch,
    WEATHER_FOG,
    load_game,
    save_game,
)

DEFAULT_STATE = "harborlight_save.json"
DEFAULT_SEED = 20260929


def _print(data: object) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def _store(path: str, watch: NightWatch, payload: object) -> int:
    save_game(watch, path)
    _print(payload)
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    names = [part.strip() for part in args.beacons.split(",") if part.strip()]
    watch = NightWatch(beacon_names=names, materials=args.materials, seed=args.seed)
    return _store(args.state, watch, {"ok": True, "night": watch.night_number})


def cmd_status(args: argparse.Namespace) -> int:
    _print(load_game(args.state).to_dict())
    return 0


def cmd_weather(args: argparse.Namespace) -> int:
    watch = load_game(args.state)
    weather = watch.roll_weather() if args.roll else watch.weather
    return _store(
        args.state,
        watch,
        {"weather": weather, "vessel_visible": watch.vessel_visible},
    )


def cmd_foglamp(args: argparse.Namespace) -> int:
    watch = load_game(args.state)
    watch.set_fog_lamp(args.on)
    return _store(
        args.state,
        watch,
        {"fog_lamp_on": watch.fog_lamp_on, "vessel_visible": watch.vessel_visible},
    )


def cmd_alarm(args: argparse.Namespace) -> int:
    watch = load_game(args.state)
    total = watch.report_alarm(args.beacon)
    return _store(args.state, watch, {"alarm_count": total})


def cmd_pass(args: argparse.Namespace) -> int:
    watch = load_game(args.state)
    lock = watch.request_passage(args.vessel)
    return _store(args.state, watch, {"channel_lock": lock})


def cmd_clear(args: argparse.Namespace) -> int:
    watch = load_game(args.state)
    released = watch.release_channel()
    return _store(args.state, watch, {"released": released, "channel_lock": watch.channel_lock})


def cmd_repair(args: argparse.Namespace) -> int:
    watch = load_game(args.state)
    remaining = watch.repair_beacon(args.beacon, args.cost)
    return _store(args.state, watch, {"materials": remaining})


def cmd_inspect(args: argparse.Namespace) -> int:
    watch = load_game(args.state)
    reading = watch.inspect_beacon(args.beacon, args.reading)
    return _store(args.state, watch, {"last_reading": reading})


def cmd_undo(args: argparse.Namespace) -> int:
    watch = load_game(args.state)
    reading = watch.undo_inspection(args.beacon)
    return _store(args.state, watch, {"last_reading": reading})


def cmd_settle(args: argparse.Namespace) -> int:
    watch = load_game(args.state)
    result = watch.settle_night()
    return _store(args.state, watch, result)


def cmd_next(args: argparse.Namespace) -> int:
    watch = load_game(args.state)
    watch.settle_night()
    number = watch.start_night()
    weather = watch.roll_weather()
    return _store(args.state, watch, {"night": number, "weather": weather})


def run_demo(seed: int = DEFAULT_SEED) -> list[dict[str, object]]:
    """运行一次确定性固定种子演示并返回关键事件（不读写磁盘）。"""
    watch = NightWatch(beacon_names=["north-tower", "east-bell"], materials=3, seed=seed)
    events: list[dict[str, object]] = []

    def record(label: str, payload: dict[str, object]) -> None:
        events.append({"step": label, **payload})

    # 第 1 夜：推进到雾天，雾灯恢复可见，船只通过后释放航道锁。
    while watch.weather != WEATHER_FOG:
        first_weather = watch.roll_weather()
    watch.set_fog_lamp(False)
    record("fog-without-lamp", {"weather": first_weather, "vessel_visible": watch.vessel_visible})
    watch.set_fog_lamp(True)
    record("fog-lamp-on", {"vessel_visible": watch.vessel_visible})
    watch.request_passage("MV Lighthouse")
    record("passage-locked", {"channel_lock": watch.channel_lock})
    watch.release_channel()
    record("passage-cleared", {"channel_lock": watch.channel_lock})

    watch.report_alarm("north-tower")
    try:
        watch.report_alarm("north-tower")
    except HarborError:
        record("duplicate-alarm-rejected", {"alarm_count": watch.alarm_count})

    watch.inspect_beacon("north-tower", 42.5)
    watch.inspect_beacon("north-tower", 43.0)
    watch.undo_inspection("north-tower")
    record(
        "inspection-undo",
        {"last_reading": watch.beacons["north-tower"].last_reading},
    )

    settled_once = watch.settle_night()
    batteries_after = {name: beacon.battery for name, beacon in watch.beacons.items()}
    settled_again = watch.settle_night()
    record(
        "settle-once",
        {
            "first": settled_once,
            "second_same": settled_once == settled_again,
            "batteries": batteries_after,
        },
    )

    # 第 2 夜：风暴天气下危险保持，材料不足维修被拒。
    watch.start_night()
    watch.set_weather("storm")
    watch.repair_beacon("north-tower")
    try:
        watch.repair_beacon("north-tower", cost=99)
    except HarborError:
        record(
            "repair-blocked",
            {
                "materials": watch.materials,
                "damaged": watch.beacons["north-tower"].damaged,
            },
        )
    record("storm-still-danger", {"danger": watch.danger, "weather": watch.weather})
    watch.settle_night()

    # 第 3 夜：转晴后危险只能在结算点清除；读档不改变夜班编号。
    watch.start_night()
    watch.set_weather("clear")
    record("clear-before-settle", {"danger": watch.danger, "night": watch.night_number})
    watch.settle_night()
    record("clear-after-settle", {"danger": watch.danger, "night": watch.night_number})

    restored = NightWatch.from_dict(json.loads(json.dumps(watch.to_dict())))
    record(
        "save-roundtrip",
        {"night": restored.night_number, "same": restored.night_number == watch.night_number},
    )

    return events


def cmd_demo(args: argparse.Namespace) -> int:
    _print({"seed": args.seed, "events": run_demo(args.seed)})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harborlight", description="灯塔夜班值守工具")
    parser.add_argument("--state", default=DEFAULT_STATE, help="JSON 存档路径")
    sub = parser.add_subparsers(dest="command")
    sub.required = True

    init_parser = sub.add_parser("init", help="初始化新夜班")
    init_parser.add_argument("--beacons", default="north-tower,east-bell")
    init_parser.add_argument("--materials", type=int, default=3)
    init_parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    init_parser.set_defaults(func=cmd_init)

    status_parser = sub.add_parser("status", help="查看状态")
    status_parser.set_defaults(func=cmd_status)

    weather_parser = sub.add_parser("weather", help="查看或推进天气")
    weather_parser.add_argument("--roll", action="store_true", help="按种子随机推进天气")
    weather_parser.set_defaults(func=cmd_weather)

    lamp_parser = sub.add_parser("foglamp", help="开关雾灯")
    lamp_group = lamp_parser.add_mutually_exclusive_group(required=True)
    lamp_group.add_argument("--on", dest="on", action="store_true")
    lamp_group.add_argument("--off", dest="on", action="store_false")
    lamp_parser.set_defaults(func=cmd_foglamp)

    alarm_parser = sub.add_parser("alarm", help="上报告警")
    alarm_parser.add_argument("beacon")
    alarm_parser.set_defaults(func=cmd_alarm)

    pass_parser = sub.add_parser("pass", help="船只申请通过")
    pass_parser.add_argument("vessel")
    pass_parser.set_defaults(func=cmd_pass)

    clear_parser = sub.add_parser("clear", help="释放航道锁")
    clear_parser.set_defaults(func=cmd_clear)

    repair_parser = sub.add_parser("repair", help="维修航标")
    repair_parser.add_argument("beacon")
    repair_parser.add_argument("--cost", type=int, default=1)
    repair_parser.set_defaults(func=cmd_repair)

    inspect_parser = sub.add_parser("inspect", help="记录巡检读数")
    inspect_parser.add_argument("beacon")
    inspect_parser.add_argument("reading", type=float)
    inspect_parser.set_defaults(func=cmd_inspect)

    undo_parser = sub.add_parser("undo", help="撤销巡检")
    undo_parser.add_argument("beacon")
    undo_parser.set_defaults(func=cmd_undo)

    settle_parser = sub.add_parser("settle", help="结算夜班（幂等）")
    settle_parser.set_defaults(func=cmd_settle)

    next_parser = sub.add_parser("next", help="进入下一个夜班")
    next_parser.set_defaults(func=cmd_next)

    demo_parser = sub.add_parser("demo", help="固定种子演示")
    demo_parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    demo_parser.set_defaults(func=cmd_demo)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 2)
    try:
        return int(args.func(args) or 0)
    except HarborError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"错误: 找不到存档 {exc.filename}（请先运行 init）", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
