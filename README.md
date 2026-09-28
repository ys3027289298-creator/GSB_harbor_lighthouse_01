# Harborlight 灯塔夜班值守

纯 Python 标准库实现，无第三方依赖。核心逻辑在 `harborlight/core.py`，
命令入口在 `harborlight/cli.py`，状态以 JSON 存档保存。

## 运行测试

```bash
python -m unittest -v
```

## 固定种子演示（离线、确定性）

```bash
python -m harborlight demo                 # 默认种子 20260929
python -m harborlight demo --seed 42
```

演示覆盖：雾天雾灯可见性、航道锁释放、重复告警去重、撤销巡检、
材料不足拒绝维修、风暴危险保持、晴好在结算点清除、存档读回夜班编号不变，
并验证每个夜班只结算一次。

## 值守命令

```bash
python -m harborlight --state save.json init [--beacons n,e] [--materials 3] [--seed 20260929]
python -m harborlight --state save.json status
python -m harborlight --state save.json weather [--roll]
python -m harborlight --state save.json foglamp --on|--off
python -m harborlight --state save.json alarm <beacon>
python -m harborlight --state save.json pass <vessel>
python -m harborlight --state save.json clear
python -m harborlight --state save.json repair <beacon> [--cost 1]
python -m harborlight --state save.json inspect <beacon> <reading>
python -m harborlight --state save.json undo <beacon>
python -m harborlight --state save.json settle   # 幂等，每夜只结算一次
python -m harborlight --state save.json next
```

Windows 下使用 `harborlight.cmd`，类 Unix 下使用 `./harborlight.sh`。

业务错误（非法命令、未知航标、重复告警、航道占用、材料不足等）
打印中文错误信息并以退出码 2 返回。
