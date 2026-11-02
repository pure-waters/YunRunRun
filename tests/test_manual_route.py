# -*- coding: utf-8 -*-
"""manual 引擎（手动打点）与面板手动打点流程的离线测试。

锁定的是这次新增的契约，不改动 deform 的既有基准：
  * 生成路线**逐字经过**每个打点（锚点原值，不是"附近"）；
  * 打点之间按 5~7m 线性补点，抖动在锚点处归零、单步不折返；
  * 里程/时间/步数自洽（时间严格递增、配速回到设定值）；
  * 打卡点只有"路线真的含该顶点"才标 Y，否则 N（不虚构踩点）；
  * 面板：预览与审核同源（sha256 校验）、不覆盖同名文件、文件名白名单、
    落盘后的 cfg 能被 --route-config 原样复算，派生的打表任务表能过 main 的校验。
"""
import json
import os

import pytest

import main as M
import web
import yun_route as R

ANCHORS = [[112.417622, 34.612324], [112.420000, 34.612324],
           [112.420000, 34.615000], [112.417622, 34.615000], [112.417622, 34.612324]]


def _cfg(**over):
    cfg = {"engine": "manual", "coordinate_system": "GCJ-02", "anchors": ANCHORS,
           "seed": 7, "pace_min_km": 5.0, "cadence_spm": 174.0}
    cfg.update(over)
    return cfg


def _points(task):
    return [tuple(map(float, p["point"].split(","))) for p in task["data"]["pointsList"]]


# ------------------------------------------------------------------ 几何契约
def test_manual_passes_near_every_anchor():
    task = R.generate_cfg(_cfg(), None)          # 默认容差 5m：经过打点附近即可
    pts = _points(task)
    for a in ANCHORS:
        assert min(R.distance(a, p) for p in pts) <= 5.0
    assert task["metadata"]["anchor_tolerance_m"] == 5.0
    assert 0.0 < task["metadata"]["anchor_max_deviation_m"] <= 5.0


def test_tolerance_zero_means_exact_pass_through():
    task = R.generate_cfg(_cfg(anchor_tolerance_m=0.0), None)
    for a in ANCHORS:
        assert min(R.distance(a, p) for p in _points(task)) == 0.0
    assert task["metadata"]["anchor_max_deviation_m"] == 0.0


def test_manual_spacing_timeline_and_summary_are_consistent():
    task = R.generate_cfg(_cfg(), None)
    data = task["data"]
    rows = data["pointsList"]
    miles = [p["runMileage"] for p in rows]
    times = [p["runTime"] for p in rows]
    steps = [p["runStep"] for p in rows]
    assert all(b >= a for a, b in zip(miles, miles[1:]))
    assert all(b > a for a, b in zip(times, times[1:]))       # 整数秒严格递增
    assert all(b >= a for a, b in zip(steps, steps[1:]))
    mean_step = (miles[-1]-miles[0])/(len(rows)-1)
    assert 5.0 <= mean_step <= 7.5                            # 5~7m 线性补点（抖动会略微拉长）
    assert abs(data["recodePace"]-5.0) < 0.02
    assert abs(data["recodeCadence"]-174.0) < 0.5
    assert data["duration"] == times[-1]
    assert rows[0]["runMileage"] == 0.0 and rows[0]["runTime"] == 0
    assert rows[0]["speed"] == '0.0'
    assert all(1.0 <= float(p["speed"]) <= 900.0 for p in rows[1:])


def test_zero_jitter_stays_on_the_straight_line():
    rng = [[112.4170, 34.6120], [112.4190, 34.6120]]
    pts = R.manual_geometry(rng, seed=3,
                            profile={"jitter_sigma_m": 0.0, "drift_sigma_m": 0.0})
    spread = max(p[1] for p in pts)-min(p[1] for p in pts)
    assert spread < 1e-7
    assert pts[0] == (112.4170, 34.6120) and pts[-1] == (112.4190, 34.6120)


def test_jitter_moves_points_off_the_line_but_stays_near_anchors():
    rng = [[112.4170, 34.6120], [112.4190, 34.6120]]
    pts = R.manual_geometry(rng, seed=3)
    spread = max(abs(p[1]-34.612) for p in pts)
    assert 0.0 < spread < 20.0/111320.0*3        # 幅度在米级，不是"跑到另一条街"
    assert R.distance(pts[0], rng[0]) <= 5.0     # 端点也在容差内
    assert R.distance(pts[-1], rng[-1]) <= 5.0


def test_seed_is_reproducible_and_changes_geometry():
    assert R.manual_geometry(ANCHORS, seed=11) == R.manual_geometry(ANCHORS, seed=11)
    assert R.manual_geometry(ANCHORS, seed=11) != R.manual_geometry(ANCHORS, seed=12)


def test_short_segment_does_not_explode():
    """两个打点只隔 8m 时，抖动必须被单步限幅压住，不能来回折返。"""
    rng = [[112.417622, 34.612324], [112.417700, 34.612324]]
    pts = R.manual_geometry(rng, seed=5)
    assert len(pts) >= 2
    assert R.distance(pts[0], rng[0]) <= 5.0
    lateral = max(abs(p[1]-34.612324) for p in pts)
    assert lateral <= 8.0/111320.0


# ------------------------------------------------------------------ 输入校验
def test_manual_requires_valid_anchors():
    with pytest.raises(ValueError, match="至少需要"):
        R.generate_cfg(_cfg(anchors=[[112.4, 34.6]]), None)
    with pytest.raises(ValueError, match="经纬度越界"):
        R.generate_cfg(_cfg(anchors=[[200.0, 34.6], [112.4, 34.6]]), None)
    with pytest.raises(ValueError, match="coordinate_system"):
        R.generate_cfg(_cfg(coordinate_system="WGS-84"), None)
    with pytest.raises(ValueError, match="至少需要"):
        R.generate_cfg(_cfg(anchors=[[112.4, 34.6], [112.4, 34.6]]), None)   # 去重后不足


def test_manual_rejects_unknown_and_legacy_keys():
    with pytest.raises(ValueError, match="未知的 manual 配置项"):
        R.generate_cfg(_cfg(source_json="x.json"), None)
    with pytest.raises(ValueError, match="V4"):
        R.generate_cfg(_cfg(max_offset_m=5), None)


def test_distance_m_must_match_geometry():
    length = R.generate_cfg(_cfg(), None)["data"]["recordMileage"]*1000.0
    R.generate_cfg(_cfg(distance_m=round(length)), None)
    with pytest.raises(ValueError, match="distance_m"):
        R.generate_cfg(_cfg(distance_m=round(length*1.2)), None)


def test_unknown_engine_is_rejected_but_deform_still_default():
    with pytest.raises(ValueError, match="未知 engine"):
        R.generate_cfg(_cfg(engine="v5"), None)
    with pytest.raises(ValueError, match="source_json"):
        R.generate_cfg({"coordinate_system": "GCJ-02"}, None)      # 缺省仍是 deform


# ------------------------------------------------------------------ 打卡点与管理列表
def test_checkin_only_marked_when_route_really_passes_it():
    on = [f"{ANCHORS[1][0]:.6f},{ANCHORS[1][1]:.6f}"]
    off = ["112.430000,34.630000"]
    hit = R.generate_cfg(_cfg(checkin_points=on), None)
    assert hit["data"]["manageList"][0]["marked"] == "Y"
    assert hit["data"]["recodeDislikes"] == 1
    assert hit["metadata"]["checkin_matched"] == 1
    assert hit["data"]["manageList"][0]["index"].isdigit()

    miss = R.generate_cfg(_cfg(checkin_points=off), None)
    assert miss["data"]["manageList"][0]["marked"] == "N"
    assert miss["data"]["recodeDislikes"] == 0
    assert miss["data"]["manageList"][0]["index"] == ""


def test_snap_anchors_inserts_by_path_position():
    anchors = [[112.4170, 34.6120], [112.4190, 34.6120]]
    check = [[112.4180, 34.61202]]        # 离折线约 2.2m：在吸附半径内
    out, report = R.snap_anchors(anchors, check, 10.0)
    assert len(out) == 3 and len(report["inserted"]) == 1
    assert out[1] == (112.4180, 34.61202)          # 插入的是打卡点本身
    assert out[0] == (112.4170, 34.6120) and out[-1] == (112.4190, 34.6120)

    far, report2 = R.snap_anchors(anchors, [[112.5, 34.7]], 10.0)
    assert far == [(112.4170, 34.6120), (112.4190, 34.6120)] and report2["skipped"]
    # 已在锚点上：不重复插
    same, report3 = R.snap_anchors(anchors, [[112.4170, 34.6120]], 10.0)
    assert same == [(112.4170, 34.6120), (112.4190, 34.6120)] and report3["skipped"]


def test_match_checkins_uses_polyline_distance():
    """打点允许偏移后，"经过"必须按到折线的距离判，不能要求顶点重合。"""
    pts = [(112.4, 34.6), (112.401, 34.6)]
    near = R.match_checkins(pts, [(112.4005, 34.60001)], 5.0)     # 离折线约 1.1m
    assert near[0]["matched"] and near[0]["distance_m"] < 2.0
    assert near[0]["index"] in (0, 1)
    far = R.match_checkins(pts, [(112.4005, 34.601)], 5.0)       # 远离约 111m
    assert far[0]["matched"] is False and far[0]["index"] is None
    json.dumps({"h": near+far}, allow_nan=False)                  # inf/nan 会直接抛


def test_snapped_route_then_marks_checkin():
    anchors = [[112.4170, 34.6120], [112.4190, 34.6120]]
    check = [[112.4180, 34.61202]]
    snapped, _ = R.snap_anchors(anchors, check, 10.0)
    task = R.generate_cfg(_cfg(anchors=snapped, checkin_points=[
        f"{check[0][0]:.6f},{check[0][1]:.6f}"]), None)
    assert task["data"]["manageList"][0]["marked"] == "Y"


# ------------------------------------------------------------------ 配置文件往返
def test_saved_config_replays_identically(tmp_path):
    cfg = _cfg(checkin_points=["112.420000,34.612324"])
    path = tmp_path/"my_route.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    from_file = R.generate(str(path))
    in_memory = R.generate_cfg(cfg, None)
    assert ([p["point"] for p in from_file["data"]["pointsList"]] ==
            [p["point"] for p in in_memory["data"]["pointsList"]])
    assert from_file["data"] == in_memory["data"]
    assert from_file["metadata"]["engine"] == "manual"
    assert from_file["metadata"]["coordinate_system"] == "GCJ-02"


# ------------------------------------------------------------------ 里程要求与自动补圈
def _square(size=0.0015):
    """~150m 见方的小环（一圈约 0.6km），首尾同点。"""
    lon0, lat0 = 112.4170, 34.6120
    return [[lon0, lat0], [lon0+size, lat0], [lon0+size, lat0+size],
            [lon0, lat0+size], [lon0, lat0]]


def test_closed_loop_auto_fills_to_min_distance():
    task = R.generate_cfg(_cfg(anchors=_square(), min_distance_m=1500), None)
    md = task["metadata"]
    assert md["closed"] is True and md["laps"] >= 2
    assert task["data"]["recordMileage"]*1000 >= 1500
    assert md["mileage_gap_m"] == 0


def test_open_route_short_reports_gap_without_error():
    anchors = [[112.4170, 34.6120], [112.4175, 34.6120], [112.4180, 34.6120]]
    md = R.generate_cfg(_cfg(anchors=anchors, min_distance_m=1500), None)["metadata"]
    assert md["closed"] is False and md["laps"] == 1
    assert md["mileage_gap_m"] > 0          # 预览不报错，缺口如实上报（落盘时才拦）


def test_auto_loop_stops_at_max_laps():
    md = R.generate_cfg(_cfg(anchors=_square(0.0002), min_distance_m=20000), None)["metadata"]
    assert md["laps"] == R.MAX_AUTO_LAPS and md["mileage_gap_m"] > 0


def test_each_lap_gets_its_own_jitter():
    """同一次生成里，两圈不是同一条线复制（同一条 OU 过程跨圈连续、但状态已不同）。"""
    ring = _square()
    task = R.generate_cfg(_cfg(anchors=ring, min_distance_m=1500), None)
    md, pts = task["metadata"], _points(task)
    assert md["laps"] >= 2
    per = (len(pts)-1)//md["laps"]
    same = sum(1 for i in range(per) if pts[i] == pts[i+per])
    assert same < per*0.5


def test_closed_loop_snaps_tail_to_head():
    ring = _square()
    ring[-1] = [ring[0][0] + 0.00005, ring[0][1]]     # 末点偏开 ~5m
    task = R.generate_cfg(_cfg(anchors=ring, min_distance_m=1500), None)
    pts = _points(task)
    assert task["metadata"]["closed"] is True
    # 首尾都落在起点附近（各容许一个容差），不是几十米开外
    assert R.distance(pts[0], ring[0]) <= 5.0 and R.distance(pts[-1], ring[0]) <= 5.0


def test_panel_auto_fills_and_says_so(panel, monkeypatch):
    monkeypatch.setattr(web, "TASK", {"data": dict(TASK_SNAPSHOT, mileage_min=1.5)})
    preview = web.manual_preview(_body(anchors=_square()))
    assert preview["metrics"]["laps"] >= 2
    assert preview["metrics"]["mileage_gap_m"] == 0
    assert any("自动补到" in w for w in preview["warnings"])


def test_panel_approve_blocks_short_open_route(panel):
    body = _body(anchors=[[112.4170, 34.6120], [112.4180, 34.6120]])
    preview = web.manual_preview(body)
    assert preview["metrics"]["mileage_gap_m"] > 0
    with pytest.raises(ValueError, match="里程还差"):
        web.manual_approve(dict(body, reviewed=True, name="short",
                                preview_sha256=preview["preview_sha256"]))


# ------------------------------------------------------------------ 面板流程
TASK_SNAPSHOT = {
    "mileage_min": 0.5, "mileage_max": 5.0,          # 服务端区间
    "cadence_min": 100, "cadence_max": 220,
    "ra_dislikes": 0, "run_face_status": "N", "fence": "",
}


@pytest.fixture()
def panel(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "ROUTES_DIR", str(tmp_path/"routes"))
    monkeypatch.setattr(web, "SEND_DIR", str(tmp_path/"send"))
    monkeypatch.setattr(web, "CONFIG_PATH", str(tmp_path/"config.ini"))
    monkeypatch.setattr(web, "TASK", {"data": dict(TASK_SNAPSHOT)})
    monkeypatch.setattr(web, "PLAN", {"argv": None, "dry": [], "detail": "", "cwd": str(tmp_path)})
    return tmp_path


def _body(**over):
    body = {"anchors": ANCHORS, "checkins": [], "snap": True, "seed": "7",
            "pace": "5.0", "cadence": "174", "spacing": "6.0",
            "profile": {"jitter_sigma_m": "1.2", "drift_sigma_m": "0.6"}}
    body.update(over)
    return body


def test_preview_payload_is_pure(panel):
    out = web.manual_preview(_body())
    assert out["ok"] and out["metrics"]["points"] > 100
    assert len(out["preview_sha256"]) == 64
    assert out["metrics"]["anchors"] == len(ANCHORS)
    assert out["metrics"]["min_distance_m"] == 500.0        # 任务区间 + 偏移注入
    assert out["metrics"]["mileage_gap_m"] == 0
    assert web.PLAN["argv"] is None          # 预览不登记任何可执行计划


def test_preview_with_auto_seed_randomises_each_time(panel):
    """面板默认发 seed="auto"：每次预览的种子与抖动都不同，但审核能钉死预览用的那个。"""
    a = web.manual_preview(_body(seed="auto"))
    b = web.manual_preview(_body(seed="auto"))
    assert a["ok"] and b["ok"]
    assert isinstance(a["metrics"]["seed"], int)
    assert a["metrics"]["seed"] != b["metrics"]["seed"]
    assert a["preview_sha256"] != b["preview_sha256"]
    # 用 a 的种子重新生成 = a（审核复现的前提；seed 走整数字符串，别被 float 舍入）
    again = web.manual_preview(_body(seed=a["metrics"]["seed_text"]))
    assert again["preview_sha256"] == a["preview_sha256"]
    assert again["metrics"]["seed"] == a["metrics"]["seed"]


def test_preview_without_task_only_does_geometry(panel, monkeypatch):
    monkeypatch.setattr(web, "TASK", {"data": None})
    out = web.manual_preview(_body())
    assert out["ok"] and "尚未读取" in out["warnings"][0]
    assert out["metrics"]["min_distance_m"] is None          # 不知道要求就不假装知道


def test_approve_requires_task(panel, monkeypatch):
    web.manual_preview(_body())
    monkeypatch.setattr(web, "TASK", {"data": None})
    with pytest.raises(ValueError, match="请先「读取任务与打卡点」"):
        web.manual_approve(_body(reviewed=True, name="notask", preview_sha256="x"*64))


def test_approve_writes_route_and_replay_table(panel):
    preview = web.manual_preview(_body())
    out = web.manual_approve(_body(reviewed=True, name="unit_route",
                                   preview_sha256=preview["preview_sha256"]))
    assert out["ok"]
    route_file = os.path.join(web.ROUTES_DIR, "unit_route.json")
    task_file = os.path.join(web.SEND_DIR, "manual_unit_route", "tasklist_0.json")
    assert os.path.exists(route_file) and os.path.exists(task_file)
    saved = json.load(open(route_file, encoding="utf-8"))
    assert saved["engine"] == "manual" and len(saved["anchors"]) == len(ANCHORS)
    task = json.load(open(task_file, encoding="utf-8"))
    M.Yun_For_New._validate_tasklist(task, "unit")      # main 的上传前校验必须能过
    assert web.PLAN["argv"][:2] == ["-a", "-t"] and "--task-file" in web.PLAN["argv"]
    assert web.PLAN["argv"][2] == out["command"]["argv"][2]


def test_approve_needs_review_and_refuses_overwrite(panel):
    preview = web.manual_preview(_body())
    with pytest.raises(ValueError, match="人工审核"):
        web.manual_approve(_body(name="x"))
    web.manual_approve(_body(reviewed=True, name="dup", preview_sha256=preview["preview_sha256"]))
    with pytest.raises(ValueError, match="同名路线"):
        web.manual_approve(_body(reviewed=True, name="dup", preview_sha256=preview["preview_sha256"]))


def test_approve_rejects_stale_preview(panel):
    web.manual_preview(_body())
    with pytest.raises(ValueError, match="不一致"):
        web.manual_approve(_body(reviewed=True, name="stale", preview_sha256="0"*64))


def test_approve_requires_preview_fingerprint(panel):
    with pytest.raises(ValueError, match="预览指纹"):
        web.manual_approve(_body(reviewed=True, name="nosha"))


def test_approve_rolls_back_when_config_cannot_replay(panel, monkeypatch):
    preview = web.manual_preview(_body())

    def boom(_path):
        raise ValueError("模拟落盘后复算失败")
    monkeypatch.setattr(web.yun_route, "generate", boom)
    with pytest.raises(ValueError, match="复算失败"):
        web.manual_approve(_body(reviewed=True, name="rollback",
                                 preview_sha256=preview["preview_sha256"]))
    assert not os.path.exists(os.path.join(web.ROUTES_DIR, "rollback.json"))


def test_name_whitelist_and_bad_numbers(panel):
    with pytest.raises(ValueError, match="文件名"):
        web._safe_name("../evil")
    with pytest.raises(ValueError, match="文件名"):
        web._safe_name("a"*80)
    assert web._safe_name("").startswith("my_route_")
    with pytest.raises(ValueError, match="必须是数字"):
        web.manual_preview(_body(pace="abc"))
    with pytest.raises(ValueError, match="必须在"):
        web.manual_preview(_body(spacing="99"))


def test_preview_clears_previous_plan(panel):
    web._set_plan({"argv": ["-a", "--route-config", "old.json"]})
    assert web.PLAN["argv"] is not None
    web.manual_preview(_body())
    assert web.PLAN["argv"] is None


# ------------------------------------------------------------------ 地图搜索（高德）
def test_geocode_rejects_empty_query(panel):
    with pytest.raises(ValueError, match="请输入"):
        web.geocode("   ")


def test_geocode_requires_map_key(panel):
    with pytest.raises(ValueError, match="map_key"):
        web.geocode("田径场")


def test_geocode_parses_poi_as_gcj02(panel, tmp_path, monkeypatch):
    (tmp_path/"config.ini").write_text("[User]\nmap_key = TESTKEY\n", encoding="utf-8")
    monkeypatch.setattr(web, "_amap", lambda url, params: {"status": "1", "pois": [
        {"name": "田径场", "location": "112.4176,34.6123", "address": "王城大道",
         "pname": "河南省", "cityname": "洛阳市"}]})
    row = web.geocode("田径场")["results"][0]
    assert (row["lon"], row["lat"]) == (112.4176, 34.6123)
    assert row["kind"] == "POI" and "王城大道" in row["address"]


def test_geocode_falls_back_to_address_lookup(panel, tmp_path, monkeypatch):
    (tmp_path/"config.ini").write_text("[User]\nmap_key = TESTKEY\n", encoding="utf-8")
    calls = []

    def fake(url, params):
        calls.append(url)
        if "place/text" in url:
            return {"status": "1", "pois": []}
        return {"status": "1", "geocodes": [
            {"location": "112.4,34.6", "formatted_address": "河南省洛阳市某路"}]}

    monkeypatch.setattr(web, "_amap", fake)
    out = web.geocode("某路")
    assert out["results"][0]["kind"] == "地址" and len(calls) == 2


def test_geocode_reports_amap_error(panel, tmp_path, monkeypatch):
    (tmp_path/"config.ini").write_text("[User]\nmap_key = BAD\n", encoding="utf-8")
    monkeypatch.setattr(web, "_amap",
                        lambda url, params: {"status": "0", "info": "INVALID_USER_KEY"})
    with pytest.raises(ValueError, match="INVALID_USER_KEY"):
        web.geocode("x")


def test_panel_page_exposes_manual_tab():
    page = web.PAGE
    assert 'id="p4"' in page and 'class="tab" data-p="p4"' in page
    assert "previewManual" in page and "approveRoute" in page and "loadTask" in page
    assert 'id="mmap"' in page
    # 搜索入口在地图上方（p4 -> mSearch -> mmap 的文档顺序）
    assert page.index('id="p4"') < page.index('id="mSearch"') < page.index('id="mmap"')
    assert "searchPlace" in page and 'id="mResults"' in page


def test_task_payload_without_config_says_so(panel):
    """面板侧改成子进程调用后，真正读配置的是 task_payload（在辅助进程里跑）。"""
    with pytest.raises(ValueError, match="config.ini"):
        web.task_payload()


def test_python_exe_prefers_project_venv():
    exe = web.python_exe()
    assert os.path.exists(exe)
    venv = os.path.join(web.BASE_DIR, ".venv", "Scripts", "python.exe")
    if os.path.exists(venv):
        assert os.path.normcase(exe) == os.path.normcase(venv)


def test_fetch_task_caches_snapshot_for_warnings(panel, monkeypatch):
    """子进程拿到的任务快照要留在面板进程里，审核时的里程/步频预警才有依据。"""
    monkeypatch.setattr(web, "_helper", lambda flag, timeout=120: {"points": ["1,2"]})
    out = web.fetch_task()
    assert out["points"] == ["1,2"]
    assert web.TASK["data"] is out          # 同一个对象，面板侧后续预警读它


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_helper_parses_last_json_line(monkeypatch):
    """main.py 的杂项输出可能在前面，辅助进程只保证最后一行是 JSON。"""
    monkeypatch.setattr(web.subprocess, "run",
                        lambda *a, **k: _Proc(0, 'log line\n{"ok": true}\n', ""))
    assert web._helper("--helper-task") == {"ok": True}


def test_helper_raises_on_error_payload(monkeypatch):
    monkeypatch.setattr(web.subprocess, "run",
                        lambda *a, **k: _Proc(3, '{"error": "token 失效"}\n', ""))
    with pytest.raises(ValueError, match="token 失效"):
        web._helper("--helper-task")


def test_helper_reports_missing_dependency_clearly(monkeypatch):
    """用没有 requests 的解释器跑辅助进程时，要给出可操作的提示而不是裸报错。"""
    monkeypatch.setattr(web.subprocess, "run",
                        lambda *a, **k: _Proc(1, "", "ModuleNotFoundError: No module named 'requests'\n"))
    with pytest.raises(ValueError, match="缺少依赖"):
        web._helper("--helper-task")


def test_warnings_use_the_same_mileage_window_as_main(panel, tmp_path):
    """预警必须按 config.ini 的偏移换算，和 main.validate_generated_route 用同一个窗口。"""
    conf = tmp_path/"config.ini"
    conf.write_text("[Run]\nsingle_mileage_min_offset = 0.5\n"
                    "single_mileage_max_offset = -0.5\n", encoding="utf-8")
    web.TASK["data"] = {"mileage_min": 1.0, "mileage_max": 20.0,
                        "cadence_min": 120, "cadence_max": 190,
                        "ra_dislikes": 0, "run_face_status": "N", "fence": ""}
    task = R.generate_cfg(_cfg(anchors=[[112.417622, 34.612324], [112.419000, 34.613000]]), None)
    warnings = web._manual_warnings(task)
    assert warnings and "1.500～19.500 km" in warnings[0]
    assert "不足" in warnings[0]

