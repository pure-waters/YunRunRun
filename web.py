# -*- coding: utf-8 -*-
import argparse
import configparser
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
import yun_route  # 轨迹合成引擎（deform）；只 import，不修改

CONFIG_PATH = os.path.join(BASE_DIR, "config.ini")
ROUTES_DIR = os.path.join(BASE_DIR, "examples", "routes")
MASK = "__MASKED__"          # 返回前端的敏感值占位
KEEP = "__KEEP__"            # 前端未改动敏感值的回传哨兵
SENSITIVE_KEYS = {"password", "token", "cipherkey", "cipherkeyencrypted"}

# 面板自产的"发送素材"：work_dir 已被 .gitignore 忽略，不污染仓库。
SEND_DIR = os.path.join(BASE_DIR, "work_dir", "web_send")

RUN = {"proc": None, "buf": [], "lock": threading.Lock()}  # 执行日志占位实现
# 选项卡②最近一次成功预览对应的发送计划：/api/run 只跑它，保证"看到什么就发什么"，
# 也避免前端（或任何本地页面）能凭任意 argv 调起 main.py。
PLAN = {"argv": None, "dry": [], "detail": "", "cwd": BASE_DIR}


# ------------------------------------------------------------------ helpers
def list_campuses():
    out = {}
    for d in sorted(os.listdir(BASE_DIR)):
        if d.startswith("tasks_") and os.path.isdir(os.path.join(BASE_DIR, d)):
            files = sorted(f for f in os.listdir(os.path.join(BASE_DIR, d))
                           if f.endswith(".json"))
            if files:
                out[d] = files
    return out


def list_geojsons():
    """examples/routes/ 下可作形变来源的 GeoJSON（排除 fence* 围栏等非轨迹文件）。"""
    if not os.path.isdir(ROUTES_DIR):
        return []
    return sorted(f for f in os.listdir(ROUTES_DIR)
                  if f.endswith(".geojson") and not f.lower().startswith("fence"))


def list_sources():
    """形变方案的"任选底图 json"清单：任务表 + 任意 GeoJSON（仓库相对路径）。

    与 yun_route.extract_points 的能力对应：tasklist（data.pointsList）、
    GeoJSON LineString/FeatureCollection、裸坐标列表都能直接当形变来源。
    """
    out = []
    for campus, files in list_campuses().items():
        out.extend(f"{campus}/{f}" for f in files)
    out.extend(f"examples/routes/{f}" for f in list_geojsons())
    return out


def _source_path(rel):
    """把清单里的仓库相对路径解析为绝对路径；不在清单内一律拒绝（防目录穿越）。"""
    rel = (rel or "").replace("\\", "/")
    if rel not in list_sources():
        return None
    return os.path.join(BASE_DIR, *rel.split("/"))


def _task_file_points(path):
    """tasklist 原始点列 -> ([[lon,lat],...], km)。'point' 字段格式 'lon,lat'。"""
    j = json.load(open(path, encoding="utf-8-sig"))
    d = j.get("data") or j
    rows = d.get("pointsList") or []
    pts = [[float(x) for x in r["point"].split(",")] for r in rows]
    # 注意：里程字段实测可能是字符串（'2214' 米 / '3' 公里，格式跨表不统一），
    # 防御性处理：>100 视为米，否则视为公里（审计点：单位启发式有歧义风险）
    raw_km = float(rows[-1].get("runMileage") or 0) if rows else 0.0
    km = raw_km / 1000.0 if raw_km > 100 else raw_km
    return pts, km


# ------------------------------------------- 发送计划（②预览 ↔ ③命令，同一份事实）
def _rel(path):
    """绝对路径 → 仓库相对路径（正斜杠）：命令可读，main.py 按调用者 cwd(仓库根)解析。

    路径在别的盘符上时 relpath 会抛 ValueError，此时退回绝对路径（main 同样接受）。
    """
    try:
        return os.path.relpath(path, BASE_DIR).replace("\\", "/")
    except ValueError:
        return os.path.abspath(path).replace("\\", "/")


def _dump_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    return path


def _drill_fixture():
    """离线演练夹具（面板自产，放宽步频/里程区间）。

    判据来自假服务端回包：仓库自带 dry_run_home.json 是打表夹具（raDislikes=3、
    6~7 km），拿它演练合成轨迹必被 validate_generated_route 拒；这份只压链路，
    不冒充真实任务区间（真跑的区间由 getHomeRunInfo 下发，离线无从得知）。
    路径按**当前** SEND_DIR 拼（测试会替换 SEND_DIR，不能写死模块级常量）。

    读到过任务时，把**真实打卡点**塞进夹具：这样 dry-run 日志里"服务端下发的打卡点"
    是有内容的，manageList 也能照常构造。`raDislikes` 仍保持 0——否则
    `--route-config` 会被既有策略直接拒（那条策略不该被演练夹具绕过）。
    """
    info = TASK.get("data") or {}
    points = "|".join(str(p) for p in (info.get("points") or []))
    return _dump_json(os.path.join(SEND_DIR, "dry_home_drill.json"), {
        "code": 200, "msg": "web.py offline drill fixture（放宽区间，仅演练链路）",
        "data": {"cralist": [{"id": 900001, "schoolId": "197", "raType": "T1",
                              "raRunArea": "洛阳理工学院", "raDislikes": 0,
                              "raSingleMileageMin": 1.0, "raSingleMileageMax": 20.0,
                              "raCadenceMin": 1, "raCadenceMax": 1000,
                              "runFaceStatus": "N", "points": points}]}})


def _plan_base(campus, task):
    """打表回放：把所选任务表放进"独占目录"，让 -a 不可能随机选到别的表。

    main.py 的 -t/--task_path 交给 do_by_points_map(random_choose=True)，内部是
    random.choice(os.listdir(目录))：直接把 tasks_ly 传进去，预览的 tasklist_0
    和真正发送的可能是任意一张表。故把所选表复制到只含该表的目录。
    """
    src = os.path.join(BASE_DIR, campus, task)
    pick_dir = os.path.join(SEND_DIR, "task_pick")
    os.makedirs(pick_dir, exist_ok=True)
    for old in os.listdir(pick_dir):
        os.remove(os.path.join(pick_dir, old))
    dst = os.path.join(pick_dir, task)
    shutil.copyfile(src, dst)
    with open(src, "rb") as a, open(dst, "rb") as b:
        if a.read() != b.read():
            raise ValueError("任务表复制校验失败：" + _rel(dst))
    return {"argv": ["-a", "-t", _rel(pick_dir)], "dry": ["--dry-run"], "cwd": BASE_DIR,
            "detail": f"{campus}/{task} → {_rel(pick_dir)}/（单表目录，-a 不会随机选表）"}


def _plan_deform(cfg, preview_task):
    """合成轨迹：把预览用的 cfg 原样落盘，③只引用这一份 cfg。

    自检：用 CLI 的同一入口（yun_route.generate，base_dir=cfg 所在目录）复算一遍，
    与预览逐点比对；不一致就报错，而不是给出一条"看起来对"的命令。
    """
    cfg_path = _dump_json(os.path.join(SEND_DIR, "route_cfg.json"), cfg)
    again = yun_route.generate(cfg_path)
    a, b = preview_task["data"]["pointsList"], again["data"]["pointsList"]
    same = (len(a) == len(b) and a[0]["point"] == b[0]["point"]
            and a[-1]["point"] == b[-1]["point"]
            and (preview_task["data"]["recordMileage"], preview_task["data"]["duration"])
            == (again["data"]["recordMileage"], again["data"]["duration"]))
    if not same:
        raise ValueError("预览与命令不一致（cfg 落盘后复算结果不同）：已拒绝生成命令")
    md = again["metadata"]
    return {"argv": ["-a", "--route-config", _rel(cfg_path)],
            "dry": ["--dry-run", "--dry-home", _rel(_drill_fixture())], "cwd": BASE_DIR,
            "detail": f"{_rel(cfg_path)} ｜ {md['source']} seed={md['seed']} "
                      f"第 {md['attempt']} 次抽中 ｜ {len(b)} 点 "
                      f"{again['data']['recordMileage']:.4f} km"}


def _cadence_gate_warn(cadence):
    """按本地 config.ini 的步频偏移，提前算出"真跑会不会被区间拒"。

    validate_generated_route 判据：raCadenceMin+偏移 ≤ 合成步频 ≤ raCadenceMax+偏移，
    偏移取自 config.ini（本仓库 +30/−150）。区间被压窄到需要 ≥180 spm 宽度时，
    典型任务区间（如 120~190）必然判负——真跑会在 start 之前中止，dry-run 不受影响。
    """
    if cadence is None:
        return ""
    cp = configparser.ConfigParser()
    cp.read(CONFIG_PATH, encoding="utf-8")
    try:
        lo = int(cp.get("Run", "cadence_min_offset"))
        hi = int(cp.get("Run", "cadence_max_offset"))
    except Exception:
        return ""
    need_min, need_max = cadence - lo, cadence - hi
    if need_max - need_min <= 70:          # 70 ≈ 常见任务区间宽度（120~190），不算异常
        return ""
    return (f"步频闸门提示：本次合成步频 {cadence:.1f} spm；按本地 config.ini 偏移 "
            f"({lo:+d}/{hi:+d})，服务端必须 raCadenceMin ≤ {need_min:.0f} 且 "
            f"raCadenceMax ≥ {need_max:.0f}（区间宽 ≥ {need_max - need_min:.0f} spm）才放行。"
            f"若学校下发 120~190，真跑会被 validate_generated_route 拒（步频不在允许范围），"
            f"需调小 cadence_max_offset 的收缩量")


def _set_plan(plan):
    """把②的预览结果登记为③可执行（且唯一可执行）的计划。"""
    PLAN["argv"] = list(plan.get("argv") or []) or None
    PLAN["dry"] = list(plan.get("dry") or [])
    PLAN["detail"] = plan.get("detail", "")
    PLAN["cwd"] = plan.get("cwd", BASE_DIR)


# ------------------------------------------- 手动打点（任务 → 打点 → 抖动 → 审核 → 回放）
# 流程：①复用现有配置/登录取服务端任务与打卡点 → ②地图手动打点 → ③保锚点平滑抖动
# 生成路线 → ④人工审核 → ⑤落 examples/routes/my_route*.json（--route-config 兼容）
# 并派生一份打表任务表 → ⑥模式1（打表）回放上传这条线。
TASK = {"data": None}      # 最近一次 getHomeRunInfo 的精简快照（只读查询，用于约束校验）
SNAP_RADIUS_M = 25.0       # 打卡点自动吸附半径："点在这附近"→ 精确插入该打卡点


def _as_number(value, key, default):
    """前端表单值（可能是字符串）→ 数字；空值用默认值。

    seed 走整数解析：64 位种子超过 2^53，先转 float 会被悄悄舍入（预览/审核就对不上）。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    if isinstance(value, bool):
        raise ValueError(f"{key} 必须是数字")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if key == "seed":
            if not value.is_integer():
                raise ValueError("seed 必须是整数")
            return int(value)
        return value
    text = str(value).strip()
    if key == "seed":
        try:
            return int(text, 10)
        except ValueError as exc:
            raise ValueError("seed 必须是整数") from exc
    try:
        return float(text)
    except ValueError as exc:
        raise ValueError(f"{key} 必须是数字") from exc


def _safe_name(raw):
    """路线文件名白名单：只允许字母数字下划线短横线点，防止路径穿越。"""
    name = str(raw or "").strip()
    if not name:
        return time.strftime("my_route_%Y%m%d_%H%M%S")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name):
        raise ValueError("文件名只能包含字母、数字、下划线、短横线和点（≤64 字符）")
    return name


def python_exe():
    """选一个「能 import main 依赖」的解释器：优先项目 .venv，否则当前解释器。

    面板本身只用标准库 + yun_route，所以 `python web.py` 用什么 Python 都能起；
    但凡是碰 main/tools 的动作（读任务、登录、真实执行）都必须在有 requests/
    gmssl/pycryptodome 的环境里跑——这里统一选解释器，避免"面板能开、一读任务
    就 ModuleNotFoundError"。
    """
    candidates = (os.path.join(BASE_DIR, ".venv", "Scripts", "python.exe"),
                  os.path.join(BASE_DIR, ".venv", "bin", "python"))
    for path in candidates:
        if os.path.exists(path):
            return path
    return sys.executable


def _helper(flag, timeout=120):
    """在带依赖的解释器里跑本文件的辅助模式，取回 JSON。

    子进程 stdout 只用于单行 JSON（其它输出一律走 stderr），这样面板进程可以完全
    不 import main——既避开依赖问题，也避开 main 顶部的 os.chdir 副作用。
    """
    cmd = [python_exe(), os.path.join(BASE_DIR, "web.py"), flag]
    try:
        proc = subprocess.run(cmd, cwd=BASE_DIR, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout,
                              env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"辅助进程超时（{timeout}s）：{flag}") from exc
    except OSError as exc:
        raise ValueError(f"无法启动辅助进程 {cmd[0]}：{exc}") from exc
    out = [line for line in (proc.stdout or "").splitlines() if line.strip()]
    payload = None
    if out:
        try:
            payload = json.loads(out[-1])
        except json.JSONDecodeError:
            payload = None
    if not isinstance(payload, dict):
        err = (proc.stderr or "").strip().splitlines()
        detail = err[-1] if err else f"退出码 {proc.returncode}，无输出"
        if "ModuleNotFoundError" in (proc.stderr or ""):
            raise ValueError("面板子进程缺少依赖（requests/gmssl 等）："
                             f"请用项目 .venv 的解释器启动面板，或 pip install -r requirements.txt"
                             f"（当前选用：{python_exe()}）")
        raise ValueError(f"辅助进程失败：{detail}")
    if payload.get("error"):
        raise ValueError(str(payload["error"]))
    return payload


def task_payload():
    """读取 getHomeRunInfo（在辅助进程里执行）：复用 main.set_args 的配置加载与客户端构造。

    不发 start/split/finish，不创建记录；token 缺失时明确要求先登录，不静默失败。
    """
    import main as M
    if not os.path.exists(CONFIG_PATH):
        raise ValueError("缺少 config.ini：先从 config.example.ini 复制一份并填写")
    M.set_args(CONFIG_PATH)
    if not (M.my_token or "").strip():
        raise ValueError("config.ini 里没有 token：请点『账号密码登录』（会写回配置，并使手机端会话失效）")
    try:
        obj = M.default_client().post_json('/run/getHomeRunInfo', '', raise_on_business_code=True)
    except Exception as exc:                      # noqa: BLE001 - 401/失效要给出可操作提示
        text = str(exc)
        if "401" in text or "认证失败" in text or "token" in text.lower():
            raise ValueError(
                f"{text}（token 可能已失效：点『账号密码登录』重新登录；"
                "登录会使手机端会话失效，尽量少用）") from exc
        raise
    cralist = (obj.get('data') or {}).get('cralist') or []
    if not cralist:
        raise ValueError("服务端未下发可用跑步任务（cralist 为空）")
    data = cralist[0]
    outer = obj.get('data') or {}

    def _num(value, default=0.0):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    return {
        "school_host": M.my_host or "",
        "school_id": data.get("schoolId"),
        "ra_id": data.get("id"),
        "ra_type": data.get("raType"),
        "ra_run_area": data.get("raRunArea"),
        "ra_dislikes": int(_num(data.get("raDislikes"), 0)),
        "run_face_status": data.get("runFaceStatus"),
        "mileage_min": _num(data.get("raSingleMileageMin")),
        "mileage_max": _num(data.get("raSingleMileageMax")),
        "cadence_min": _num(data.get("raCadenceMin")),
        "cadence_max": _num(data.get("raCadenceMax")),
        # 服务端下发的打卡点：'lon,lat|lon,lat|...'，原样保留（manageList 用同一字符串）
        "points": [p.strip() for p in str(data.get("points") or "").split("|") if p.strip()],
        "fence": str(data.get("fence") or "").strip(),
        "day_start": data.get("dayStartTime"),
        "day_end": data.get("dayEndTime"),
        "ra_start": data.get("raStartTime"),
        "ra_end": data.get("raEndTime"),
        "ra_week": data.get("raWeekTime"),
        "qualified": outer.get("qualifiedCount"),
        "target": outer.get("raTargetNum") or data.get("raTargetNum"),
        "face_time": data.get("faceTime"),
        "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def login_payload():
    """账号密码登录（在辅助进程里执行）：复用 tools.Login + main 的会话落盘。"""
    import main as M
    from tools.Login import Login
    result = Login.main(CONFIG_PATH)
    if not result:
        raise ValueError("登录失败或已取消：未写回 token。不要连点重试（可能触发锁定/验证码）")
    M.apply_login_result(*result, conf_path=CONFIG_PATH)
    M.persist_session(CONFIG_PATH, result)
    return {"ok": True, "message": "登录成功，token 已写回 config.ini；手机端会话可能已失效"}


def fetch_task():
    """面板侧入口：子进程读任务，并把快照留在本进程供审核时做约束校验。"""
    info = _helper("--helper-task")
    TASK["data"] = info
    lo_m, hi_m = required_window()          # 面板侧算好"必须落进去的里程区间"给前端显示
    info["need_min_m"], info["need_max_m"] = lo_m, hi_m
    return info


def do_login():
    """面板侧入口：子进程登录；换了 token 之后任务快照作废。"""
    out = _helper("--helper-login")
    TASK["data"] = None
    return out


def _manual_cfg(body):
    """前端 body -> manual 引擎 cfg（白名单；数值范围仍由 yun_route 校验）。"""
    seed_raw = body.get("seed", 31337)
    # "auto" 是引擎支持的"每次随机"语义，不能当数字解析（面板默认就发它）
    seed = ("auto" if isinstance(seed_raw, str) and seed_raw.strip().lower() == "auto"
            else _as_number(seed_raw, "seed", 31337))
    cfg = {
        "engine": "manual",
        "coordinate_system": "GCJ-02",   # 面板底图与服务端打卡点都是 GCJ-02，不做转换
        "anchors": body.get("anchors") or [],
        "checkin_points": body.get("checkins") or [],
        "seed": seed,
        "pace_min_km": _as_number(body.get("pace"), "pace", 5.0),
        "cadence_spm": _as_number(body.get("cadence"), "cadence", 170.0),
        "spacing_m": _as_number(body.get("spacing"), "spacing", 6.0),
    }
    profile = body.get("profile") or {}
    if not isinstance(profile, dict):
        raise ValueError("profile 必须是对象")
    for key, (default, _low, _high) in yun_route.MANUAL_LIMITS.items():
        if key in profile:
            cfg[key] = _as_number(profile[key], key, default)
    if body.get("distance_m") not in (None, ""):
        cfg["distance_m"] = _as_number(body.get("distance_m"), "distance_m", 0)
    return cfg


def _prepare_manual(body):
    """body -> (cfg, 实际锚点)。预览与审核共用，保证"审的就是发的"。

    里程要求**不用前端传**：这里直接按「服务端任务区间 + config.ini 偏移」注入，
    前端改不了它（否则就失去了"必须满足跑步要求"的约束）。
    """
    anchors = yun_route.points_from_raw(body.get("anchors"), "anchors", minimum=2)
    checkins = yun_route.points_from_raw(body.get("checkins") or [], "checkins", minimum=0)
    report = {"inserted": [], "skipped": []}
    if checkins and body.get("snap", True):
        radius = _as_number(body.get("snap_tolerance_m"), "snap_tolerance_m", SNAP_RADIUS_M)
        anchors, report = yun_route.snap_anchors(anchors, checkins, float(radius))
    cfg = _manual_cfg(dict(body, anchors=anchors))
    lo_m, hi_m = required_window()
    if lo_m:
        cfg["min_distance_m"] = round(lo_m, 1)
        cfg["max_distance_m"] = round(hi_m, 1)
    return cfg, anchors, report


def _points_sha(task):
    """点列的稳定指纹：预览与落盘复算必须一致，否则拒绝（"看到什么就发什么"）。"""
    h = hashlib.sha256()
    for p in task["data"]["pointsList"]:
        h.update(f"{p['point']}|{p['runMileage']:.6f}|{p['runTime']}|{p['runStep']}".encode())
    return h.hexdigest()


def _config_offsets():
    """读 [Run] 的里程/步频偏移：预警必须和 main.validate_generated_route 用同一个窗口。"""
    cp = configparser.ConfigParser()
    cp.read(CONFIG_PATH, encoding="utf-8")

    def _get(key):
        try:
            return float(cp.get("Run", key))
        except (configparser.Error, ValueError):
            return 0.0

    return {"mileage_min": _get("single_mileage_min_offset"),
            "mileage_max": _get("single_mileage_max_offset"),
            "cadence_min": _get("cadence_min_offset"),
            "cadence_max": _get("cadence_max_offset")}


def required_window():
    """服务端里程区间 + config 偏移 -> 本次必须落进去的 [lo_m, hi_m]（米）。

    区间被偏移压反（lo > hi）时以 lo 为准：至少满足下限，上限也按 lo 处理。
    没有任务快照时返回 (None, None)——此时只做几何预览，不假装知道要求。
    """
    info = TASK.get("data")
    if not info:
        return None, None
    off = _config_offsets()
    lo = float(info.get("mileage_min") or 0) + off["mileage_min"]
    hi = float(info.get("mileage_max") or 0) + off["mileage_max"]
    if lo <= 0 and hi <= 0:
        return None, None
    return lo*1000.0, max(lo, hi)*1000.0


def _manual_warnings(task):
    """把"能不能发"的判据讲在前面：里程（含自动补圈）/步频/打卡点/人脸/围栏。"""
    md, data = task["metadata"], task["data"]
    out = []
    info = TASK.get("data")
    if not info:
        out.append("尚未读取服务端任务：本次只是几何预览，未与学校里程/步频/围栏比对。")
        return out
    off = _config_offsets()
    km = float(data["recordMileage"])
    lo_m, hi_m = required_window()          # 以"当前任务 + config"为准，不只看引擎元数据
    km_m = km*1000.0
    gap_m = max(0.0, (lo_m or 0.0) - km_m) if lo_m else 0.0
    over_m = max(0.0, km_m - hi_m) if hi_m else 0.0
    if int(md.get("laps") or 1) > 1:
        out.append(f"里程不足，已沿环线自动补到 {md['laps']} 圈（单圈约 {float(md['lap_length_m']):.0f} 米；"
                   "每圈用不同种子，抖动不一样）")
    span = (f"{lo_m/1000:.3f}～{hi_m/1000:.3f} km"
            f"（服务端 {float(info['mileage_min']):g}～{float(info['mileage_max']):g} ± config.ini 偏移）"
            if lo_m else "")
    if gap_m > 0:
        if md.get("closed"):
            out.append(f"里程 {km:.3f} km 不足，要求 {span}：已补到 "
                       f"{yun_route.MAX_AUTO_LAPS} 圈上限，请把单圈画长一点。")
        else:
            out.append(f"里程 {km:.3f} km 不足，要求 {span}："
                       "首尾相接（闭合）可自动补圈，或继续打点。")
    elif over_m > 0:
        out.append(f"超出上限 {over_m:.0f} 米（当前 {km:.3f} km，要求 {span}）："
                   "main 会在到达上限时截断末点，路线尾部会被切掉。")
    elif lo_m:
        out.append(f"里程 {km:.3f} km 满足要求（{span}）")
    cmin = float(info["cadence_min"] or 0) + off["cadence_min"]
    cmax = float(info["cadence_max"] or 0) + off["cadence_max"]
    if cmax > 0 and not cmin <= float(data["recodeCadence"]) <= cmax:
        out.append(f"步频 {data['recodeCadence']:.1f} spm 不在本地有效区间 {cmin:g}～{cmax:g}："
                   "请调步频参数后重新预览。")
    if int(info.get("ra_dislikes") or 0) > 0:
        out.append(f"任务要求经过 {info['ra_dislikes']} 个打卡点：--route-config 路径会被 main 直接拒绝；"
                   "模式1回放会带上 manageList（只标真正经过的点），请逐点确认。")
    if str(info.get("run_face_status") or "").upper() == "Y":
        out.append("任务要求人脸核验：真跑需提供 --face-photo/--face-video，否则在建记录前停止。")
    missing = [c for c in md["checkins"] if not c["matched"]]
    if missing:
        def _dist(c):
            return f"{c['distance_m']}m" if c["distance_m"] is not None else "无可用顶点"
        out.append("有 %d 个打卡点没有被路线经过：%s" % (
            len(missing), "、".join(f"{c['point']}({_dist(c)})" for c in missing[:5])))
    fence = (info.get("fence") or "").strip()
    if fence:
        try:
            ring = [tuple(map(float, p.split(","))) for p in fence.split("|") if p.strip()]
            if len(ring) >= 3:
                yun_route.validate_polygon_route(
                    [tuple(map(float, p["point"].split(","))) for p in data["pointsList"]], ring)
        except (TypeError, ValueError) as exc:
            out.append("围栏校验未通过：" + str(exc))
    return out


def _manual_payload(task, anchors, report):
    """生成结果 -> 前端渲染用的载荷（不回传整张任务表，审核时以参数复算）。"""
    md, data = task["metadata"], task["data"]
    return {
        "ok": True,
        "send": [[float(x) for x in p["point"].split(",")] for p in data["pointsList"]],
        "anchors": [[float(a[0]), float(a[1])] for a in anchors],
        "snap": report,
        "metrics": {
            "points": len(data["pointsList"]),
            "length_km": round(float(data["recordMileage"]), 4),
            "duration_s": int(data["duration"]),
            "pace": round(float(data["recodePace"]), 3),
            "cadence": round(float(data["recodeCadence"]), 1),
            "interval_s": md["interval_s"],
            "step_m": md["mean_step_m"],
            "max_offset_m": md["max_offset_m"],
            "anchor_tolerance_m": md.get("anchor_tolerance_m"),
            "anchor_max_deviation_m": md.get("anchor_max_deviation_m"),
            "anchors": md["anchors"],
            "seed": md["seed"],
            # 大种子别走 JS number：前端审核时用这个字符串原样回传
            "seed_text": md.get("seed_text") or str(md["seed"]),
            "closed": bool(md.get("closed")),
            "laps": int(md.get("laps") or 1),
            "lap_length_m": md.get("lap_length_m"),
            "min_distance_m": md.get("min_distance_m"),
            "max_distance_m": md.get("max_distance_m"),
            "mileage_gap_m": md.get("mileage_gap_m") or 0,
            "mileage_over_m": md.get("mileage_over_m") or 0,
            "checkin_total": md["checkin_total"],
            "checkin_matched": md["checkin_matched"],
        },
        "warnings": _manual_warnings(task),
        "preview_sha256": _points_sha(task),
    }


def manual_preview(body):
    """打点 -> 抖动路线（不落盘、不登记计划；预览失败会清空旧计划）。"""
    _set_plan({})              # 预览一开始就撤销②的旧计划，避免③误发上一条路线
    cfg, anchors, report = _prepare_manual(body)
    task = yun_route.generate_cfg(cfg, BASE_DIR)
    return _manual_payload(task, anchors, report)


def manual_approve(body):
    """人工审核通过 -> 落 my_route*.json + 派生打表任务表 + 登记模式1回放命令。"""
    if not body.get("reviewed"):
        raise ValueError("请先勾选『人工审核通过』再生成文件")
    cfg, anchors, report = _prepare_manual(body)
    task = yun_route.generate_cfg(cfg, BASE_DIR)
    # 落盘前必须满足服务端里程要求：没有任务快照就无从校验，直接拒绝（不猜要求）
    if not TASK.get("data"):
        raise ValueError("请先「读取任务与打卡点」：落盘前要用服务端里程要求校验这条路线")
    gap_m = float(task["metadata"].get("mileage_gap_m") or 0)
    if gap_m > 0:
        lo_m, _hi = required_window()
        hint = ("已按环线自动补圈到上限，请把单圈画长一点"
                if task["metadata"].get("closed") else "首尾相接（闭合）可自动补圈，或继续打点")
        raise ValueError(f"里程还差 {gap_m:.0f} 米（当前 {float(task['data']['recordMileage']):.3f} km，"
                         f"要求 ≥ {(lo_m or 0)/1000:.3f} km）：{hint}")
    sha = _points_sha(task)
    if not body.get("preview_sha256"):
        raise ValueError("缺少预览指纹：请先「生成预览」再审核（审核必须对应一次真实预览）")
    if body["preview_sha256"] != sha:
        raise ValueError("预览与当前生成不一致（锚点/参数/吸附结果已变）：请重新预览再审核")

    name = _safe_name(body.get("name"))
    route_path = os.path.join(ROUTES_DIR, name + ".json")
    if os.path.exists(route_path):
        raise ValueError(
            f"同名路线已存在：{_rel(route_path)}（不覆盖——仓库里的 my_route.json 是 deform 示例）。"
            "请换个名字，例如留空让面板自动用 my_route_时间戳。")
    os.makedirs(ROUTES_DIR, exist_ok=True)
    _dump_json(route_path, cfg)
    try:
        again = yun_route.generate(route_path)     # 以落盘文件为准复算，证明 cfg 可回放
        if _points_sha(again) != sha:
            raise ValueError("落盘后复算与预览不一致：已删除该文件并拒绝生成回放命令")
    except Exception:
        if os.path.exists(route_path):
            os.remove(route_path)
        raise

    task_dir = os.path.join(SEND_DIR, "manual_" + name)
    task_path = _dump_json(os.path.join(task_dir, "tasklist_0.json"), again)
    plan = {
        "argv": ["-a", "-t", _rel(task_dir), "--task-file", "tasklist_0.json"],
        "dry": ["--dry-run", "--dry-home", _rel(_drill_fixture())],
        "cwd": BASE_DIR,
        "detail": (f"{_rel(route_path)} ｜ 模式1回放 {_rel(task_path)} ｜ "
                   f"{float(again['data']['recordMileage']):.4f} km / "
                   f"{len(again['data']['pointsList'])} 点"),
    }
    _set_plan(plan)
    return {"ok": True, "route": _rel(route_path), "task": _rel(task_path),
            "command": plan, "warnings": _manual_warnings(again),
            "metrics": _manual_payload(again, anchors, report)["metrics"]}


# --------------------------------------------------- 地点搜索（高德 Web 服务，GCJ-02）
# 底图是高德瓦片（GCJ-02），服务端打卡点也是 GCJ-02；所以搜索也走高德，坐标才能对得上。
# 只带 [User] map_key 与搜索词，不发送任何账号信息。
AMAP_TEXT_URL = "https://restapi.amap.com/v3/place/text"
AMAP_GEO_URL = "https://restapi.amap.com/v3/geocode/geo"


def _map_key():
    cp = configparser.ConfigParser()
    cp.read(CONFIG_PATH, encoding="utf-8")
    return (cp.get("User", "map_key", fallback="") or "").strip()


def _amap(url, params):
    query = urllib.parse.urlencode({**params, "output": "json"})
    req = urllib.request.Request(f"{url}?{query}",
                                 headers={"User-Agent": "yun-panel/0.1 (local)"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _loc(text):
    parts = str(text or "").split(",")
    if len(parts) != 2:
        return None
    try:
        lon, lat = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0) or (lon == 0 and lat == 0):
        return None
    return lon, lat


def geocode(q):
    """按关键字搜地点，返回 GCJ-02 坐标（先 POI 搜索，再地址解析）。"""
    q = str(q or "").strip()
    if not q:
        raise ValueError("请输入要搜索的地点，例如「洛阳理工学院王城校区 田径场」")
    if len(q) > 60:
        raise ValueError("搜索词过长（≤60 字）")
    key = _map_key()
    if not key:
        raise ValueError("config.ini 缺少 [User] map_key（高德 Web 服务 key），无法搜索地点")
    results = []
    try:
        obj = _amap(AMAP_TEXT_URL, {"key": key, "keywords": q, "offset": "8", "page": "1"})
        if str(obj.get("status")) != "1":
            raise ValueError(f"高德返回错误：{obj.get('info') or obj.get('infocode')}")
        for poi in obj.get("pois") or []:
            loc = _loc(poi.get("location"))
            if not loc:
                continue
            addr = poi.get("address")
            if not isinstance(addr, str):
                addr = ""
            results.append({
                "name": poi.get("name") or q,
                "address": f"{poi.get('pname') or ''}{poi.get('cityname') or ''}{addr}".strip(),
                "lon": loc[0], "lat": loc[1], "kind": "POI",
            })
        if not results:
            obj = _amap(AMAP_GEO_URL, {"key": key, "address": q})
            if str(obj.get("status")) != "1":
                raise ValueError(f"高德返回错误：{obj.get('info') or obj.get('infocode')}")
            for row in obj.get("geocodes") or []:
                loc = _loc(row.get("location"))
                if not loc:
                    continue
                results.append({"name": row.get("formatted_address") or q, "address": "",
                                "lon": loc[0], "lat": loc[1], "kind": "地址"})
    except ValueError:
        raise
    except Exception as exc:      # noqa: BLE001 - 网络/证书/超时都收敛成一句人话
        raise ValueError(f"搜索失败（网络或接口异常）：{exc}") from exc
    if not results:
        raise ValueError(f"没有找到「{q}」：加上城市/校区名再试，或换个说法")
    return {"results": results[:8]}


def _guard(fn, *args):
    """端点统一错误出口：异常变成 {"error": ...}，不让面板崩在 500 上。"""
    try:
        return fn(*args)
    except ValueError as exc:     # 业务/用法错误：消息本身就是给操作者看的
        return {"error": str(exc)}
    except Exception as exc:      # noqa: BLE001 - 其余失败保留类型名，便于定位
        return {"error": f"{type(exc).__name__}: {exc}"}


def build_preview(qs):
    scheme = (qs.get("scheme") or ["base"])[0]
    if scheme == "base":
        campus = (qs.get("campus") or ["tasks_ly"])[0]
        task = (qs.get("task") or [""])[0]
        meta = list_campuses()
        if campus not in meta:
            return {"error": "未知校区目录: " + campus}
        if task not in meta[campus]:
            task = meta[campus][0]
        ref, km = _task_file_points(os.path.join(BASE_DIR, campus, task))
        return {"ref": ref, "send": ref,
                "ref_name": f"参考=即将发送：{campus}/{task}（打表原样上传）",
                "send_name": "打表原轨迹", "send_km": round(km, 3), "note": "",
                "command": _plan_base(campus, task), "warn": ""}

    if scheme == "deform":
        # 轨迹合成（deform）：5.5m 弧长重采样 + 法向 OU 游走 + 二维相关漂移 +
        # 稀疏衰减跳变，再按 ±10% 抽签配速/步频/总里程并做一致性采样。
        # 与 main.py --route-config 共用 yun_route.generate_cfg 同一实现。
        src = (qs.get("src") or [""])[0]
        src_path = _source_path(src)
        if src_path is None:
            return {"error": "未知底图：" + (src or "(空)")}
        try:
            seed = int((qs.get("seed") or ["31337"])[0])
            interval = int((qs.get("interval") or ["2"])[0])
        except ValueError:
            return {"error": "seed / 采样间隔必须是整数"}
        cfg = {"source_json": src_path, "coordinate_system": "GCJ-02",
               "seed": seed, "sample_seconds": interval}
        for key, qk, label in (("min_distance_m", "min", "最短里程"), ("max_distance_m", "max", "最长里程")):
            raw = (qs.get(qk) or [""])[0].strip()
            if raw:
                try:
                    cfg[key] = float(raw)
                except ValueError:
                    return {"error": f"{label}必须是数字（留空=不限制）"}
        try:
            task = yun_route.generate_cfg(cfg, BASE_DIR)
        except ValueError as exc:
            return {"error": str(exc), "window_hint": "可放宽最短/最长里程，或改 seed 重抽"}
        md, data = task["metadata"], task["data"]
        points, ref_metrics = yun_route.load_source(src_path)
        rows = data["pointsList"]
        try:
            command = _plan_deform(cfg, task)
        except ValueError as exc:
            return {"error": str(exc)}
        return {"ref": [[p[0], p[1]] for p in points],
                "send": [[float(x) for x in r["point"].split(",")] for r in rows],
                "ref_name": f"参考原轨迹：{src}",
                "send_name": f"合成任务（seed={md['seed']}，第 {md['attempt']} 次抽中）",
                "send_km": data["recordMileage"],
                "metrics": {
                    "ref_len_km": round(ref_metrics["length_m"]/1000.0, 3),
                    "ref_pace": None if ref_metrics["pace_min_km"] is None else round(ref_metrics["pace_min_km"], 2),
                    "ref_cadence": None if ref_metrics["cadence_spm"] is None else round(ref_metrics["cadence_spm"], 1),
                    "send_len_km": round(data["recordMileage"], 3),
                    "send_pace": round(data["recodePace"], 2),
                    "send_cadence": round(data["recodeCadence"], 1),
                    "chord_km": round(md["geometry_chord_m"]/1000.0, 3),
                    "interval_s": md["interval_s"], "step_m": md["step_m"],
                    "points": len(rows), "attempt": md["attempt"],
                    "window": md["distance_window_m"],
                },
                "note": f"输入 {len(points)} 点 → 输出 {len(rows)} 点",
                "command": command, "warn": _cadence_gate_warn(data["recodeCadence"])}

    return {"error": "未知 scheme: " + scheme}


def read_config_masked():
    cp = configparser.ConfigParser()
    cp.read(CONFIG_PATH, encoding="utf-8")
    out = {}
    for sec in cp.sections():
        out[sec] = {k: (MASK if k.lower() in SENSITIVE_KEYS and cp[sec][k]
                        else cp[sec][k]) for k in cp[sec]}
    return out


def save_config(payload):
    cp = configparser.ConfigParser()
    cp.read(CONFIG_PATH, encoding="utf-8")   # 先读原值，__KEEP__ 直接跳过
    for sec, kv in (payload or {}).items():
        sec = str(sec).strip()
        if not sec or sec.lower() == "DEFAULT":
            continue
        if not cp.has_section(sec):
            cp.add_section(sec)
        for k, v in kv.items():
            if v in (KEEP, MASK):   # 纵深防御：掩码占位串永远不许落盘
                continue
            cp[sec][str(k).strip()] = "" if v is None else str(v)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        cp.write(f)   # 已验证 config.ini 无注释，重写无损（见文件头 TODO）


def run_main(args_list):
    with RUN["lock"]:
        if RUN["proc"] is not None and RUN["proc"].poll() is None:
            return {"error": "已有 main.py 在运行，先点停止"}
        RUN["buf"] = []
        cmd = [python_exe(), os.path.join(BASE_DIR, "main.py")] + list(args_list)
        # 子进程 stdout 是管道 → Python 默认按本地代码页(GBK)写，而这里按 utf-8 读，
        # 日志会整片乱码。只改子进程的 stdio 编码（PYTHONIOENCODING 不影响 open() 默认值）。
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        RUN["buf"].append("$ " + " ".join(cmd) + "\n")
        RUN["proc"] = subprocess.Popen(
            cmd, cwd=BASE_DIR, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace")
        t = threading.Thread(target=_pump, args=(RUN["proc"],), daemon=True)
        t.start()
    return {"ok": True}


def _pump(proc):
    for line in proc.stdout:
        with RUN["lock"]:
            RUN["buf"].append(line)
            if len(RUN["buf"]) > 20000:          # 缓冲上限，防长跑内存膨胀
                del RUN["buf"][:10000]
            RUN["buf"][:] = RUN["buf"]           # offset 语义简单化：截断后前端全量重拉
    proc.wait()
    with RUN["lock"]:
        RUN["buf"].append(f"\n[进程退出 code={proc.returncode} {time.strftime('%H:%M:%S')}]\n")


# ------------------------------------------------------------------ frontend
PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>云运动 · 本地工作台</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <style>
    :root {
      --nav:#111c31;--nav-deep:#0c1425;--nav-sub:#a2b2cc;
      --ink:#162138;--ink-2:#344157;--muted:#738198;--hint:#9ca8b9;
      --blue:#3668e9;--blue-deep:#2456d9;--cyan:#12b9b1;
      --bg:#f4f7fb;--surface:#ffffff;--border:#e2e9f2;
      --soft:#f8fafc;--shadow:0 10px 35px rgba(29,52,95,.05);
      --radius:18px;
    }
    *{box-sizing:border-box}
    html{scroll-behavior:smooth}
    body{margin:0;background:var(--bg);color:var(--ink);font-family:Inter,"Segoe UI",-apple-system,BlinkMacSystemFont,"Microsoft YaHei","PingFang SC",sans-serif;line-height:1.6;-webkit-font-smoothing:antialiased}
    button,input,select,textarea{font:inherit}
    button{cursor:pointer}
    a{color:inherit;text-decoration:none}
    code{font-family:"Cascadia Code",Consolas,"SFMono-Regular",monospace}
    ::selection{background:#dbeafe;color:#173c82}
    .app-shell{min-height:100vh;display:grid;grid-template-columns:264px minmax(0,1fr)}
    .sidebar{background:linear-gradient(168deg,#14233a 0%,#111b2e 56%,#0b1424 100%);color:#fff;min-height:100vh;position:sticky;top:0;height:100vh;padding:30px 16px 22px;display:flex;flex-direction:column;overflow:hidden;isolation:isolate}
    .sidebar:before{content:"";position:absolute;inset:0;pointer-events:none;z-index:-1;background:radial-gradient(circle at -45% -15%,rgba(26,190,178,.26),transparent 40%),radial-gradient(circle at 120% 65%,rgba(68,104,238,.18),transparent 45%)}
    .brand{display:flex;align-items:center;gap:13px;padding:0 14px 36px}
    .brand-mark{width:42px;height:42px;border:1px solid rgba(167,230,235,.28);border-radius:14px;display:grid;place-items:center;color:#e7ffff;background:linear-gradient(140deg,rgba(27,200,182,.35),rgba(32,107,224,.27));box-shadow:0 5px 20px #05102150}
    .brand-mark svg{width:25px;height:25px}
    .brand-title{font-size:18px;line-height:1.3;letter-spacing:.07em;font-weight:760}
    .brand-caption{display:block;font-size:10px;color:#91a7c5;letter-spacing:.18em;font-weight:670;margin-top:3px}
    .nav-caption{font-size:10px;letter-spacing:.22em;color:#6f89ad;font-weight:800;padding:8px 16px 12px}
    #tabs{display:flex;flex-direction:column;gap:7px}
    .tab{min-height:49px;display:flex;align-items:center;gap:13px;cursor:pointer;color:#a8b7d0;font-size:13.5px;font-weight:630;letter-spacing:.01em;border:1px solid transparent;border-radius:11px;padding:0 14px;position:relative;transition:background .18s ease,color .18s ease,transform .18s ease}
    .tab svg{width:19px;height:19px;flex:none;stroke-width:1.8}
    .tab:hover{color:#f0f8ff;background:rgba(255,255,255,.06)}
    .tab.on{color:white;background:linear-gradient(95deg,rgba(60,118,222,.32),rgba(27,151,178,.10));border-color:rgba(145,192,255,.12);box-shadow:inset 3px 0 0 #56d7d0}
    .tab.on svg{color:#70e1da}
    .sidebar-bottom{margin-top:auto;padding:20px 13px 0;border-top:1px solid rgba(160,192,233,.12)}
    .sidebar-visual{height:83px;position:relative;margin:0 0 16px;border:1px solid rgba(157,205,246,.12);border-radius:12px;overflow:hidden;background:radial-gradient(ellipse at 65% 65%,#28648639,transparent 55%),repeating-radial-gradient(ellipse at 60% 95%,transparent 0 16px,#5db6d32e 17px 18px,transparent 19px 27px)}
    .sidebar-visual span{position:absolute;left:14px;top:12px;font-size:10px;letter-spacing:.16em;color:#9ab9d1}
    .sidebar-visual i{position:absolute;width:6px;height:6px;border-radius:50%;background:#41e1c8;right:43px;bottom:25px;box-shadow:0 0 0 7px #4bd7bd20,0 0 22px #26e5c96b}
    .local-label{display:flex;align-items:center;gap:9px;font-size:12px;color:#c7d9e9;font-weight:650}
    .local-label:before{content:"";width:7px;height:7px;border-radius:50%;background:#39d7aa;box-shadow:0 0 0 4px #2ac79c1b}
    #bar{margin-top:9px;word-break:break-word;font-size:11px;color:#839cbb;line-height:1.75}
    #bar a{color:#b6d1f2;text-decoration:underline;text-decoration-color:#6283a9;text-underline-offset:3px}
    .workspace{min-width:0;min-height:100vh}
    .topbar{height:78px;background:rgba(255,255,255,.91);border-bottom:1px solid var(--border);padding:0 clamp(20px,3.5vw,58px);display:flex;justify-content:space-between;align-items:center;gap:12px}
    .breadcrumbs{display:flex;align-items:center;gap:9px;font-size:12px;font-weight:650;color:#8b99ac}
    .breadcrumbs strong{font-weight:760;color:#435373}
    .top-right{display:flex;align-items:center;gap:13px}
    .chip{display:inline-flex;align-items:center;gap:8px;padding:6px 12px;border-radius:30px;border:1px solid #dde9e9;background:#f4fbf9;color:#328272;font-size:11px;font-weight:760;letter-spacing:.03em}
    .chip i{width:6px;height:6px;border-radius:50%;background:#2ac49a}
    .top-clock{display:inline-flex;align-items:center;color:#9aa7b6;font-size:11px;letter-spacing:.1em;font-weight:750}
    .content{padding:38px clamp(20px,3.5vw,58px) 72px;max-width:1640px;margin:0 auto}
    .pane{display:none;animation:reveal .22s ease}
    .pane.on{display:block}
    @keyframes reveal{from{opacity:.5;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}
    .page-titleline{display:flex;flex-wrap:wrap;gap:14px;justify-content:space-between;align-items:center;margin:0 0 28px}
    .eyebrow{font-size:10px;font-weight:830;letter-spacing:.22em;color:#477de2;text-transform:uppercase;margin:0 0 7px}
    h1{font-weight:800;letter-spacing:-.05em;font-size:clamp(27px,2.7vw,35px);line-height:1.28;margin:0 0 6px}
    .page-description{font-size:13px;color:var(--muted);margin:0;max-width:720px}
    .top-number{font-size:11px;color:#a7b2c1;border:1px solid var(--border);padding:8px 11px;border-radius:10px;background:#fff;letter-spacing:.1em;font-weight:750}
    .surface{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow);min-width:0}
    .section-header{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:17px}
    .section-heading{font-size:15px;font-weight:790;margin:0;color:#24334d;letter-spacing:-.02em}
    .section-subtitle{color:#8795aa;font-size:11px}
    .section-badge{font-size:10px;letter-spacing:.07em;color:#5b7baa;font-weight:740;padding:5px 9px;border-radius:7px;background:#f0f5fd}
    .info-banner{display:flex;gap:13px;align-items:flex-start;background:linear-gradient(95deg,#eef5ff,#f5f8ff);padding:17px 20px;border:1px solid #dce9ff;border-radius:14px;margin-bottom:24px}
    .info-banner .banner-icon{color:#4275e9;background:#dceafe;width:31px;height:31px;border-radius:9px;display:grid;place-items:center;flex:none}
    .banner-icon svg{width:17px;height:17px}
    .info-banner b{font-size:12.5px;font-weight:790;color:#2d4f91;display:block;margin-bottom:3px}
    .info-banner p{font-size:12px;color:#6580a5;margin:0}
    .info-banner code{color:#3762ae}
    .settings-surface{padding:26px;margin-bottom:18px}
    #cfg2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));align-items:start;gap:16px}
    #cfg2 .cfg-anchor{display:none}
    fieldset{border:1px solid #e4eaf2;background:#fff;border-radius:13px;margin:0;padding:14px 18px 20px;min-width:0;display:grid;grid-template-columns:minmax(96px,.70fr) minmax(0,1.35fr);align-items:center;column-gap:14px;row-gap:11px}
    fieldset legend{font-weight:800;font-size:13px;color:#223855;padding:0 10px;letter-spacing:.01em}
    fieldset label{font-size:11.5px;font-weight:620;color:#69788f;text-align:left;overflow-wrap:anywhere;min-width:0}
    fieldset br{display:none}
    .f,select{background:#fff;border:1px solid #dce4ee;color:#26384f;border-radius:9px;outline:0;min-height:40px;padding:9px 12px;font-size:12px;transition:box-shadow .15s,border-color .15s,background .15s}
    .f:hover,select:hover{border-color:#aebfdb}
    .f:focus,select:focus{border-color:#6e98ed;box-shadow:0 0 0 3px #d8e8ff99}
    .f::placeholder{color:#a6b0bf}
    fieldset input.f{width:100%;min-width:0}
    button{border:0;transition:background .16s ease,box-shadow .16s ease,transform .16s ease}
    button:hover{transform:translateY(-1px)}
    button:active{transform:translateY(0)}
    .btn{min-height:40px;display:inline-flex;gap:8px;align-items:center;justify-content:center;border-radius:9px;padding:9px 17px;background:linear-gradient(130deg,#4276ec,#305fdc);color:white;font-size:12px;font-weight:750;box-shadow:0 5px 12px #3668e72b;white-space:nowrap}
    .btn:hover{background:linear-gradient(130deg,#4e85ff,#2b59d0);box-shadow:0 7px 17px #3567e63a}
    .btn.gray,.btn-secondary{background:#f2f5fa;color:#50627d;box-shadow:none;border:1px solid #e0e8f1}
    .btn.gray:hover,.btn-secondary:hover{background:#e9f0f9}
    .btn-danger{background:#fff2f3;color:#d4495f;box-shadow:none;border:1px solid #f8d9de}
    .btn-danger:hover{background:#ffe8ed}
    .btn svg{width:15px;height:15px}
    .actionbar{display:flex;align-items:center;flex-wrap:wrap;gap:10px;padding:18px 22px;border-top:1px solid #edf1f6}
    #cfgmsg{font-size:11px;color:#338873;font-weight:650;margin-left:auto}
    .config-hint{font-size:11px;color:#95a3b5;margin-top:10px}
    .control-card{padding:24px 24px 20px;margin-bottom:18px}
    .control-grid{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:end;gap:15px 16px}
    .control-field{display:flex;flex-direction:column;gap:7px;min-width:0}
    .control-field>label,.field-caption{font-size:11px;font-weight:760;color:#65768d}
    .control-field select{max-width:100%;width:100%}
    .control-field.scheme{grid-column:1/-1;width:100%;max-width:500px;min-width:0}
    #pickBase,#pickDeform{grid-column:1;grid-row:2;display:flex;flex-wrap:wrap;align-items:end;gap:11px;min-width:0}
    #pickBase .mini-field,#pickDeform .mini-field{display:flex;flex-direction:column;gap:7px;min-width:0;flex:1 1 120px}
    #pickBase .mini-field:first-child{flex:1 1 145px}
    #pickDeform .mini-field.source{flex:3 1 245px}
    #pickDeform .mini-field.tiny{flex:0 1 80px}
    #pickBase select,#pickDeform select,#pickDeform input.f{width:100%!important;min-width:0}
    #pickDeform .mini-field label,#pickBase .mini-field label{font-size:11px;font-weight:760;color:#65768d;white-space:nowrap}
    .preview-button{grid-column:2;grid-row:2;align-self:end;min-width:135px}
    .metrics-surface{padding:20px 24px;margin-bottom:18px;min-height:82px}
    #pvinfo{font-size:12px;color:#617186;overflow-x:auto;margin:0;line-height:1.8}
    #pvinfo:empty:after{content:"请选择上方方案并点击「刷新预览」，轨迹详情会显示在这里。";color:#9ba8b8;font-size:12px}
    #pvinfo table{border-collapse:separate!important;border-spacing:0;width:100%;min-width:410px;margin:5px 0 10px;font-size:12px!important}
    #pvinfo th,#pvinfo td{padding:12px 15px!important;text-align:left;border-bottom:1px solid #e7edf5}
    #pvinfo th{background:#f2f6fc!important;font-size:11px;color:#647895}
    #pvinfo th:first-child{border-radius:8px 0 0 0}
    #pvinfo th:last-child{border-radius:0 8px 0 0}
    #pvinfo b{color:#275bd2}
    .map-surface{overflow:hidden;margin-bottom:18px}
    .map-head{padding:19px 24px;display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}
    .map-legend{display:flex;align-items:center;flex-wrap:wrap;gap:15px;font-size:11px;font-weight:730;color:#71839b}
    .map-legend span{display:flex;align-items:center;gap:7px}
    .line-sample{display:inline-block;width:23px;height:0;border-top:3px solid #2974f2}
    .line-sample.reference{border-top:3px dashed #9da7b3}
    .map-search{display:flex;flex-wrap:wrap;align-items:end;gap:10px;padding:14px 24px;border-top:1px solid var(--border);background:var(--soft)}
    .map-search .mini-field{display:flex;flex-direction:column;gap:7px;flex:1 1 320px;min-width:190px}
    .map-search .mini-field label{font-size:11px;font-weight:760;color:#65768d;white-space:nowrap}
    .map-search input.f{width:100%!important;min-width:0}
    .map-search-results{padding:10px 24px 14px;background:var(--soft);font-size:11px;color:#75869e;line-height:1.9}
    .map-search-results:empty{display:none}
    #map{height:clamp(390px,61vh,630px);background:linear-gradient(125deg,#e8f1ed,#e2eaf1);border-top:1px solid var(--border);border-bottom:1px solid var(--border);position:relative;z-index:1}
    #mmap{height:clamp(390px,61vh,630px);background:linear-gradient(125deg,#e8f1ed,#e2eaf1);border-top:1px solid var(--border);border-bottom:1px solid var(--border);position:relative;z-index:1}
    .manual-row{display:flex;flex-wrap:wrap;align-items:end;gap:11px}
    .manual-row .mini-field{display:flex;flex-direction:column;gap:7px;min-width:0;flex:1 1 112px}
    .manual-row .mini-field label{font-size:11px;font-weight:760;color:#65768d;white-space:nowrap}
    .manual-row .mini-field input{width:100%!important;min-width:0}
    .pin-label{width:24px;height:24px;border-radius:50%;background:linear-gradient(140deg,#4a86f7,#2f5fe0);color:#fff;display:grid;place-items:center;font:760 11px/1 Inter,sans-serif;border:2px solid #fff;box-shadow:0 3px 9px #1f4bb855}
    .line-sample.send{border-top:3px solid #00a06a}
    .dot-sample{width:9px;height:9px;border-radius:50%;display:inline-block}
    .dot-sample.checkin{background:#ff6b5b;border:2px solid #c0392b}
    .pathline{display:block;font:11.5px "Cascadia Code",Consolas,monospace;color:#2d6c4f;word-break:break-all;margin:3px 0 8px}
    .geo-chip{display:inline-block;margin:3px 6px 3px 0;padding:4px 9px;border:1px solid #d8e2f0;border-radius:8px;background:#f7fafd;color:#33517d;font-size:11.5px;font-weight:650}
    .geo-chip:hover{background:#eaf2ff;border-color:#a9c4ee}
    .map-foot{padding:13px 20px;font-size:11px;color:#718399;line-height:1.75}
    .map-foot code{font-size:10.5px;color:#4165ad}
    .run-control{padding:23px;margin-bottom:18px}
    .run-inline{display:flex;flex-wrap:wrap;align-items:end;gap:10px}
    .run-input{display:flex;flex-direction:column;gap:7px;flex:1 1 360px;min-width:180px}
    .run-input label{font-size:11px;font-weight:760;color:#65768d}
    #runCmd{width:100%!important;min-width:0;font-family:"Cascadia Code",Consolas,monospace;font-size:11.5px;min-height:42px;background:var(--soft);color:#33445e}
    #runCmd[readonly]{cursor:default}
    #runCmd[readonly]:focus{border-color:#dce4ee;box-shadow:none}
    .dry-toggle{display:inline-flex;align-items:center;gap:7px;min-height:40px;padding:0 13px;border:1px solid #dce4ee;border-radius:9px;background:#fff;font-size:12px;font-weight:700;color:#4a5c76;cursor:pointer;user-select:none;white-space:nowrap}
    .dry-toggle:hover{border-color:#aebfdb}
    .dry-toggle input{width:15px;height:15px;accent-color:#3668e9;cursor:pointer;margin:0}
    .gate-warn{display:block;margin-top:6px;color:#a8620f;font-size:11.5px;line-height:1.75}
    .note{font-size:11px;color:#75869e;line-height:1.8;margin:14px 0 0}
    .terminal{background:#121d30;border-radius:var(--radius);overflow:hidden;box-shadow:0 18px 36px #12253c12;border:1px solid #23354e}
    .terminal-head{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;padding:16px 20px;border-bottom:1px solid #253753;background:#162339}
    .terminal-title{display:flex;align-items:center;gap:9px;color:#dbe6f5;font-weight:720;font-size:12px}
    .terminal-dots{display:flex;gap:6px}
    .terminal-dots i{display:block;width:8px;height:8px;border-radius:50%;background:#ff7469}
    .terminal-dots i:nth-child(2){background:#e7c26c}
    .terminal-dots i:nth-child(3){background:#60c89d}
    .terminal-hint{font:10px Consolas,monospace;letter-spacing:.04em;color:#8499b5}
    #log{display:block;width:100%;min-height:465px;height:min(62vh,680px);padding:22px 24px;resize:vertical;background:#101b2e;border:0;outline:none;color:#a7e9ca;font:12px/1.78 "Cascadia Code","SFMono-Regular",Consolas,monospace;white-space:pre;overflow:auto;tab-size:2}
    #log::placeholder{color:#637997}
    .footer-note{font-size:10px;color:#a0adbc;margin-top:28px;text-align:center;letter-spacing:.02em}
    .svg-icon{width:16px;height:16px;vertical-align:-3px;flex:none}
    @media(max-width:1250px){#cfg2{grid-template-columns:1fr}}
    @media(max-width:850px){.app-shell{display:block}.sidebar{position:relative;min-height:unset;height:auto;padding:14px 18px}.sidebar:before{display:none}.brand{padding:0 0 12px}.brand-mark{width:36px;height:36px}.brand-title{font-size:15px}.nav-caption,.sidebar-bottom{display:none}#tabs{flex-direction:row;gap:5px;overflow:auto}.tab{flex:1 0 auto;min-height:43px;justify-content:center;padding:0 13px}.tab.on{box-shadow:inset 0 -3px 0 #56d7d0}.tab svg{width:17px;height:17px}.topbar{height:57px;padding:0 20px}.content{padding:25px 20px 60px}}
    @media(max-width:600px){.page-titleline{margin-bottom:22px}h1{font-size:27px}.content{padding:22px 13px 45px}.topbar{padding:0 14px}.top-clock{display:none}.settings-surface,.control-card,.metrics-surface,.run-control{padding:17px}.info-banner{padding:13px}.control-grid{grid-template-columns:minmax(0,1fr);align-items:stretch}#pickBase,#pickDeform{grid-column:1;grid-row:auto;width:100%;gap:9px}.preview-button{grid-column:1;grid-row:auto;width:100%}.control-field.scheme{grid-column:1}.map-head{padding:15px}.map-legend{gap:11px}.map-search{padding:12px 15px}.map-search-results{padding:9px 15px 12px}.map-foot{padding:13px 15px}#map{height:440px}.actionbar{padding:15px;flex-wrap:wrap}.actionbar .btn{flex:1 1 175px}#cfgmsg{margin:0;width:100%}fieldset{grid-template-columns:1fr;row-gap:6px}fieldset>input.f{margin-bottom:7px}.terminal-head{padding:12px 15px}#log{padding:14px;height:420px}}
    @media(prefers-reduced-motion:reduce){*,*:before,*:after{animation:none!important;transition:none!important;scroll-behavior:auto!important}}
  </style>
</head>
<body>
<div class="app-shell">
  <aside class="sidebar" aria-label="工作台侧边导航">
    <div class="brand">
      <span class="brand-mark" aria-hidden="true">
        <svg viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 22l7-13 5 9 5-6 5 10"/><path d="M8 25h16"/><circle cx="12" cy="9" r="2" fill="currentColor" stroke="none"/></svg>
      </span>
      <div><div class="brand-title">云运动工作台</div><span class="brand-caption">LOCAL DASHBOARD</span></div>
    </div>
    <div class="nav-caption">WORKSPACE / 工作区</div>
    <nav id="tabs">
      <div class="tab on" data-p="p1">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M4 12h16M4 17h16"/><circle cx="8" cy="7" r="2" fill="#14233a"/><circle cx="15" cy="12" r="2" fill="#14233a"/></svg>
        <span>信息配置</span>
      </div>
      <div class="tab" data-p="p2">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><path d="m3 6 6-3 6 3 6-3v15l-6 3-6-3-6 3zM9 3v15m6-12v15"/><path d="m11 11 2 2 2-3"/></svg>
        <span>路线预览</span>
      </div>
      <div class="tab" data-p="p3">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3m6 0h4"/></svg>
        <span>执行日志</span>
      </div>
      <div class="tab" data-p="p4">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><path d="M12 21s7-6.1 7-11a7 7 0 1 0-14 0c0 4.9 7 11 7 11z"/><circle cx="12" cy="10" r="2.5"/></svg>
        <span>手动打点</span>
      </div>
    </nav>
    <div class="sidebar-bottom">
      <div class="sidebar-visual" aria-hidden="true"><span>ROUTE / VISUALIZATION</span><i></i></div>
      <div class="local-label">本地工作模式</div>
      <div id="bar">当前控制台地址：<a id="selflink" href="#">__URL__</a><br>敏感字段未编辑时保持原值</div>
    </div>
  </aside>
  <div class="workspace">
    <header class="topbar">
      <div class="breadcrumbs"><span>YUN</span><span>/</span><strong>CONTROL CENTER</strong></div>
      <div class="top-right"><span class="top-clock">WEB CONSOLE</span><span class="chip"><i></i>127.0.0.1 · LOCAL</span></div>
    </header>
    <main class="content">
      <section class="pane on" id="p1">
        <div class="page-titleline">
          <div><p class="eyebrow">01 / CONFIGURATION</p><h1>信息配置</h1><p class="page-description">集中管理本地配置文件。字段与现有 config.ini 保持一致，修改后可以随时重新读取。</p></div>
          <span class="top-number">SETTINGS · 01</span>
        </div>
        <div class="info-banner wip">
          <span class="banner-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 10v6m0-9v.5"/></svg></span>
          <div><b>快捷登录正在开发中</b><p>当前请继续使用 <code>python main.py</code> 的登录流程。本页只负责查看和修改配置；已有敏感信息默认隐藏，留空则保留原值。</p></div>
        </div>
        <div class="surface settings-surface">
          <div class="section-header"><div><h2 class="section-heading">配置参数</h2><div class="section-subtitle">CONFIGURATION FIELDS · 读取自 config.ini</div></div><span class="section-badge">本地文件</span></div>
          <div id="cfg"></div>
          <div id="cfg2"><div class="cfg-anchor" aria-hidden="true"></div></div>
          <p class="config-hint">带有隐藏提示的字段不会在未编辑时覆盖现有值。</p>
        </div>
        <div class="surface actionbar">
          <button class="btn" onclick="saveCfg()"><svg class="svg-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 4h12l3 3v13H4V4h1zM7 4v6h10V4M7 20v-7h10v7"/></svg>保存配置</button>
          <button class="btn gray" onclick="loadCfg()"><svg class="svg-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 12a8 8 0 1 1-2.3-5.6M20 4v5h-5"/></svg>重新读取 INI</button>
          <span id="cfgmsg" role="status" aria-live="polite"></span>
        </div>
      </section>

      <section class="pane" id="p2">
        <div class="page-titleline">
          <div><p class="eyebrow">02 / ROUTE VISUALIZATION</p><h1>路线预览</h1><p class="page-description">在地图上对照参考轨迹和当前方案的轨迹点列，检查几何位置与路线信息。</p></div>
          <span class="top-number">PREVIEW · 02</span>
        </div>
        <div class="surface control-card">
          <div class="section-header"><div><h2 class="section-heading">预览参数</h2><div class="section-subtitle">ROUTE CONFIGURATION</div></div><span class="section-badge">地图可视化</span></div>
          <div class="control-grid">
            <div class="control-field scheme"><label for="scheme">路线方案</label>
              <select id="scheme" onchange="schemeUI()"><option value="base">方案 1 · 打表回放（原始任务表）</option><option value="deform">方案 2 · 轨迹合成（Deform）</option></select>
            </div>
            <span id="pickBase">
              <span class="mini-field"><label for="campus">校区目录</label><select id="campus"></select></span>
              <span class="mini-field"><label for="task">任务表</label><select id="task"></select></span>
            </span>
            <span id="pickDeform" style="display:none">
              <span class="mini-field source"><label for="srcSel">参考底图 JSON</label><select id="srcSel"></select></span>
              <span class="mini-field tiny"><label for="deformSeed">随机种子</label><input id="deformSeed" class="f" style="width:80px" value="31337"></span>
              <span class="mini-field tiny"><label for="deformInterval">采样间隔 / s</label><input id="deformInterval" class="f" style="width:56px" value="2"></span>
              <span class="mini-field tiny"><label for="deformMin">最短 / m</label><input id="deformMin" class="f" style="width:76px" placeholder="不限"></span>
              <span class="mini-field tiny"><label for="deformMax">最长 / m</label><input id="deformMax" class="f" style="width:76px" placeholder="不限"></span>
            </span>
            <button class="btn preview-button" onclick="preview()"><svg class="svg-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12h4l3-7 4 14 3-7h2"/></svg>刷新预览</button>
          </div>
        </div>
        <div class="surface metrics-surface">
          <div class="section-header" style="margin-bottom:8px"><h2 class="section-heading">轨迹数据</h2><span class="section-badge">METRICS</span></div>
          <div id="pvinfo" aria-live="polite"></div>
        </div>
        <div class="surface map-surface">
          <div class="map-head"><div><h2 class="section-heading">交互地图</h2><div class="section-subtitle">INTERACTIVE MAP · 地图自动定位参考轨迹</div></div>
            <div class="map-legend"><span><i class="line-sample reference"></i>参考路线</span><span><i class="line-sample"></i>当前路线</span></div>
          </div>
          <div id="map" aria-label="轨迹地图"></div>
          <div class="map-foot note">灰色虚线为参考轨迹；蓝色实线为预览方案生成的点列——③「执行日志」发送的就是这条线。方案 2 与 <code>python main.py --route-config &lt;cfg&gt;</code> 使用同一实现；打表模式的旧式微量平移不会单独显示。</div>
        </div>
      </section>

      <section class="pane" id="p3">
        <div class="page-titleline">
          <div><p class="eyebrow">03 / EXECUTION LOGS</p><h1>执行日志</h1><p class="page-description">命令由「路线预览」的当前轨迹自动确定（只读）。勾选 dry run 走本地假传输演练，取消勾选即真实发送这条轨迹。手动打点的路线审核通过后也会把命令切到这里（模式1回放）。</p></div>
          <span class="top-number">TERMINAL · 03</span>
        </div>
        <div class="surface run-control">
          <div class="section-header"><div><h2 class="section-heading">运行控制</h2><div class="section-subtitle">PROCESS CONTROL · 命令由「路线预览」当前轨迹自动确定</div></div><span class="section-badge">MAIN.PY</span></div>
          <div class="run-inline">
            <div class="run-input"><label for="runCmd">将要执行的命令（只读 · 跟随选项卡②/④的预览结果）</label><input class="f" id="runCmd" readonly placeholder="先在「路线预览」刷新预览，或在「手动打点」里生成并审核路线，命令会自动生成"></div>
            <button class="btn" onclick="runMain()"><svg class="svg-icon" viewBox="0 0 24 24" fill="currentColor"><path d="m8 5 12 7-12 7V5z"/></svg>执行</button>
            <label class="dry-toggle" title="勾选=追加 --dry-run（本地假传输：不登录、不探测学校、不 sleep、不发真实请求）"><input type="checkbox" id="dryRun" checked onchange="renderCmd()">dry run</label>
            <button class="btn btn-danger" onclick="killMain()"><svg class="svg-icon" viewBox="0 0 24 24" fill="currentColor"><rect x="5" y="5" width="14" height="14" rx="2"/></svg>停止</button>
            <button class="btn gray" onclick="offset=0;log.value=''">清屏</button>
          </div>
          <p class="note" id="cmdnote">命令与②/④里的轨迹同源（同底图 / 同随机种子 / 同采样间隔与里程区间）。<code>-a</code> 自动确认：面板无法代答控制台的 y/n。</p>
        </div>
        <div class="terminal">
          <div class="terminal-head"><div class="terminal-title"><span class="terminal-dots"><i></i><i></i><i></i></span>&nbsp; 运行输出 / stdout</div><span class="terminal-hint">PYTHON PROCESS · LIVE LOG</span></div>
          <textarea id="log" readonly placeholder="等待进程输出…"></textarea>
        </div>
      </section>
      <section class="pane" id="p4">
        <div class="page-titleline">
          <div><p class="eyebrow">04 / MANUAL ROUTE</p><h1>手动打点</h1><p class="page-description">先取服务端任务与打卡点，再在地图上按顺序打点；生成器让路线贴着打点走（默认 5 米内）并在点之间平滑抖动，最后由人工审核落盘。</p></div>
          <span class="top-number">MANUAL · 04</span>
        </div>
        <div class="surface control-card">
          <div class="section-header"><div><h2 class="section-heading">① 任务与打卡点</h2><div class="section-subtitle">TASK POINTS · 复用 config.ini 与现有登录逻辑</div></div><span class="section-badge" id="taskBadge">未读取</span></div>
          <div class="manual-row">
            <button class="btn" onclick="loadTask()"><svg class="svg-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12a8 8 0 1 1 2.3 5.6M20 4v5h-5"/></svg>读取任务与打卡点</button>
            <button class="btn gray" onclick="panelLogin()">账号密码登录</button>
            <label class="dry-toggle" title="打点落在打卡点附近时，自动把该打卡点精确插入路线（保证经过它）"><input type="checkbox" id="snapChk" checked>自动吸附打卡点</label>
            <button class="btn gray" onclick="undoAnchor()">撤销上一点</button>
            <button class="btn gray" onclick="clearAnchors()">清空打点</button>
          </div>
          <div id="taskInfo" class="note">还没读取任务：先点左上角按钮（会调用 getHomeRunInfo，只读查询，不创建记录）。</div>
        </div>
        <div class="surface control-card">
          <div class="section-header"><div><h2 class="section-heading">② 抖动参数</h2><div class="section-subtitle">SMOOTH JITTER · 打点处严格归零</div></div>
            <button class="btn" onclick="previewManual()"><svg class="svg-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12h4l3-7 4 14 3-7h2"/></svg>生成预览</button></div>
          <div class="manual-row">
            <span class="mini-field"><label for="mSpacing">点间距 / m</label><input class="f" id="mSpacing" value="6.0"></span>
            <span class="mini-field"><label for="mPace">配速 / min·km</label><input class="f" id="mPace" value="5.0"></span>
            <span class="mini-field"><label for="mCad">步频 / spm</label><input class="f" id="mCad" value="170"></span>
            <span class="mini-field"><label for="mSeed">随机种子</label><input class="f" id="mSeed" value="31337"></span>
            <label class="dry-toggle" style="align-self:end" title="勾选=每次预览换一个种子（复用同一组打点也不会生成一模一样的路线）；取消=按右边种子复现"><input type="checkbox" id="autoSeed" checked onchange="seedUI()">每次随机</label>
            <span class="mini-field"><label for="mJit">抖动强度 / m</label><input class="f" id="mJit" value="1.2"></span>
            <span class="mini-field"><label for="mDrift">漂移 / m</label><input class="f" id="mDrift" value="0.6"></span>
            <span class="mini-field"><label for="mTol" title="打点处允许的最大偏移：5 = 路线从打点 5 米以内经过；0 = 严格穿过">打点容差 / m</label><input class="f" id="mTol" value="5"></span>
            <span class="mini-field"><label for="mFade">锚点收敛 / m</label><input class="f" id="mFade" value="15"></span>
          </div>
        </div>
        <div class="surface metrics-surface">
          <div class="section-header" style="margin-bottom:8px"><h2 class="section-heading">轨迹数据</h2><span class="section-badge">MANUAL METRICS</span></div>
          <div id="minfo" aria-live="polite"></div>
        </div>
        <div class="surface map-surface">
          <div class="map-head"><div><h2 class="section-heading">打点地图</h2><div class="section-subtitle">左键打点 · 右键拖动地图 · 滚轮缩放</div></div>
            <div class="map-legend"><span><i class="dot-sample checkin"></i>服务端打卡点</span><span><i class="line-sample"></i>打点连线</span><span><i class="line-sample send"></i>生成路线</span></div>
          </div>
          <div class="map-search">
            <span class="mini-field"><label for="mSearch">搜索地点（高德 · 需 config [User] map_key）</label>
              <input class="f" id="mSearch" placeholder="例如：洛阳理工学院王城校区 田径场" onkeydown="if(event.key==='Enter'){event.preventDefault();searchPlace();}"></span>
            <button class="btn gray" onclick="searchPlace()"><svg class="svg-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.6-3.6"/></svg>搜索并定位</button>
          </div>
          <div id="mResults" class="map-search-results" aria-live="polite"></div>
          <div id="mmap" aria-label="手动打点地图"></div>
          <div class="map-foot note">左键点击加点（双击=放大，不会误加点）、拖动编号点可微调、右键按住拖动地图；红色是服务端打卡点，绿色是抖动后的最终路线——它从每个打点附近经过（默认 5 米内，可调）。首尾相接（闭合）且里程不够时，会沿环线自动补圈。</div>
        </div>
        <div class="surface actionbar">
          <label class="dry-toggle"><input type="checkbox" id="reviewChk">人工审核通过</label>
          <input class="f" id="routeName" placeholder="文件名（留空 = my_route_时间戳）" style="flex:1 1 240px;min-width:170px">
          <button class="btn" onclick="approveRoute()"><svg class="svg-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 4h12l3 3v13H4V4h1zM8 13l3 3 5-5"/></svg>生成 my_route.json</button>
        </div>
        <div class="surface control-card" id="approveBox" style="display:none">
          <div class="section-header"><div><h2 class="section-heading">③ 回放</h2><div class="section-subtitle">MODE 1 · 打表回放这条路线</div></div>
            <button class="btn" onclick="gotoReplay()"><svg class="svg-icon" viewBox="0 0 24 24" fill="currentColor"><path d="m8 5 12 7-12 7V5z"/></svg>去执行（模式1回放）</button></div>
          <div id="approveInfo" class="note"></div>
        </div>
      </section>
      <div class="footer-note">YUN LOCAL DASHBOARD &nbsp;·&nbsp; WEB UI &nbsp;·&nbsp; 仅本地访问</div>
    </main>
  </div>
</div>
<script>
let offset=0, map=null, refLayer=null, sendLayer=null, meta=null, leafletOK=(typeof L!=='undefined');
let lastPlan=null, lastWarn='';   // ②最近一次成功预览：③的命令唯一来源
function showPane(id){
  document.querySelectorAll('.tab,.pane').forEach(x=>x.classList.remove('on'));
  const tab=document.querySelector('.tab[data-p="'+id+'"]'); if(tab) tab.classList.add('on');
  const pane=document.getElementById(id); if(pane) pane.classList.add('on');
  if(id==='p2'){ if(!map&&leafletOK){ map=L.map('map');
      L.tileLayer('https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}',
        {subdomains:['1','2','3','4'],maxNativeZoom:18,maxZoom:20,attribution:'© 高德'}).addTo(map);
      refLayer=L.polyline([],{color:'#999',dashArray:'6,4',weight:3}).addTo(map);
      sendLayer=L.polyline([],{color:'#2379f6',weight:4}).addTo(map); }
    if(map) setTimeout(()=>map.invalidateSize(),50); }
  if(id==='p4'){ initManualMap(); if(mmap) setTimeout(()=>mmap.invalidateSize(),50); }
}
document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>showPane(t.dataset.p));
async function j(u,opt){ const r=await fetch(u,opt); return r.json(); }

async function boot(){ meta=await j('/api/meta');
  const c=document.getElementById('campus'); c.innerHTML='';
  for(const k of Object.keys(meta.campuses)){ const o=document.createElement('option');o.value=k;o.textContent=k;c.appendChild(o);} 
  const ss=document.getElementById('srcSel'); ss.innerHTML='';
  for(const k of meta.sources){ const o=document.createElement('option');o.value=k;o.textContent=k;ss.appendChild(o);} 
  taskFill(); c.onchange=taskFill; loadCfg(); seedUI();
  document.getElementById('selflink').href=location.href;
  document.getElementById('selflink').textContent=location.href;
  renderCmd(); preview();
  if(location.hash==='#manual') showPane('p4');   // 深链接：#manual 直接打开手动打点
  if(location.hash==='#preview') showPane('p2');
  if(location.hash==='#log') showPane('p3'); }
function taskFill(){ const t=document.getElementById('task'); t.innerHTML='';
  for(const k of (meta.campuses[document.getElementById('campus').value]||[])){
    const o=document.createElement('option');o.value=k;o.textContent=k;t.appendChild(o);} }
function schemeUI(){ const s=document.getElementById('scheme').value;
  document.getElementById('pickBase').style.display=(s==='base')?'':'none';
  document.getElementById('pickDeform').style.display=(s==='base')?'none':''; }
function renderCmd(){ const box=document.getElementById('runCmd'), cb=document.getElementById('dryRun');
  const note=document.getElementById('cmdnote');
  if(!lastPlan){ box.value='';
    note.innerHTML='命令与②/④里那条线同源（同底图 / 同随机种子 / 同采样间隔与里程区间）。'+
      '先在「路线预览」刷新预览，或在「手动打点」里生成并审核路线，这里会自动生成对应的发送命令。'; return; }
  box.value='python main.py '+lastPlan.argv.concat(cb.checked?(lastPlan.dry||[]):[]).join(' ');
  note.innerHTML='工作目录 <code>'+lastPlan.cwd+'</code> ｜ '+lastPlan.detail+
    (cb.checked?' ｜ 已追加 <code>--dry-run</code>：本地假传输，不登录、不探测、不发真实请求'
               :' ｜ <b style="color:#c0392b">未勾选 dry run：将真实登录并发送这条轨迹</b>')+
    (lastWarn?'<span class="gate-warn">'+lastWarn+'</span>':''); }

async function loadCfg(){ const d=await j('/api/config'); const host=document.getElementById('cfg2')||document.getElementById('p1');
  host.querySelectorAll('fieldset').forEach(x=>x.remove());
  for(const [sec,kv] of Object.entries(d)){ const fs=document.createElement('fieldset');
    fs.innerHTML='<legend>['+sec+']</legend>';
    for(const [k,v] of Object.entries(kv)){ const lab=document.createElement('label'); lab.textContent=k;
      const inp=document.createElement('input'); inp.className='f'; inp.dataset.sec=sec; inp.dataset.key=k;
      if(v==='__MASKED__'){ inp.dataset.keep='1'; inp.placeholder='•••••• 已隐藏；不输入则保存时原样保留'; }
      else inp.value=v;
      fs.appendChild(lab); fs.appendChild(inp);
      const br=document.createElement('br'); fs.appendChild(br);} 
    host.insertBefore(fs, host.lastElementChild); }
  document.getElementById('cfgmsg').textContent='已读取 '+new Date().toLocaleTimeString(); }
async function saveCfg(){ const body={};
  document.querySelectorAll('#p1 input').forEach(i=>{ const sec=i.dataset.sec,k=i.dataset.key;
    body[sec]=body[sec]||{}; body[sec][k]=(i.dataset.keep&&i.value==='')?'__KEEP__':i.value; });
  const r=await j('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  document.getElementById('cfgmsg').textContent=r.ok?('保存成功 '+new Date().toLocaleTimeString()):('失败: '+(r.error||'?')); }

async function preview(){ const s=document.getElementById('scheme').value;
  const q=new URLSearchParams({scheme:(s==='base'?'base':'deform')});
  if(s==='base'){ q.set('campus',document.getElementById('campus').value);
                  q.set('task',document.getElementById('task').value); }
  else { q.set('src',document.getElementById('srcSel').value);
         q.set('seed',document.getElementById('deformSeed').value||'31337');
         q.set('interval',document.getElementById('deformInterval').value||'2');
         const mn=document.getElementById('deformMin').value.trim();
         const mx=document.getElementById('deformMax').value.trim();
         if(mn) q.set('min',mn);
         if(mx) q.set('max',mx); }
  const d=await j('/api/routes?'+q); const info=document.getElementById('pvinfo');
  if(d.error){ lastPlan=null; lastWarn=''; renderCmd();
    info.innerHTML='<b style=color:#c00>预览失败：'+d.error+'</b>'+
    (d.window_hint?'<br><span style=color:#a60>'+d.window_hint+'</span>':'');
    if(leafletOK){ refLayer.setLatLngs([]); sendLayer.setLatLngs([]); }   /* 失败即清图，③同步失效 */
    return; }
  lastPlan=d.command||null; lastWarn=d.warn||''; renderCmd();
  if(d.metrics){ const m=d.metrics; const f=(v,u,s)=>v===null||v===undefined?'—':(v+(u||''));
    info.innerHTML='<table style="border-collapse:collapse;font-size:13px">'+
      '<tr style="background:#eef3f8"><th style="padding:3px 10px">参数</th><th style="padding:3px 10px">参考原轨迹</th>'+
      '<th style="padding:3px 10px">即将发送</th></tr>'+
      '<tr><td style="padding:3px 10px">总里程</td><td style="padding:3px 10px">'+f(m.ref_len_km,' km')+
      '</td><td style="padding:3px 10px"><b>'+f(m.send_len_km,' km')+'</b></td></tr>'+
      '<tr><td style="padding:3px 10px">配速</td><td style="padding:3px 10px">'+f(m.ref_pace,' min/km')+
      '</td><td style="padding:3px 10px"><b>'+f(m.send_pace,' min/km')+'</b></td></tr>'+
      '<tr><td style="padding:3px 10px">步频</td><td style="padding:3px 10px">'+f(m.ref_cadence,' spm')+
      '</td><td style="padding:3px 10px"><b>'+f(m.send_cadence,' spm')+'</b></td></tr>'+
      '</table>'+
      '<div style="font-size:12px;color:#567;margin-top:4px">'+m.points+' 点 ｜ 间隔 '+m.interval_s+
      's ｜ 每点 '+m.step_m+' m ｜ 几何弦长 '+m.chord_km+' km ｜ '+d.send_name+
      (m.window?' ｜ 里程区间 ['+m.window[0]+', '+m.window[1]+']':'')+'</div>'+
      (d.warn?'<div class="gate-warn">'+d.warn+'</div>':'');
  } else {
    info.innerHTML='<b>参考</b> '+d.ref_name+'　→　<b style=color:#2379f6>即将发送</b> '+d.send_name+
                   '　≈'+d.send_km+' km，'+d.send.length+' 点'+
                   (d.note?'　<b style=color:#a60>［'+d.note+'］</b>':'')+
                   (d.warn?'<div class="gate-warn">'+d.warn+'</div>':'');
  }
  if(!leafletOK){ info.innerHTML+='<br><b>Leaflet CDN 未能加载（离线？），地图不可用</b>'; return; }
  refLayer.setLatLngs(d.ref.map(p=>[p[1],p[0]])); sendLayer.setLatLngs(d.send.map(p=>[p[1],p[0]]));
  if(d.ref.length){ map.fitBounds(L.latLngBounds(d.ref.map(p=>[p[1],p[0]])).pad(0.12)); }
  sendLayer.bindTooltip(d.send_name+' · '+d.send_km+' km',{sticky:true}); }

async function runMain(){ const r=await j('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({dry_run:document.getElementById('dryRun').checked})});
  if(r.error) alert(r.error); }
async function killMain(){ await j('/api/run/kill',{method:'POST'}); }
async function tick(){ try{ const d=await j('/api/log?offset='+offset);
    if(d.text){ log.value+=d.text; offset=d.offset; log.scrollTop=1e9; }
    if(d.reset&&d.text){ log.value=d.text; offset=d.offset; } }catch(e){}
  setTimeout(tick,1200); }
/* ------------------------------------------------ 手动打点（④） */
let mmap=null, mitems=null, mcheck=null, mline=null, mgen=null, mfence=null,
    manchors=[], mtask=null, mlast=null, mplan=null;

function initManualMap(){
  if(mmap) return;
  if(!leafletOK){ document.getElementById('taskInfo').innerHTML=
    '<b style="color:#c00">Leaflet 未能从 CDN 加载（离线？）：地图不可用，无法打点</b>'; return; }
  // 左键只用来打点：关掉 Leaflet 的左键拖动平移，改由右键拖动（见下）。
  mmap=L.map('mmap',{dragging:false,boxZoom:false}).setView([34.6136,112.4184],16);
  L.tileLayer('https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}',
    {subdomains:['1','2','3','4'],maxNativeZoom:18,maxZoom:20,attribution:'© 高德'}).addTo(mmap);
  mfence=L.polygon([],{color:'#e08a3c',weight:2,dashArray:'5,5',fillOpacity:.04}).addTo(mmap);
  mcheck=L.layerGroup().addTo(mmap);
  mgen=L.polyline([],{color:'#00a06a',weight:4}).addTo(mmap);
  mline=L.polyline([],{color:'#2379f6',weight:3,opacity:.7,dashArray:'5,4'}).addTo(mmap);
  mitems=L.layerGroup().addTo(mmap);

  // 左键打点：双击放大的两次 click 会互相抵消（240ms 内只算一次，dblclick 取消）
  let clickTimer=null;
  mmap.on('click',e=>{
    if(e.originalEvent&&e.originalEvent.button===2) return;
    if(clickTimer) return;
    const ll=e.latlng;
    clickTimer=setTimeout(()=>{ clickTimer=null; manchors.push([ll.lng,ll.lat]); drawAnchors(); },240);
  });
  mmap.on('dblclick',()=>{ if(clickTimer){ clearTimeout(clickTimer); clickTimer=null; } });

  // 右键拖动地图（Leaflet 默认只认左键，这里自己实现平移）
  const box=mmap.getContainer();
  let drag=null;
  box.addEventListener('contextmenu',ev=>ev.preventDefault());
  box.addEventListener('mousedown',ev=>{
    if(ev.button!==2) return;
    ev.preventDefault(); ev.stopPropagation();
    drag={x:ev.clientX,y:ev.clientY};
  });
  mmap.on('mouseup',()=>{ drag=null; });
  document.addEventListener('mousemove',ev=>{
    if(!drag) return;
    mmap.panBy([drag.x-ev.clientX, drag.y-ev.clientY],{animate:false});
    drag={x:ev.clientX,y:ev.clientY};
  });
  document.addEventListener('mouseup',()=>{ drag=null; });
}
function drawAnchors(){
  if(!mitems) return;
  mitems.clearLayers();
  manchors.forEach((a,i)=>{
    const m=L.marker([a[1],a[0]],{draggable:true,
      icon:L.divIcon({className:'',html:'<div class="pin-label">'+(i+1)+'</div>',iconSize:[24,24],iconAnchor:[12,12]})}).addTo(mitems);
    m.on('dragend',()=>{ const p=m.getLatLng(); manchors[i]=[p.lng,p.lat]; drawAnchors(); });
  });
  mline.setLatLngs(manchors.map(a=>[a[1],a[0]]));
}
function undoAnchor(){ if(manchors.length){ manchors.pop(); drawAnchors(); } }
function clearAnchors(){ manchors=[]; mlast=null; if(mitems){ mitems.clearLayers(); mline.setLatLngs([]); mgen.setLatLngs([]); }
  document.getElementById('minfo').innerHTML=''; document.getElementById('approveBox').style.display='none'; }

// 地图搜索：高德 Web 服务（GCJ-02，和底图/打卡点同一坐标系）
let geoMark=null;
async function searchPlace(){
  const box=document.getElementById('mResults');
  const q=document.getElementById('mSearch').value.trim();
  if(!q){ box.textContent='请输入地点关键词'; return; }
  box.textContent='搜索中…';
  const d=await j('/api/geocode?q='+encodeURIComponent(q));
  if(d.error){ box.innerHTML='<b style="color:#c00">'+d.error+'</b>'; return; }
  box.innerHTML='<div style="margin:4px 0 2px">点击结果定位（'+d.results.length+' 条）：</div>';
  d.results.forEach(r=>{
    const a=document.createElement('a');
    a.href='#'; a.className='geo-chip';
    a.textContent='['+r.kind+'] '+r.name+(r.address?' · '+r.address:'');
    a.onclick=ev=>{ ev.preventDefault(); focusPlace(r); };
    box.appendChild(a);
  });
}
function focusPlace(r){
  if(!mmap) return;
  mmap.setView([r.lat,r.lon],18);
  if(geoMark) mmap.removeLayer(geoMark);
  geoMark=L.circleMarker([r.lat,r.lon],{radius:9,color:'#7c3aed',weight:3,
    fillColor:'#a78bfa',fillOpacity:.45}).addTo(mmap).bindTooltip(r.name,{direction:'top'}).openTooltip();
}

const _ll=s=>{ const p=String(s).split(',').map(Number); return (p.length>=2&&isFinite(p[0])&&isFinite(p[1]))?p:null; };

async function loadTask(){
  const box=document.getElementById('taskInfo');
  box.textContent='正在读取 getHomeRunInfo…';
  const d=await j('/api/task');
  if(d.error){ document.getElementById('taskBadge').textContent='读取失败';
    box.innerHTML='<b style="color:#c00">'+d.error+'</b>'; return; }
  mtask=d; mlast=null;
  document.getElementById('taskBadge').textContent='已读取 '+(d.points||[]).length+' 个打卡点';
  box.innerHTML='<b>'+((d.ra_run_area)||'-')+'</b> · raType '+d.ra_type+' · 服务端里程 '+
    d.mileage_min+'～'+d.mileage_max+' km'+
    (d.need_min_m?'（含 config 偏移，实际要 '+_km(d.need_min_m)+' ～ '+_km(d.need_max_m)+'）':'')+
    ' · 步频 '+d.cadence_min+'～'+d.cadence_max+
    ' · 打卡点 '+(d.points||[]).length+' 个 · 人脸 '+(d.run_face_status||'-')+
    '<br>时段 '+d.day_start+'~'+d.day_end+' · 任务期 '+d.ra_start+'~'+d.ra_end+
    ' · 已跑 '+(d.qualified==null?'-':d.qualified)+'/'+(d.target==null?'-':d.target)+
    ' · 读取于 '+d.fetched_at;
  if(d.cadence_min&&d.cadence_max&&(d.cadence_min+d.cadence_max)>0){
    document.getElementById('mCad').value=Math.round((d.cadence_min+d.cadence_max)/2);
  }
  drawTask(d); drawAnchors();
}
function drawTask(d){
  if(!mmap) return;
  mcheck.clearLayers(); mfence.setLatLngs([]);
  const pts=(d.points||[]).map(_ll).filter(Boolean);
  pts.forEach((p,i)=>L.circleMarker([p[1],p[0]],{radius:7,color:'#c0392b',weight:2,fillColor:'#ff6b5b',fillOpacity:.85})
    .bindTooltip('打卡点 '+(i+1),{direction:'top'}).addTo(mcheck));
  const fence=(d.fence||'').split('|').map(_ll).filter(Boolean);
  if(fence.length>=3) mfence.setLatLngs(fence.map(p=>[p[1],p[0]]));
  const all=pts.concat(manchors);
  if(all.length) mmap.fitBounds(L.latLngBounds(all.map(p=>[p[1],p[0]])).pad(.3));
}
async function panelLogin(){
  if(!confirm('登录会用账号密码换新 token 并写回 config.ini，可能使手机端会话失效。继续？')) return;
  document.getElementById('taskInfo').textContent='登录中（失败即停，勿连点）…';
  const d=await j('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  document.getElementById('taskInfo').innerHTML=d.error
    ? '<b style="color:#c00">'+d.error+'</b>' : ('✅ '+d.message);
}

function mbody(){
  return {anchors:manchors, checkins:(mtask&&mtask.points)||[],
    snap:document.getElementById('snapChk').checked,
    seed:document.getElementById('autoSeed').checked?'auto':document.getElementById('mSeed').value,
    pace:document.getElementById('mPace').value,
    cadence:document.getElementById('mCad').value,
    spacing:document.getElementById('mSpacing').value,
    profile:{jitter_sigma_m:document.getElementById('mJit').value,
             drift_sigma_m:document.getElementById('mDrift').value,
             anchor_tolerance_m:document.getElementById('mTol').value,
             anchor_fade_m:document.getElementById('mFade').value}};
}
function seedUI(){
  const auto=document.getElementById('autoSeed').checked;
  const box=document.getElementById('mSeed');
  box.disabled=auto; box.style.opacity=auto?.45:1;
}
const _mrow=(k,v)=>'<tr><td style="padding:3px 10px">'+k+'</td><td style="padding:3px 10px"><b>'+v+'</b></td></tr>';
const _km=v=>v==null?'—':(v/1000).toFixed(3)+' km';

async function previewManual(){
  const info=document.getElementById('minfo');
  if(manchors.length<2){ info.innerHTML='<b style="color:#c00">至少在地图上打两个点</b>'; return; }
  info.textContent='生成中…';
  lastPlan=null; lastWarn=''; renderCmd();    // 服务端也已撤销旧计划：③同步失效
  const d=await j('/api/manual/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(mbody())});
  if(d.error){ mlast=null; if(mgen) mgen.setLatLngs([]);
    info.innerHTML='<b style="color:#c00">生成失败：'+d.error+'</b>'; return; }
  mlast=d;
  manchors=d.anchors.slice(); drawAnchors();       // 吸附后的真实锚点回填：审的就是发的
  if(mgen){ mgen.setLatLngs(d.send.map(p=>[p[1],p[0]]));
    if(d.send.length) mmap.fitBounds(L.latLngBounds(d.send.map(p=>[p[1],p[0]])).pad(.12)); }
  const m=d.metrics;
  const need=(m.min_distance_m==null)?'—':(_km(m.min_distance_m)+' ～ '+_km(m.max_distance_m));
  const lapTxt=m.laps>1?('自动补到 '+m.laps+' 圈（单圈 '+m.lap_length_m+' m）'):(m.closed?'单圈就够':'未闭合');
  info.innerHTML='<table style="border-collapse:separate;border-spacing:0;width:100%">'+
    '<tr style="background:#eef3f8"><th style="padding:3px 10px;text-align:left">项目</th>'+
    '<th style="padding:3px 10px;text-align:left">值</th></tr>'+
    _mrow('总里程', m.length_km+' km')+_mrow('里程要求', need)+_mrow('圈数', lapTxt)+
    _mrow('时长', m.duration_s+' 秒')+
    _mrow('配速 / 步频', m.pace+' min·km / '+m.cadence+' spm')+
    _mrow('点数 / 点间距', m.points+' 点 / 约 '+m.step_m+' m')+
    _mrow('平均间隔', m.interval_s+' 秒')+_mrow('最大横向偏移', m.max_offset_m+' m')+
    _mrow('打点容差 / 实际偏离',
      (m.anchor_tolerance_m==null?'—':m.anchor_tolerance_m+' m')+' / '+
      (m.anchor_max_deviation_m==null?'—':m.anchor_max_deviation_m+' m'))+
    _mrow('打点 / 打卡点命中', m.anchors+' 个 / '+m.checkin_matched+'·'+m.checkin_total)+
    _mrow('种子', String(m.seed))+
    '</table>'+
    (d.snap.inserted.length?'<div class="note">自动吸附插入了 '+d.snap.inserted.length+' 个打卡点（已精确落在打卡点上）</div>':'')+
    (d.snap.skipped.length?'<div class="note">未吸附 '+d.snap.skipped.length+' 个：'+
      d.snap.skipped.map(s=>s.reason).join('；')+'</div>':'')+
    (d.warnings.length?'<div class="gate-warn">'+d.warnings.map(w=>'⚠ '+w).join('<br>')+'</div>':'');
}

async function approveRoute(){
  const info=document.getElementById('minfo');
  if(!mlast){ info.innerHTML='<b style="color:#c00">请先「生成预览」</b>'; return; }
  if(!document.getElementById('reviewChk').checked){ info.innerHTML='<b style="color:#c00">请先勾选『人工审核通过』</b>'; return; }
  info.textContent='正在落盘并复算校验…';
  // 审核必须复现"刚才看到的那条"：把预览实际用的种子钉死（否则随机种子会生成另一条）
  const body=Object.assign(mbody(),{seed:(mlast.metrics.seed_text||mlast.metrics.seed), reviewed:true,
                                    name:document.getElementById('routeName').value,
                                    preview_sha256:mlast.preview_sha256});
  const d=await j('/api/manual/approve',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(d.error){ info.innerHTML='<b style="color:#c00">落盘失败：'+d.error+'</b>'; return; }
  mplan=d.command;
  lastPlan=d.command; lastWarn=''; renderCmd();   // ③立即切到"模式1回放这条线"
  document.getElementById('approveBox').style.display='';
  document.getElementById('approveInfo').innerHTML='✅ 路线配置 <span class="pathline">'+d.route+'</span>'+
    '打表任务表 <span class="pathline">'+d.task+'</span>'+
    '<div class="note">模式1回放：<code>python main.py '+d.command.argv.join(' ')+'</code></div>'+
    (d.warnings&&d.warnings.length?'<div class="gate-warn">'+d.warnings.map(w=>'⚠ '+w).join('<br>')+'</div>':'');
  info.innerHTML='<b style="color:#1a7f52">已生成：'+d.route+'</b>';
}
function gotoReplay(){
  if(!mplan) return;
  lastPlan=mplan; lastWarn=''; renderCmd();
  showPane('p3');
}

boot(); tick();
</script>
</body>
</html>"""


# ------------------------------------------------------------------ http glue
class H(BaseHTTPRequestHandler):
    server_version = "YunPanel/0.1"

    def _send(self, code, ctype, body):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + ";charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, "application/json", json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/":
            self._send(200, "text/html", PAGE.replace("__URL__", f"http://127.0.0.1:{PORT}/"))
        elif u.path == "/api/meta":
            self._json({"campuses": list_campuses(), "sources": list_sources()})
        elif u.path == "/api/config":
            try:
                self._json(read_config_masked())
            except Exception as e:
                self._json({"error": str(e)})
        elif u.path == "/api/routes":
            try:
                out = build_preview(q)
            except Exception as e:
                out = {"error": f"{type(e).__name__}: {e}"}
            if out.get("error"):
                _set_plan({})            # 预览失败即撤销计划：③不许再发上一次的轨迹
            else:
                _set_plan(out.get("command") or {})
            self._json(out)
        elif u.path == "/api/log":
            want = int((q.get("offset") or ["0"])[0])
            with RUN["lock"]:
                full = "".join(RUN["buf"])
                if want > len(full):          # 后端截断过 → 全量重发
                    self._json({"text": full, "offset": len(full), "reset": True})
                else:
                    self._json({"text": full[want:], "offset": len(full)})
        elif u.path == "/api/task":
            # 手动打点第①步：服务端任务 + 打卡点（只读查询，不创建记录）
            self._json(_guard(fetch_task))
        elif u.path == "/api/geocode":
            # 地图搜索：高德 Web 服务（GCJ-02，与底图/打卡点同坐标系）
            self._json(_guard(geocode, (q.get("q") or [""])[0]))
        elif u.path == "/api/run/status":
            p = RUN["proc"]
            self._json({"running": p is not None and p.poll() is None})
        else:
            self._send(404, "text/plain", "not found")

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except Exception as e:
            self._json({"error": "bad json: " + str(e)})
            return
        dry = bool((body or {}).get("dry_run", True))   # 缺省按演练处理（安全侧）
        if u.path == "/api/config":
            try:
                save_config(body)
                self._json({"ok": True})
            except Exception as e:
                self._json({"error": str(e)})
        elif u.path == "/api/run":
            # 只跑②登记过的计划：argv 由服务端持有，前端只能决定是否追加 --dry-run，
            # 保证"地图上看到的那条轨迹"就是被发送的那条。
            if not PLAN["argv"]:
                self._json({"error": "还没有发送计划：请先在「路线预览」点一次「刷新预览」。"})
                return
            argv = list(PLAN["argv"])
            if dry:
                argv += list(PLAN["dry"])
            self._json(run_main(argv))
        elif u.path == "/api/run/kill":
            p = RUN["proc"]
            if p is not None and p.poll() is None:
                p.kill()
                self._json({"ok": True})
            else:
                self._json({"ok": False, "error": "没有正在运行的进程"})
        elif u.path == "/api/login":
            # 手动打点第①步的登录：等价于 main.py --login（会写回 token，使手机端失效）
            self._json(_guard(do_login))
        elif u.path == "/api/manual/preview":
            self._json(_guard(manual_preview, body or {}))
        elif u.path == "/api/manual/approve":
            self._json(_guard(manual_approve, body or {}))
        else:
            self._send(404, "text/plain", "not found")

    def log_message(self, fmt, *args):   # 安静模式，避免污染控制台
        pass


PORT = 8080


def main():
    global PORT
    # 说明文字写死：__doc__ 会随文件头整理而消失（本文件已无模块 docstring），
    # 用 __doc__.splitlines()[0] 会直接 AttributeError 导致面板起不来。
    ap = argparse.ArgumentParser(description="云运动脚本本地网页面板（信息配置 / 路线预览 / 执行日志）")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    port = args.port
    for _ in range(12):                     # 端口占用则顺延，最多试 12 个
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), H)
            break
        except OSError:
            port += 1
    else:
        sys.exit("没有可用端口")
    PORT = port
    url = f"http://127.0.0.1:{port}/"
    exe = python_exe()
    print(f"[web.py] 云运动本地面板已启动：{url}   （Ctrl+C 退出）", flush=True)
    print("[web.py] 再次打开浏览器可 Win+R 重跑本命令，或直接点上面的地址", flush=True)
    print(f"[web.py] 需要项目依赖的动作（读任务/登录/执行）用：{exe}", flush=True)
    if os.path.normcase(exe) == os.path.normcase(sys.executable):
        probe = subprocess.run([exe, "-c", "import requests"], capture_output=True)
        if probe.returncode:
            print("[web.py] 警告：该解释器缺少 requests 等依赖，读任务/登录/真实执行会失败。"
                  "建议改用项目 .venv 的解释器启动，例如："
                  r".venv\Scripts\python.exe web.py", flush=True)
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    srv.serve_forever()


if __name__ == "__main__":
    # 辅助模式：用带依赖的解释器跑「读任务 / 登录」，只往 stdout 打一行 JSON。
    helper = next((a for a in sys.argv[1:] if a in ("--helper-task", "--helper-login")), None)
    if helper:
        try:
            payload = {"ok": True}
            payload.update(task_payload() if helper == "--helper-task" else login_payload())
            code = 0
        except Exception as exc:      # noqa: BLE001 - 由父进程解释成 {"error": ...}
            payload, code = {"error": f"{exc}"}, 3
        print(json.dumps(payload, ensure_ascii=False))
        sys.exit(code)
    main()
