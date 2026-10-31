# -*- coding: utf-8 -*-
"""健壮性批次回归：start 前预检、选表确定性、审计钩子。

这些用例都离线运行（FakeTransport），不联网。
"""
import datetime
import json
import shutil

import pytest

import main as M
from conftest import FIXTURES
from test_main_phase_a import CFG, _home, make_client

TASK_FIXTURE = FIXTURES + "/tasks/tasklist_0.json"


def _entry(**over):
    entry = {
        "id": 900001, "schoolId": "197", "raType": "T3", "raRunArea": "LY",
        "raDislikes": 0, "raSingleMileageMin": 2.0, "raSingleMileageMax": 5.0,
        "raCadenceMin": 35, "raCadenceMax": 350, "runFaceStatus": "N",
        "points": "112.4,34.6",
    }
    entry.update(over)
    return entry


def _yun(entry=None, outer=None, skip_preflight=False):
    M.set_args(CFG)
    _, client = make_client()
    home = {"code": 200, "data": {"cralist": [entry or _entry()], **(outer or {})}}
    return M.Yun_For_New(auto_generate_task=False, client=client, home_info=home,
                         skip_preflight=skip_preflight)


# ---------------------------------------------------------------- start 前预检
def test_preflight_passes_when_window_open():
    now = datetime.datetime(2026, 10, 31, 12, 0)  # 周六
    yun = _yun(_entry(raStartTime="2026-09-23", raEndTime="2027-01-08",
                      dayStartTime="06:00", dayEndTime="22:00",
                      raWeekTime="星期一,星期二,星期三,星期四,星期五,星期六,星期日"))
    yun.preflight_check(now=now)  # 不抛即通过


def test_preflight_blocks_expired_task():
    yun = _yun(_entry(raStartTime="2020-01-01", raEndTime="2020-06-01"))
    with pytest.raises(ValueError, match="任务已结束"):
        yun.preflight_check(now=datetime.datetime(2026, 10, 31, 12, 0))


def test_preflight_blocks_outside_daily_window():
    yun = _yun(_entry(raStartTime="2020-01-01", raEndTime="2099-01-01",
                      dayStartTime="06:00", dayEndTime="22:00"))
    with pytest.raises(ValueError, match="可跑时段"):
        yun.preflight_check(now=datetime.datetime(2026, 10, 31, 3, 0))


def test_preflight_blocks_closed_weekday():
    yun = _yun(_entry(raStartTime="2020-01-01", raEndTime="2099-01-01",
                      raWeekTime="星期一,星期二,星期三,星期四,星期五"))
    with pytest.raises(ValueError, match="不在任务开放日"):
        yun.preflight_check(now=datetime.datetime(2026, 10, 31, 12, 0))  # 周六


def test_preflight_blocks_full_quota():
    yun = _yun(_entry(raStartTime="2020-01-01", raEndTime="2099-01-01"),
               outer={"qualifiedCount": 30, "raTargetNum": 30})
    with pytest.raises(ValueError, match="额度已满"):
        yun.preflight_check(now=datetime.datetime(2026, 10, 31, 12, 0))


def test_preflight_missing_fields_are_skipped():
    yun = _yun()  # 任务体没有任何窗口字段：不猜、直接放行
    yun.preflight_check(now=datetime.datetime(2026, 10, 31, 12, 0))


def test_skip_preflight_bypasses_checks():
    yun = _yun(_entry(raStartTime="2020-01-01", raEndTime="2020-06-01"),
               skip_preflight=True)
    yun.preflight_check(now=datetime.datetime(2026, 10, 31, 12, 0))


# ---------------------------------------------------------------- 选表确定性
def test_pick_task_ignores_non_task_json(tmp_path):
    shutil.copy(TASK_FIXTURE, tmp_path / "tasklist_0.json")
    (tmp_path / "route.json").write_text(json.dumps({"source_json": "x"}), encoding="utf-8")
    (tmp_path / "fence.geojson").write_text("{}", encoding="utf-8")
    path, task_map = M.Yun_For_New.pick_task(str(tmp_path), random_choose=True)
    assert path.endswith("tasklist_0.json")
    assert task_map["data"]["pointsList"]


def test_pick_task_explicit_file_and_rejects_non_task(tmp_path):
    shutil.copy(TASK_FIXTURE, tmp_path / "a.json")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"data": {"pointsList": []}}), encoding="utf-8")
    path, _ = M.Yun_For_New.pick_task(str(tmp_path), task_file="a.json")
    assert path.endswith("a.json")
    with pytest.raises(ValueError, match="结构不对|pointsList"):
        M.Yun_For_New.pick_task(str(tmp_path), task_file="bad.json")


def test_pick_task_missing_directory(tmp_path):
    with pytest.raises(FileNotFoundError):
        M.Yun_For_New.pick_task(str(tmp_path / "nope"), random_choose=True)


def test_validate_tasklist_rejects_bad_summary(tmp_path):
    task = json.loads(open(TASK_FIXTURE, encoding="utf-8-sig").read())
    task["data"]["recordMileage"] = 0
    with pytest.raises(ValueError, match="不是正数"):
        M.Yun_For_New._validate_tasklist(task, "x")


# ---------------------------------------------------------------- 审计钩子
def test_audit_hook_receives_each_request():
    M.set_args(CFG)
    _, client = make_client()
    seen = []
    client.audit = lambda router, obj, error, elapsed: seen.append(
        (router, (obj or {}).get("code"), error))
    client.post_json("/run/getHomeRunInfo", "")
    assert seen and seen[0][0] == "/run/getHomeRunInfo" and seen[0][1] == 200


def test_audit_hook_failure_does_not_break_request():
    M.set_args(CFG)
    _, client = make_client()

    def boom(*_args, **_kwargs):
        raise RuntimeError("audit down")

    client.audit = boom
    obj = client.post_json("/run/getHomeRunInfo", "")
    assert obj["code"] == 200


def test_setup_logging_writes_audit_file(tmp_path):
    M.set_args(CFG)
    target = M.setup_logging(str(tmp_path / "run.log"))
    _, client = make_client()
    M.attach_audit(client)
    client.post_json("/run/getHomeRunInfo", "")
    text = open(target, encoding="utf-8").read()
    assert "/run/getHomeRunInfo" in text


# ---------------------------------------------------------------- 学校围栏
def test_generated_route_outside_school_fence_rejected():
    yun = _yun(_entry(fence="112.0,34.0|112.0,35.0|113.0,35.0|113.0,34.0"))
    task = {"metadata": {"coordinate_system": "GCJ-02"},
            "data": {"pointsList": [{"point": "120.000000,40.000000"}],
                     "recordMileage": 2.5, "recodeCadence": 170}}
    # 120,40 不在 112~113 × 34~35 的围栏内 → 必须拒绝
    with pytest.raises(ValueError, match="围栏"):
        yun.validate_generated_route(task)


def test_generated_route_inside_school_fence_passes():
    yun = _yun(_entry(fence="112.0,34.0|112.0,35.0|113.0,35.0|113.0,34.0"))
    task = {"metadata": {"coordinate_system": "GCJ-02"},
            "data": {"pointsList": [{"point": "112.500000,34.500000"}],
                     "recordMileage": 2.5, "recodeCadence": 170}}
    yun.validate_generated_route(task)


def test_parse_fence_tolerates_garbage():
    assert M.Yun_For_New._parse_fence("") is None
    assert M.Yun_For_New._parse_fence("bad|also bad") is None
    assert M.Yun_For_New._parse_fence("112.4,34.6|112.5,34.6|112.5,34.7") is not None


# ---------------------------------------------------------------- 时钟漂移
def test_clock_drift_is_detected():
    yun = _yun()
    clock = {"wall": 1700000000, "mono": 100.0}
    yun.client.now = lambda: int(clock["wall"])
    yun.client.mono = lambda: clock["mono"]
    yun._mark_clock()
    clock["wall"] += 60      # 墙钟跳 60s，单调钟只走 1s
    clock["mono"] += 1
    with pytest.raises(ValueError, match="时钟"):
        yun.check_clock_drift()


def test_clock_drift_tolerates_normal_progress():
    yun = _yun()
    clock = {"wall": 1700000000, "mono": 100.0}
    yun.client.now = lambda: int(clock["wall"])
    yun.client.mono = lambda: clock["mono"]
    yun._mark_clock()
    clock["wall"] += 30
    clock["mono"] += 30
    yun.check_clock_drift()


# ---------------------------------------------------------------- 截断一致性
def test_summary_rejects_mileage_over_cap():
    yun = _yun(_entry(raSingleMileageMax=3.0))
    yun.task_map = {"data": {"pointsList": [], "manageList": []}}
    points = [{"point": "112.4,34.6", "runMileage": 3500.0, "runTime": 100, "runStep": 200}]
    with pytest.raises(ValueError, match="超过任务上限"):
        yun._update_task_summary(points, 100)


# ---------------------------------------------------------------- 配置缺项
def test_missing_conf_keys_are_listed(tmp_path):
    p = tmp_path / "bad.ini"
    p.write_text("[Yun]\nschool_host=x\n[User]\ntoken=\n", encoding="utf-8")
    with pytest.raises(ValueError, match="缺少配置项"):
        M.set_args(str(p))


# ---------------------------------------------------------------- 重复上传提醒
def test_repeat_upload_warns_second_time(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(M.Yun_For_New, "UPLOAD_STATE", str(tmp_path / "state.json"))
    yun = _yun()
    yun._warn_repeat_upload("fingerprint-x")
    capsys.readouterr()
    yun._warn_repeat_upload("fingerprint-x")
    assert "重复提醒" in capsys.readouterr().out


# ---------------------------------------------------------------- 高德失败可诊断
def test_amap_connection_error_is_reported(monkeypatch):
    yun = _yun()
    yun.my_point = "112.4,34.6"
    yun.task_list = []
    yun.server_points = ["112.5,34.6"]

    def boom(*_args, **_kwargs):
        raise M.requests.ConnectionError("Failed to establish a new connection")

    monkeypatch.setattr(M.requests, "get", boom)
    monkeypatch.setenv("YUN_AMAP_DELAY", "0")
    with pytest.raises(RuntimeError, match="高德"):
        yun.add_task("112.5,34.6")
