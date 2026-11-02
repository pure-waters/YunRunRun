"""轨迹合成（deform 模式）：纯几何二维形变 + 上传任务表构造。

本模块只做一件事：把一条已有轨迹（tasklist / GeoJSON / 裸坐标列表）**形变**成
一条视觉尺度接近历史演示包的新轨迹，并补齐上传所需的逐点里程/时间/步数。

算法（与历史演示包同源，参数见 DEMO_PROFILE）：
    1. 按弧长把来源重采样成 spacing_m（默认 5.5m）等间距折线；
    2. 沿法线做 OU（Ornstein-Uhlenbeck）低频游走 —— 跨道/走廊摆动；
    3. 叠加 x/y 两维相关漂移 —— 定位噪声尺度的大范围位移；
    4. 叠加稀疏、指数衰减的偶发大偏移（约 1.2 次/km，sigma 14m）；
    5. 用固定 seed 保证可复现；可选把几何长度校准到来源的 +3%。

与旧 V4 引擎的区别：V4 是"沿底图法线的单值侧移 offset(i)"，幅度被 max_offset_m
（≤20m）锁死在底图两侧的条带内；deform 直接对二维坐标做形变场，因此能复现演示
包里那种跨越内场、彼此缠绕的密集轨迹。V4 已于 2026-10 移除，旧 cfg 键会得到明确
的迁移报错（见 LEGACY_V4_KEYS）。

纯本地计算：不读取账号、不联网、不判断道路可通行性。围栏（allowed_polygon_geojson）
是可选的一致性校验，不是可跑性证明。
"""
import bisect
import json
import math
import random
import secrets
from pathlib import Path

EARTH_M = 111320.0

# 形变参数：默认值 / 允许下界 / 允许上界。默认值即历史演示包使用的尺度，
# 改动默认值会改变"与演示包一致"的复现基准（回归测试 test_deform_matches_demo_scale）。
PROFILE_LIMITS = {
    "spacing_m": (5.5, 1.0, 50.0),            # 输出等弧长重采样间距（演示包 5.4~5.7m）
    "lane_sigma_m": (2.8, 0.0, 20.0),         # 法向 OU 游走强度
    "lane_tau_m": (280.0, 10.0, 5000.0),      # 法向 OU 相关长度
    "max_lane_m": (5.0, 0.0, 20.0),           # 法向游走硬限幅
    "drift_sigma_m": (4.2, 0.0, 20.0),        # 二维定位漂移强度
    "drift_tau_m": (200.0, 10.0, 5000.0),     # 二维漂移相关长度
    "jump_sigma_m": (14.0, 0.0, 60.0),        # 偶发大偏移幅值（标准差）
    "jump_rate_per_km": (1.2, 0.0, 20.0),     # 偶发大偏移频次
    "jump_decay": (0.90, 0.50, 1.0),          # 偶发偏移逐点衰减
    "length_gain_pct": (0.03, 0.0, 0.5),      # 几何长度校准（相对来源）
    "start_trim_m": (0.0, 0.0, 20000.0),      # 头/尾按弧长裁剪
    "end_trim_m": (0.0, 0.0, 20000.0),
}
DEMO_PROFILE = {k: v[0] for k, v in PROFILE_LIMITS.items()}

# 里程区间不满足时的重抽上限：连续这么多次都没抽中就直接失败（用户指定 5 次）。
MAX_ATTEMPTS = 5

# 来源步频的合理区间（步/分）：越界视为来源字段异常，退回 cfg 名义步频。
SANE_CADENCE_SPM = (110.0, 220.0)

# 已移除的 V4 配置键：命中即给出迁移指引，而不是含糊的"未知键"。
LEGACY_V4_KEYS = {
    "mode", "base_geojson", "base_task", "start_trim", "end_trim",
    "lane_change_indices", "lane_change_choices_m", "lane_transition_points",
    "detour_enabled", "detour_index", "detour_rejoin_offset", "detour_exit_m",
    "detour_forward_m", "detour_step_m", "max_offset_m",
    "telemetry_task", "telemetry_variation",
}


def distance(a, b):
    lon1, lat1, lon2, lat2 = map(math.radians, (*a, *b))
    h = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return 12742000 * math.asin(min(1, math.sqrt(h)))


def number(cfg, key, default, low, high, integer=False):
    value = cfg.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{key} 必须是有限数字")
    if integer and value != int(value):
        raise ValueError(f"{key} 必须为整数")
    if not low <= value <= high:
        raise ValueError(f"{key} 必须在 {low}～{high} 之间" + ("且为整数" if integer else ""))
    return int(value) if integer else float(value)


def profile_of(cfg):
    """合并并校验 deform_profile（未知键与越界值都报错）。"""
    over = cfg.get("deform_profile") or {}
    if not isinstance(over, dict):
        raise ValueError("deform_profile 必须是对象")
    unknown = set(over) - set(PROFILE_LIMITS)
    if unknown:
        raise ValueError(f"未知的形变参数：{sorted(unknown)}")
    merged = dict(DEMO_PROFILE)
    for key, value in over.items():
        low, high = PROFILE_LIMITS[key][1], PROFILE_LIMITS[key][2]
        merged[key] = number({"v": value}, "v", DEMO_PROFILE[key], low, high)
    return merged


def extract_points(obj):
    """从 tasklist / GeoJSON / 裸坐标列表抽出 [(lon,lat), ...]。

    容忍三种输入形态，这是"任选底图 json"的实现基础：
      * tasklist：``data.pointsList[*].point = "lon,lat"``（也接受顶层 pointsList）
      * GeoJSON：LineString / Feature / 单条线的 FeatureCollection
      * 裸坐标列表：``[[lon, lat], ...]``
    """
    if isinstance(obj, list):
        raw = obj
    elif not isinstance(obj, dict):
        raise ValueError("输入必须是 JSON 对象或坐标列表")
    elif isinstance(obj.get("data"), dict) and isinstance(obj["data"].get("pointsList"), list):
        raw = []
        for row in obj["data"]["pointsList"]:
            text = row.get("point") if isinstance(row, dict) else None
            if not isinstance(text, str) or text.count(",") != 1:
                raise ValueError("tasklist pointsList 中存在非法 point")
            raw.append([float(x) for x in text.split(",")])
    elif isinstance(obj.get("pointsList"), list):
        raw = [[float(x) for x in row["point"].split(",")] for row in obj["pointsList"]]
    else:
        geo = obj
        if obj.get("type") == "FeatureCollection":
            features = obj.get("features", [])
            if not isinstance(features, list) or len(features) != 1:
                raise ValueError("GeoJSON FeatureCollection 必须只有一条线")
            geo = features[0].get("geometry", {}) if isinstance(features[0], dict) else {}
        elif obj.get("type") == "Feature":
            geo = obj.get("geometry", {})
        if geo.get("type") != "LineString":
            raise ValueError("无法识别输入：需要 tasklist、GeoJSON LineString 或坐标列表")
        raw = geo.get("coordinates", [])
    out = []
    for p in raw:
        if not isinstance(p, (list, tuple)) or len(p) < 2:
            raise ValueError("坐标必须为 [经度, 纬度]")
        lon, lat = float(p[0]), float(p[1])
        if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -85 <= lat <= 85):
            raise ValueError("存在非法经纬度")
        if not out or distance(out[-1], (lon, lat)) > 0.01:
            out.append((lon, lat))
    if len(out) < 8:
        raise ValueError("有效轨迹点少于 8 个")
    # 局部投影形变只适用于校园尺度；跨度过大时投影畸变会失真，直接拒绝。
    if any(max(p[k] for p in out) - min(p[k] for p in out) > 0.5 for k in (0, 1)):
        raise ValueError("仅支持局部校园几何，经纬度跨度不能超过 0.5 度")
    return out


def load_points(path):
    return extract_points(json.loads(Path(path).read_text(encoding="utf-8-sig")))


def load_source(path):
    """读来源文件，返回 (点列, 节奏指标)。

    节奏指标只在来源是 tasklist（带 runMileage/runTime/runStep）时可得：
      * length_m    —— 总里程（米）
      * pace_min_km —— 总体配速（分/公里）
      * cadence_spm —— 总体步频（步/分）
    GeoJSON / 裸坐标列表只有几何，配速与步频为 None（此时用 cfg 的名义值做基准）。
    """
    obj = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    points = extract_points(obj)
    metrics = {"length_m": None, "pace_min_km": None, "cadence_spm": None}
    rows = None
    if isinstance(obj, dict) and isinstance(obj.get("data"), dict):
        rows = obj["data"].get("pointsList")
    elif isinstance(obj, dict) and isinstance(obj.get("pointsList"), list):
        rows = obj.get("pointsList")
    if isinstance(rows, list) and len(rows) >= 2:
        try:
            miles = [float(r["runMileage"]) for r in rows]
            times = [float(r["runTime"]) for r in rows]
        except (KeyError, TypeError, ValueError):
            miles = times = None
        # runStep 单独解析：洛阳理工的任务表没有该字段，此时仍应能拿到配速，
        # 只是拿不到步频（退回 cfg 名义值）。
        try:
            steps = [float(r["runStep"]) for r in rows] if times is not None else None
        except (KeyError, TypeError, ValueError):
            steps = None
        if miles:
            span_m, span_s = miles[-1]-miles[0], times[-1]-times[0]
            if span_m > 0:
                metrics["length_m"] = span_m
                if span_s > 0:
                    pace = span_s/60.0/(span_m/1000.0)
                    if 2.0 <= pace <= 30.0:          # 与 cfg 名义配速同一合理区间
                        metrics["pace_min_km"] = pace
                    if steps is not None:
                        step_span = steps[-1]-steps[0]
                        cadence = step_span*60.0/span_s if step_span > 0 else 0.0
                        # 合理性闸门：来源 runStep 单位异常时会算出离谱步频（历史上见过
                        # 489 spm）；跑步步频合理区间取 110~220，越界一律不采信、
                        # 退回 cfg 名义步频。
                        if SANE_CADENCE_SPM[0] <= cadence <= SANE_CADENCE_SPM[1]:
                            metrics["cadence_spm"] = cadence
    if metrics["length_m"] is None:
        metrics["length_m"] = sum(distance(a, b) for a, b in zip(points, points[1:]))
    return points, metrics


def local_projection(points):
    lat0 = sum(p[1] for p in points) / len(points)
    origin = points[0]
    sx = EARTH_M * math.cos(math.radians(lat0))
    sy = EARTH_M
    fwd = lambda p: ((p[0]-origin[0])*sx, (p[1]-origin[1])*sy)
    inv = lambda p: (p[0]/sx+origin[0], p[1]/sy+origin[1])
    return fwd, inv


def cumulative_xy(points):
    out = [0.0]
    for a, b in zip(points, points[1:]):
        out.append(out[-1]+math.dist(a, b))
    return out


def point_at(points, lengths, s):
    if s <= 0:
        return points[0]
    if s >= lengths[-1]:
        return points[-1]
    j = max(1, min(len(points)-1, bisect.bisect_left(lengths, s)))
    a, b = points[j-1], points[j]
    den = lengths[j]-lengths[j-1]
    f = 0.0 if den <= 1e-12 else (s-lengths[j-1])/den
    return (a[0]+(b[0]-a[0])*f, a[1]+(b[1]-a[1])*f)


def resample(points, spacing_m=5.5, start_trim_m=0.0, end_trim_m=0.0):
    fwd, inv = local_projection(points)
    xy = [fwd(p) for p in points]
    lengths = cumulative_xy(xy)
    lo = max(0.0, float(start_trim_m))
    hi = max(lo, lengths[-1]-max(0.0, float(end_trim_m)))
    if hi-lo < max(20.0, spacing_m*4):
        raise ValueError("裁剪后轨迹过短")
    n = max(8, int(math.floor((hi-lo)/spacing_m))+1)
    ss = [lo+(hi-lo)*i/(n-1) for i in range(n)]
    return [inv(point_at(xy, lengths, s)) for s in ss]


def tangent_normal(xy, i):
    a, b = xy[max(0, i-2)], xy[min(len(xy)-1, i+2)]
    dx, dy = b[0]-a[0], b[1]-a[1]
    n = math.hypot(dx, dy)
    if n < 1e-9:
        a, b = xy[max(0, i-1)], xy[min(len(xy)-1, i+1)]
        dx, dy = b[0]-a[0], b[1]-a[1]
        n = math.hypot(dx, dy)
    if n < 1e-9:
        return (1.0, 0.0), (0.0, 1.0)
    tx, ty = dx/n, dy/n
    return (tx, ty), (-ty, tx)


def ou_distance(rng, s_values, tau_m, sigma_m):
    """按空间距离推进的 OU 过程（不是按点序，换采样密度不影响形状）。"""
    if sigma_m <= 0:
        return [0.0]*len(s_values)
    x = 0.0
    out = []
    prev = s_values[0]
    for s in s_values:
        ds = max(0.0, s-prev)
        prev = s
        phi = math.exp(-ds/max(tau_m, 1e-6))
        step = sigma_m*math.sqrt(max(0.0, 1.0-phi*phi))
        x = phi*x+rng.gauss(0.0, step)
        out.append(x)
    return out


def deform_points(points, seed=31337, profile=None):
    """核心形变：[(lon,lat), ...] -> [(lon,lat), ...]（实现与演示包一致）。"""
    cfg = dict(DEMO_PROFILE)
    if profile:
        cfg.update(profile)
    base = resample(points, cfg["spacing_m"], cfg["start_trim_m"], cfg["end_trim_m"])
    fwd, inv = local_projection(base)
    xy = [fwd(p) for p in base]
    ss = cumulative_xy(xy)
    rng = random.Random(int(seed) ^ 0x8FFF)
    lane = ou_distance(rng, ss, cfg["lane_tau_m"], cfg["lane_sigma_m"])
    dx = ou_distance(rng, ss, cfg["drift_tau_m"], cfg["drift_sigma_m"])
    dy = ou_distance(rng, ss, cfg["drift_tau_m"], cfg["drift_sigma_m"])
    jump_p_per_m = max(0.0, cfg["jump_rate_per_km"])/1000.0
    jx = jy = 0.0
    components = []
    prev_s = ss[0]
    for i, (p, s) in enumerate(zip(xy, ss)):
        _, nrm = tangent_normal(xy, i)
        lane_i = max(-cfg["max_lane_m"], min(cfg["max_lane_m"], lane[i]))
        ds = max(0.0, s-prev_s)
        prev_s = s
        # 泊松近似：按空间长度触发，与采样点数量解耦。
        if cfg["jump_sigma_m"] > 0 and rng.random() < 1.0-math.exp(-jump_p_per_m*ds):
            ang = rng.uniform(0, 2*math.pi)
            mag = abs(rng.gauss(0, cfg["jump_sigma_m"]))
            jx += mag*math.cos(ang)
            jy += mag*math.sin(ang)
        jx *= cfg["jump_decay"]
        jy *= cfg["jump_decay"]
        components.append((nrm[0]*lane_i+dx[i]+jx, nrm[1]*lane_i+dy[i]+jy))

    def candidate(scale):
        return [(p[0]+u*scale, p[1]+v*scale) for p, (u, v) in zip(xy, components)]

    # 可选的视觉长度校准：只缩放同一组随机位移、不重新抽样，因此同 seed 可复现。
    gain = max(0.0, float(cfg.get("length_gain_pct", 0.0)))
    scale = 1.0
    if gain > 0 and len(xy) > 2:
        source_xy = [fwd(p) for p in points]
        original_len = sum(math.dist(a, b) for a, b in zip(source_xy, source_xy[1:]))
        target_len = original_len*(1.0+gain)
        lo, hi = 0.0, 3.0
        for _ in range(24):
            mid = (lo+hi)/2
            test = candidate(mid)
            length = sum(math.dist(a, b) for a, b in zip(test, test[1:]))
            if length < target_len:
                lo = mid
            else:
                hi = mid
        scale = (lo+hi)/2
    out = []
    for q in candidate(scale):
        lon, lat = inv(q)
        out.append((round(lon, 8), round(lat, 8)))
    return out


def deform_geometry(source_points, *, seed=31337, profile=None):
    """公开的纯几何 API：来源点列 -> 形变点列（不涉及上传字段）。

    source_points 可以是 [(lon,lat), ...]、[[lon,lat], ...]、tasklist dict 或
    GeoJSON dict；profile 键值会先按 PROFILE_LIMITS 校验。
    """
    points = extract_points(source_points if isinstance(source_points, (list, dict)) else list(source_points))
    merged = profile_of({"deform_profile": profile} if profile else {})
    return deform_points(points, seed=int(seed), profile=merged)


def cumulative(points):
    result = [0.0]
    for a, b in zip(points, points[1:]):
        result.append(result[-1]+distance(a, b))
    return result


def client_speed(meters, seconds):
    # APK SportRunMapActivity:4140-4154：字段名 speed，实际为 min/km。
    return format(min(900, max(1, seconds/60/(meters/1000))), '.2f') if meters > 0 and seconds > 0 else '0.0'


def position_at(route, lengths, meters):
    j = min(len(route)-1, max(1, bisect.bisect_left(lengths, meters)))
    a, b = route[j-1:j+1]
    f = (meters-lengths[j-1])/(lengths[j]-lengths[j-1])
    return tuple(round(a[k]+(b[k]-a[k])*f, 8) for k in (0, 1))


# ---------------------------------------------------------------- 围栏校验
def allowed_polygon(path):
    obj = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(obj, dict):
        raise ValueError('可跑区域必须是 GeoJSON 对象')
    if obj.get('type') == 'FeatureCollection':
        features = obj.get('features', [])
        if len(features) != 1:
            raise ValueError('可跑区域文件只能包含一个 Polygon')
        obj = features[0].get('geometry', {})
    elif obj.get('type') == 'Feature':
        obj = obj.get('geometry', {})
    if obj.get('type') != 'Polygon' or len(obj.get('coordinates', [])) != 1:
        raise ValueError('可跑区域必须是单环 Polygon；带洞或多区域暂不支持')
    raw_ring = obj['coordinates'][0]
    if not isinstance(raw_ring, list):
        raise ValueError('可跑区域坐标必须是列表')
    ring = []
    for p in raw_ring:
        if (not isinstance(p, list) or len(p) != 2 or
                any(isinstance(v, bool) or not isinstance(v, (int, float)) or
                    not math.isfinite(v) for v in p)):
            raise ValueError('可跑区域坐标必须是有限经纬度')
        if not -180 <= p[0] <= 180 or not -85 <= p[1] <= 85:
            raise ValueError('可跑区域经纬度超出支持范围')
        if not ring or tuple(p) != ring[-1]:
            ring.append(tuple(p))
    if ring and ring[0] == ring[-1]:
        ring.pop()
    if len(ring) < 3:
        raise ValueError('可跑区域至少需要三个不同顶点')
    if len(ring) > 10000:
        raise ValueError('可跑区域顶点超过 10000 个')
    x0, y0 = ring[0]
    area = sum(((a[0]-x0)*(b[1]-y0)-(b[0]-x0)*(a[1]-y0))
               for a, b in zip(ring, ring[1:]+ring[:1]))
    if abs(area) < 1e-18:
        raise ValueError('可跑区域多边形面积为零')
    edges = list(zip(ring, ring[1:]+ring[:1]))
    for i, (a, b) in enumerate(edges):
        for j in range(i+1, len(edges)):
            if j == i+1 or (i == 0 and j == len(edges)-1):
                continue
            c, d = edges[j]
            if segment_intersections(a, b, c, d):
                raise ValueError('可跑区域多边形自交')
    return ring


def cross(a, b):
    return a[0]*b[1]-a[1]*b[0]


def on_segment(p, a, b):
    ab = (b[0]-a[0], b[1]-a[1])
    ap = (p[0]-a[0], p[1]-a[1])
    scale = max(abs(ab[0]), abs(ab[1]), 1e-12)
    return (abs(cross(ab, ap)) <= 1e-13*scale and
            min(a[0], b[0])-1e-12 <= p[0] <= max(a[0], b[0])+1e-12 and
            min(a[1], b[1])-1e-12 <= p[1] <= max(a[1], b[1])+1e-12)


def segment_intersections(a, b, c, d):
    """返回 AB 与 CD 的交点在 AB 上的参数，含相切及共线端点。"""
    r = (b[0]-a[0], b[1]-a[1])
    s = (d[0]-c[0], d[1]-c[1])
    qa = (c[0]-a[0], c[1]-a[1])
    det = cross(r, s)
    if abs(det) < 1e-20:
        if abs(cross(qa, r)) > 1e-20:
            return []
        rr = r[0]*r[0]+r[1]*r[1]
        if rr == 0:
            return [0.0] if on_segment(a, c, d) else []
        return [max(0.0, min(1.0, ((p[0]-a[0])*r[0]+(p[1]-a[1])*r[1])/rr))
                for p in (c, d) if on_segment(p, a, b)] or [
                    0.0 if on_segment(a, c, d) else 1.0
                    for p in (a, b) if on_segment(p, c, d)]
    t = cross(qa, s)/det
    u = cross(qa, r)/det
    return [max(0.0, min(1.0, t))] if -1e-12 <= t <= 1+1e-12 and -1e-12 <= u <= 1+1e-12 else []


def inside(point, ring):
    x, y = point
    result = False
    for a, b in zip(ring, ring[1:]+ring[:1]):
        if on_segment(point, a, b):
            return True
        if (a[1] > y) != (b[1] > y):
            edge = a[0]+(y-a[1])*(b[0]-a[0])/(b[1]-a[1])
            if x < edge:
                result = not result
    return result


def validate_polygon_route(route, ring):
    # 以线段与全部边界的交点切分，再检查每段中点；狭窄凹口不会被采样跳过。
    edges = list(zip(ring, ring[1:]+ring[:1]))
    for a, b in zip(route, route[1:]):
        cuts = [0.0, 1.0]
        for c, d in edges:
            cuts.extend(segment_intersections(a, b, c, d))
        cuts = sorted(set(cuts))
        for f in cuts[:1]+[(x+y)/2 for x, y in zip(cuts, cuts[1:])]+cuts[-1:]:
            p = (a[0]+(b[0]-a[0])*f, a[1]+(b[1]-a[1])*f)
            if not inside(p, ring):
                raise ValueError('生成路线有点或线段超出配置的可跑区域')


def validate_output_polygon(data, ring):
    if ring is not None:
        points = [tuple(map(float, p['point'].split(','))) for p in data['pointsList']]
        validate_polygon_route(points, ring)


# ---------------------------------------------------------------- 生成入口
def generate(config_path):
    """读 cfg 文件并生成上传任务表（CLI / 交互菜单入口）。"""
    path = Path(config_path).resolve()
    cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    return generate_cfg(cfg, path.parent)


def generate_cfg(cfg, base_dir):
    """按 cfg 生成上传任务表（CLI 与 web 预览共用同一入口）。

    三类随机偏移（默认各 ±10%，围绕**来源轨迹的实测值**）：
      * 配速 `pace_min_km`：来源总体配速（来源无节奏字段时用 cfg 的 pace_min_km）× (1+U(-o,o))
      * 步频 `cadence_spm`：来源总体步频（同上退回 cfg 的 cadence_spm）× (1+U(-o,o))
      * 总里程：来源总里程 × (1+U(-o,o))；若给了 `distance_m` 则精确用该值、不再抽签
    一致性规则（这是"三个参数互相算得出来"的关键）：
      * 采样是「等时间间隔 + 等里程增量」：n = round(target/(interval×速度))，
        Δm = target/n，Δt = sample_seconds，duration = n×interval → 每点配速
        interval/Δm 恒等于总体配速 duration/60/总里程；runMileage/runTime 严格等差，
        发送间隔与里程、配速三者自洽（舍入误差 < 1 个采样间隔）。
      * 步频同理：runStep = round(t×cadence/60)，末点步数×60/总时长 ≈ cadence。
      * 几何弦长（地图上量出来的折线长）比 target 短 1~3%（折返尖峰所致），
        在 metadata.geometry_chord_m 如实给出，不参与里程判定。
    里程区间：`min_distance_m`/`max_distance_m` 给定时，抽到的总里程必须落在区间内，
    否则换种子重抽；连续 MAX_ATTEMPTS(5) 次不满足直接报错，不静默放宽区间。
    """
    if not isinstance(cfg, dict):
        raise ValueError("路线配置必须为 JSON 对象")
    # 引擎分派：缺省仍是 deform（旧 cfg 不改一个字就继续可用）；manual 见文件末尾。
    engine = cfg.get("engine", "deform")
    if engine == "manual":
        return manual_route(cfg)
    if engine != "deform":
        raise ValueError(f"未知 engine：{engine!r}（可选 deform / manual）")
    legacy = sorted(set(cfg) & LEGACY_V4_KEYS)
    if legacy:
        raise ValueError(f"V4 合成参数已移除：{legacy}；请改用 source_json + deform_profile"
                         "（见 README §3.4，旧 cfg 需按新键改写）")
    allowed = {"engine", "source_json", "coordinate_system", "distance_m", "pace_min_km", "cadence_spm",
               "sample_seconds", "seed", "allowed_polygon_geojson", "deform_profile",
               "min_distance_m", "max_distance_m", "pace_offset_pct", "cadence_offset_pct",
               "length_offset_pct"}
    if cfg.keys()-allowed:
        raise ValueError(f"未知路线配置项：{sorted(cfg.keys()-allowed)}")
    if cfg.get("coordinate_system") not in ("GCJ-02", "WGS84"):
        raise ValueError("必须明确 coordinate_system 为 GCJ-02 或 WGS84；本工具不转换坐标系")
    if cfg.get('seed') == 'auto':
        cfg = dict(cfg, seed=secrets.randbits(63))
    cfg = dict(cfg, seed=number(cfg, 'seed', 31337, 0, 2**63-1, True))
    profile = profile_of(cfg)
    source = cfg.get('source_json')
    if not isinstance(source, str) or not source:
        raise ValueError('必须提供 source_json：tasklist / GeoJSON / 坐标列表任一格式')
    source_path = Path(source)
    if not source_path.is_absolute():
        source_path = base_dir/source
    points, metrics = load_source(source_path)
    interval = number(cfg, "sample_seconds", 1, 1, 5, True)
    # 基准值：cfg 里显式写了的优先（用于按学校规则固定配速/步频），没写的才取来源
    # 轨迹的实测总体值（这就是"和原轨迹偏移 ±10%"的基准）。
    base_pace = number(cfg, "pace_min_km", metrics["pace_min_km"] or 6.0, 2, 30)
    base_cadence = number(cfg, "cadence_spm", metrics["cadence_spm"] or 160, 1, 350)
    pace_offset = number(cfg, "pace_offset_pct", 0.10, 0, 0.5)
    cadence_offset = number(cfg, "cadence_offset_pct", 0.10, 0, 0.5)
    length_offset = number(cfg, "length_offset_pct", 0.10, 0, 0.5)
    exact = cfg.get("distance_m")
    if exact is not None:
        exact = number(cfg, "distance_m", 0, 10, 50000)
    low = cfg.get("min_distance_m")
    high = cfg.get("max_distance_m")
    low = number(cfg, "min_distance_m", 0, 10, 50000) if low is not None else None
    high = number(cfg, "max_distance_m", 0, 10, 50000) if high is not None else None
    if low is not None and high is not None and low > high:
        raise ValueError("min_distance_m 不能大于 max_distance_m")
    if exact is not None and ((low is not None and exact < low) or (high is not None and exact > high)):
        raise ValueError(f"distance_m={exact:.0f} 不在给定里程区间 [{low}, {high}] 内")
    polygon_path = cfg.get('allowed_polygon_geojson')
    ring = None
    if polygon_path:
        if not isinstance(polygon_path, str):
            raise ValueError('allowed_polygon_geojson 必须是文件路径')
        ring = allowed_polygon(base_dir/polygon_path)
    drawing = exact is None and (length_offset > 0 or low is not None or high is not None)
    attempts = MAX_ATTEMPTS if drawing else 1
    last = ""
    for attempt in range(1, attempts+1):
        # 每次重抽都要换几何种子与独立的抽签流：+1000003 保证相邻尝试不相似，
        # 抽签流用另一套混合常数，避免"换种子却抽到同一组偏移"。
        seed = cfg['seed'] + (attempt-1)*1000003
        rng = random.Random((cfg['seed'] ^ ((attempt*0x9E3779B1) & 0xFFFFFFFFFFFFFFFF)) & 0x7FFFFFFFFFFFFFFF)
        factor = 1.0 if exact is not None else 1+rng.uniform(-length_offset, length_offset)
        target = exact if exact is not None else round(metrics["length_m"]*factor, 3)
        pace = base_pace*(1+rng.uniform(-pace_offset, pace_offset))
        cadence = base_cadence*(1+rng.uniform(-cadence_offset, cadence_offset))
        # 长度靠形变自己的长度校准（length_gain_pct）实现：要变长就把校准抬到
        # factor-1（+1% 余量保证可用长度足够），要变短则保持校准并截断尾部。
        gain = min(0.5, max(0.0, factor-1.0) + (0.01 if factor > 1.0 else 0.0))
        profile_k = dict(profile, length_gain_pct=gain)
        route = deform_points(points, seed, profile_k)
        lengths = cumulative(route)
        if target > lengths[-1]:
            last = f"目标 {target:.1f} 米超过生成几何可用长度 {lengths[-1]:.1f} 米（形变后可用长度）"
            continue
        if low is not None and target < low:
            last = f"抽到总里程 {target:.1f} 米低于下限 {low:.0f} 米"
            continue
        if high is not None and target > high:
            last = f"抽到总里程 {target:.1f} 米高于上限 {high:.0f} 米"
            continue
        # 等时间间隔 + 等里程增量：三个参数互相自洽（见函数 docstring）
        n = max(1, int(round(target/(interval*1000.0/(pace*60.0)))))
        step_m = target/n
        duration = n*interval
        mileage = [i*step_m for i in range(n+1)]      # 等增量，不做逐点舍入
        sampled = [position_at(route, lengths, m) for m in mileage]
        if ring is not None:
            validate_polygon_route(sampled, ring)
        effective_pace = duration/60.0/(target/1000.0)
        speed_text = client_speed(step_m, interval)
        rows = []
        for i, (t, p, m) in enumerate(zip(range(0, duration+1, interval), sampled, mileage)):
            rows.append({"point": f"{p[0]:.8f},{p[1]:.8f}", "runMileage": m,
                         "runTime": t, "runStep": round(t*cadence/60),
                         "speed": speed_text if i else '0.0', "runStatus": "1",
                         "isFence": "Y", "isMock": False, "ts": "0"})
        data = {"pointsList": rows, "duration": duration, "recordMileage": target/1000,
                "recodeCadence": rows[-1]["runStep"]*60/duration,
                "recodePace": effective_pace,
                "recodeDislikes": 0, "manageList": []}
        validate_output_polygon(data, ring)
        chord_m = cumulative(sampled)[-1]
        return {"code": 200, "metadata": {
            "synthetic": True, "engine": "deform", "coordinate_system": cfg["coordinate_system"],
            "seed": seed, "source": source_path.name,
            "source_length_m": round(metrics["length_m"], 3),
            "source_pace_min_km": None if metrics["pace_min_km"] is None else round(metrics["pace_min_km"], 3),
            "source_cadence_spm": None if metrics["cadence_spm"] is None else round(metrics["cadence_spm"], 3),
            "pace_min_km": round(effective_pace, 3), "cadence_spm": cadence,
            "interval_s": interval, "step_m": round(step_m, 6), "target_m": target,
            "geometry_chord_m": round(chord_m, 3), "attempt": attempt, "attempts_limit": MAX_ATTEMPTS,
            "distance_window_m": [low, high] if (low is not None or high is not None) else None,
            "geometry_spacing_m": profile["spacing_m"], "deform_profile": profile,
            "server_verified": False}, "data": data}
    if attempts == 1:
        raise ValueError(last or "目标里程无法生成")
    span = metrics["length_m"]*length_offset
    hint = (f"来源总里程 {metrics['length_m']:.0f} 米 ±{length_offset:.0%} = "
            f"[{metrics['length_m']-span:.0f}, {metrics['length_m']+span:.0f}] 米")
    if low is not None and high is not None:
        window = max(0.0, min(high, metrics["length_m"]+span)-max(low, metrics["length_m"]-span))
        hint += f"，与区间交集约占 {window/(2*span)*100:.0f}%" if span > 0 else ""
    raise ValueError(f"连续 {attempts} 次重抽都没能落在给定里程区间 "
                     f"[{low if low is not None else '-'}, {high if high is not None else '-'}] 内（最后一次：{last}）。"
                     f"{hint}；可放宽区间、换来源表、改 seed，或用 distance_m 精确指定总里程")


def geojson(task):
    return {"type": "FeatureCollection", "metadata": task["metadata"], "features": [
        {"type": "Feature", "properties": {"duration_s": task["data"]["duration"]},
         "geometry": {"type": "LineString", "coordinates": [
             list(map(float, p["point"].split(','))) for p in task["data"]["pointsList"]]}}]}


# ============================================================ manual 引擎
# 手动打点（anchor）→ 保锚点平滑抖动 → 上传任务表。
#
# 与 deform 的分工：
#   * deform 形变一条**已有轨迹**，不保证经过任何指定点（只保证形状像人跑）；
#   * manual 只吃用户在地图上点的少量锚点（可含服务端打卡点），先按 5~7m 线性
#     插值补密，再叠加平滑抖动场；抖动在锚点附近被夹到 anchor_tolerance_m 以内
#     （默认 5m，0 才是逐字穿过），离锚点远了才放开到正常幅度——所以路线是
#     "从打点附近经过"，而不是每个打点上都硬拐一下。
#
# 抖动 = 沿法线的 OU 游走（跨道摆动）+ 二维相关漂移（定位噪声），两者都乘同一个
# 窗函数；单步位移另有 0.35×spacing 的斜率限幅，避免相邻点反向折返成尖刺。
#
# 纯本地计算：不读账号、不联网。时间是按配速从**几何里程**推出来的整数秒
# （runTime 严格递增），因此逐点 speed 与总体配速自洽。
MANUAL_LIMITS = {
    "spacing_m": (6.0, 4.0, 7.0),            # 用户要求约 5~7m 一点
    "jitter_sigma_m": (1.20, 0.0, 6.0),      # 法向 OU 游走强度
    "jitter_tau_m": (80.0, 5.0, 2000.0),     # 法向相关长度
    "jitter_max_m": (3.00, 0.0, 8.0),        # 法向硬限幅
    "drift_sigma_m": (0.60, 0.0, 4.0),       # 二维漂移强度
    "drift_tau_m": (150.0, 5.0, 2000.0),     # 二维漂移相关长度
    "anchor_fade_m": (15.0, 3.0, 60.0),      # 锚点附近收敛半径
    "anchor_tolerance_m": (5.0, 0.0, 20.0),  # 打点处允许的偏移（0=严格穿过打点）
}
MANUAL_DEFAULT = {k: v[0] for k, v in MANUAL_LIMITS.items()}

MANUAL_CFG_KEYS = set(MANUAL_DEFAULT) | {
    "engine", "coordinate_system", "anchors", "checkin_points", "pace_min_km",
    "cadence_spm", "seed", "distance_m",
    # 里程要求（面板从服务端任务 + config 偏移算出来后注入；也可写进 cfg 供 --route-config 复现）
    "min_distance_m", "max_distance_m", "auto_loop", "max_laps",
}

# 首尾相接判定：最后一点离第一点不超过该距离，就当作"闭合一圈"。
CLOSE_TOL_M = 30.0
# 自动补圈上限：防止把 100m 的小圈补成 50 圈（也没必要，正常场地一圈 300~800m）。
MAX_AUTO_LAPS = 12

# 打卡点与生成点的匹配距离：只有路线**真的**含该顶点才算命中，不按"经过附近"记账。
CHECKIN_MATCH_M = 1.5


def manual_profile_of(cfg):
    """读取 manual 抖动参数（缺省用 MANUAL_DEFAULT，越界报错）。"""
    profile = {}
    for key, (default, low, high) in MANUAL_LIMITS.items():
        profile[key] = number(cfg, key, default, low, high) if key in cfg else default
    return profile


def points_from_raw(value, key="anchors", minimum=2):
    """把 ``["lon,lat" | [lon,lat], ...]`` 校验成 ``[(lon,lat), ...]``。

    相邻重复点（同一位置多次点击 / 吸附到已有锚点）直接忽略，不当作独立锚点。
    """
    if value is None:
        value = []
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{key} 必须是点列表")
    out = []
    for i, item in enumerate(value):
        if isinstance(item, str):
            parts = item.split(",")
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            parts = list(item[:2])
        else:
            raise ValueError(f"{key}[{i}] 必须是 'lon,lat' 字符串或 [lon,lat]")
        try:
            lon, lat = float(parts[0]), float(parts[1])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key}[{i}] 坐标不是数字") from exc
        if not (math.isfinite(lon) and math.isfinite(lat)):
            raise ValueError(f"{key}[{i}] 坐标不是有限数")
        if not -180.0 <= lon <= 180.0 or not -90.0 <= lat <= 90.0:
            raise ValueError(f"{key}[{i}] 经纬度越界")
        if out and distance(out[-1], (lon, lat)) < 0.5:
            continue
        out.append((lon, lat))
    if len(out) < minimum:
        raise ValueError(f"{key} 至少需要 {minimum} 个不重复的点")
    return out


def _densify(xy, spacing_m):
    """投影坐标折线 -> (等距点列, 锚点下标)。锚点逐点保留，不会被插值丢掉。"""
    out = [xy[0]]
    anchors = [0]
    for a, b in zip(xy, xy[1:]):
        seg = math.dist(a, b)
        n = max(1, int(round(seg/spacing_m)))
        for k in range(1, n):
            f = k/n
            out.append((a[0]+(b[0]-a[0])*f, a[1]+(b[1]-a[1])*f))
        out.append(b)
        anchors.append(len(out)-1)
    return out, anchors


def _smoothstep(d, fade):
    """0→1 的平滑窗：d=fade 处斜率为 0，锚点处值与其导数都为 0（不起折角）。"""
    if fade <= 1e-9:
        return 1.0
    if d >= fade:
        return 1.0
    t = max(0.0, d/fade)
    return t*t*(3.0-2.0*t)


def _manual_build(anchors, profile, seed):
    """锚点 -> (点列, 诊断)。抖动在锚点附近收敛到容差内（默认不要求逐字穿过）。"""
    spacing = float(profile["spacing_m"])
    tol = float(profile["anchor_tolerance_m"])
    fwd, inv = local_projection(anchors)
    xy = [fwd(p) for p in anchors]
    dense, anchor_idx = _densify(xy, spacing)
    ss = cumulative_xy(dense)
    anchor_s = [ss[i] for i in anchor_idx]
    rng = random.Random(int(seed) ^ 0x51F3)
    lane = ou_distance(rng, ss, profile["jitter_tau_m"], profile["jitter_sigma_m"])
    dx = ou_distance(rng, ss, profile["drift_tau_m"], profile["drift_sigma_m"])
    dy = ou_distance(rng, ss, profile["drift_tau_m"], profile["drift_sigma_m"])
    # 远离锚点时允许的最大偏移；保证 >= 2*tol，锚点处才夹得住
    far = max(profile["jitter_max_m"]*2.0, tol*2.0, 1e-6)
    load = 0.35*spacing
    ox = oy = 0.0
    max_off = 0.0
    out = []
    for i, (p, s) in enumerate(zip(dense, ss)):
        j = bisect.bisect_left(anchor_s, s)
        near = [anchor_s[k] for k in (j-1, j) if 0 <= k < len(anchor_s)]
        w = _smoothstep(min(abs(s-x) for x in near), profile["anchor_fade_m"])
        _, nrm = tangent_normal(dense, i)
        lane_i = max(-profile["jitter_max_m"], min(profile["jitter_max_m"], lane[i]))
        tx = nrm[0]*lane_i+dx[i]
        ty = nrm[1]*lane_i+dy[i]
        # 指数跟随 + 单步限幅：抖动不会在相邻两点之间反向折返成尖刺。
        ox += max(-load, min(load, tx-ox))
        oy += max(-load, min(load, ty-oy))
        # 锚点处把偏移夹到「容差」以内，远离锚点逐步放开（tol=0 等价于严格穿过）。
        cap = tol + (far-tol)*w
        mag = math.hypot(ox, oy)
        if mag > cap:
            scale = 0.0 if cap <= 0.0 else cap/mag
            ox *= scale
            oy *= scale
        max_off = max(max_off, math.hypot(ox, oy))
        out.append(inv((p[0]+ox, p[1]+oy)))
    points = [(round(lon, 8), round(lat, 8)) for lon, lat in out]
    dev = max((distance(points[i], anchors[k]) for k, i in enumerate(anchor_idx)),
              default=0.0)
    return points, {"spacing_m": spacing, "max_offset_m": round(max_off, 3),
                    "anchor_count": len(anchors),
                    "anchor_max_deviation_m": round(dev, 3)}


def manual_geometry(anchors, *, seed=31337, profile=None):
    """公开的纯几何 API：锚点 -> 抖动后的点列（不做里程/时间，不生成任务表）。"""
    merged = manual_profile_of(profile or {})
    points, _ = _manual_build(points_from_raw(anchors), merged, int(seed))
    return points


def snap_anchors(anchors, checkins, tolerance_m=25.0):
    """把落在折线 tolerance_m 内的打卡点插进锚点序列（按路径顺序）。

    返回 ``(anchors2, report)``。插入的是打卡点本身，因此后续抖动会在该点归零——
    "经过打卡点"由几何保证，而不是事后标注。
    """
    base = list(anchors)
    if not checkins:
        return base, {"inserted": [], "skipped": []}
    fwd, _ = local_projection(base+list(checkins))
    xy = [fwd(p) for p in base]
    dense, idx_anchor = _densify(xy, 2.0)
    ss = cumulative_xy(dense)
    anchor_s = [ss[i] for i in idx_anchor]
    items = [(anchor_s[k], tuple(base[k]), "anchor") for k in range(len(base))]
    inserted, skipped = [], []
    for c in checkins:
        q = fwd(c)
        best_i, best_d = 0, float("inf")
        for i, p in enumerate(dense):
            d = math.dist(p, q)
            if d < best_d:
                best_d, best_i = d, i
        s = ss[best_i]
        if best_d > tolerance_m:
            skipped.append({"point": c, "distance_m": round(best_d, 1),
                            "reason": "离折线超过吸附半径"})
            continue
        if any(abs(s-x) < 1.0 for x in anchor_s):
            skipped.append({"point": c, "distance_m": round(best_d, 1),
                            "reason": "已与锚点重合"})
            continue
        if any(abs(s-x[0]) < 1.0 for x in items if x[2] == "checkin"):
            continue
        items.append((s, tuple(c), "checkin"))
        inserted.append({"point": c, "distance_m": round(best_d, 1)})
    items.sort(key=lambda x: x[0])
    return [x[1] for x in items], {"inserted": inserted, "skipped": skipped}


def distance_to_route(points, target):
    """点到折线的最短距离（米）与最近线段起点下标。"""
    if not points:
        return float("inf"), 0
    if len(points) == 1:
        return distance(points[0], target), 0
    fwd, _ = local_projection([target]+list(points))
    q = fwd(target)
    xy = [fwd(p) for p in points]
    best, best_i = float("inf"), 0
    for i, (a, b) in enumerate(zip(xy, xy[1:])):
        vx, vy = b[0]-a[0], b[1]-a[1]
        wx, wy = q[0]-a[0], q[1]-a[1]
        vv = vx*vx+vy*vy
        t = 0.0 if vv <= 1e-12 else max(0.0, min(1.0, (wx*vx+wy*vy)/vv))
        px, py = a[0]+t*vx, a[1]+t*vy
        d = math.hypot(q[0]-px, q[1]-py)
        if d < best:
            best, best_i = d, i
    return best, best_i


def match_checkins(points, checkins, tolerance_m=None):
    """打卡点是否被"经过"：按到折线的**最短距离**判定，不要求路线里有同一个顶点。

    打点本身也允许偏移（anchor_tolerance_m），所以"经过"的判据必须是距离而非顶点相等。
    """
    tol = CHECKIN_MATCH_M if tolerance_m is None else float(tolerance_m)
    hits = []
    for c in checkins:
        d, idx = distance_to_route(points, c)
        ok = d <= tol
        hits.append({"point": c, "distance_m": round(d, 3),
                     "index": idx if ok else None, "matched": ok})
    return hits


def _manual_timeline(points, pace_min_km, cadence_spm):
    """几何里程 -> 整数秒时间轴（严格递增）与累计步数。"""
    lengths = cumulative(points)
    rows = []
    prev_t = -1
    for i, m in enumerate(lengths):
        t = int(round(m/1000.0*pace_min_km*60.0))
        if i > 0 and t <= prev_t:
            t = prev_t+1
        prev_t = t
        rows.append((m, t, int(math.floor(t*cadence_spm/60.0))))
    return rows, lengths


def manual_route(cfg):
    """manual 引擎入口：锚点配置 -> 与 deform 同构的任务表（``generate_cfg`` 分派）。"""
    legacy = sorted(set(cfg) & LEGACY_V4_KEYS)
    if legacy:
        raise ValueError(f"V4 合成参数已移除：{legacy}；manual 引擎请用 anchors + 抖动参数")
    unknown = sorted(set(cfg)-MANUAL_CFG_KEYS)
    if unknown:
        raise ValueError(f"未知的 manual 配置项：{unknown}")
    coordinate_system = cfg.get("coordinate_system")
    if coordinate_system not in ("GCJ-02", "WGS84"):
        raise ValueError("必须明确 coordinate_system 为 GCJ-02 或 WGS84；本工具不转换坐标系")
    anchors = points_from_raw(cfg.get("anchors"), "anchors", minimum=2)
    checkin_coords = points_from_raw(cfg.get("checkin_points") or [], "checkin_points", minimum=0)
    raw_checkins = [c if isinstance(c, str) else f"{c[0]:.6f},{c[1]:.6f}"
                    for c in (cfg.get("checkin_points") or [])]
    profile = manual_profile_of(cfg)
    if cfg.get("seed") == "auto":
        # 52 位以内：JSON 数字能原样穿过浏览器（JS 的 number 只有 2^53 精度）
        seed = secrets.randbits(52)
    else:
        seed = number(cfg, "seed", 31337, 0, 2**63-1, True)
    pace = number(cfg, "pace_min_km", 5.0, 2.0, 30.0)
    cadence = number(cfg, "cadence_spm", 170.0, 60.0, 260.0)
    min_m = (number(cfg, "min_distance_m", 0.0, 0.0, 200000.0)
             if cfg.get("min_distance_m") not in (None, "") else None)
    max_m = (number(cfg, "max_distance_m", 0.0, 0.0, 200000.0)
             if cfg.get("max_distance_m") not in (None, "") else None)
    auto_loop = bool(cfg.get("auto_loop", True))
    max_laps = (number(cfg, "max_laps", MAX_AUTO_LAPS, 1, 60, True)
                if cfg.get("max_laps") is not None else MAX_AUTO_LAPS)

    # 首尾相接：把最后一点对齐到第一点，多圈拼接处才不留缝。
    closed = len(anchors) >= 4 and distance(anchors[0], anchors[-1]) <= CLOSE_TOL_M
    if closed:
        anchors = anchors[:-1] + [anchors[0]]
    ring = anchors[:-1] if closed else anchors

    def build(laps):
        """一次成型：把环线重复 laps 遍再抖——同一段 OU 过程跨圈连续，接缝不跳。"""
        chain = (ring*laps + [ring[0]]) if closed else anchors
        return _manual_build(chain, profile, seed)

    points, diag = build(1)
    laps = 1
    base_len = cumulative(points)[-1]
    # 闭合圈且不够里程 → 沿同一条环线继续跑；同一次生成里每圈的抖动天然不同。
    if closed and auto_loop and min_m and base_len > 0:
        need = min(max_laps, max(1, int(math.ceil(min_m/base_len))))
        if need > 1:
            points, diag = build(need)
            laps = need
            while cumulative(points)[-1] + 1e-9 < min_m and laps < max_laps:
                laps += 1
                points, diag = build(laps)
    rows, lengths = _manual_timeline(points, pace, cadence)
    total = lengths[-1]
    lap_length = total/laps if laps else total
    gap_m = max(0.0, (min_m or 0.0) - total)
    over_m = max(0.0, total - max_m) if max_m else 0.0
    if total < 50.0:
        raise ValueError("生成的轨迹不足 50 米：请多打几个点，或把点分散一些")
    duration = rows[-1][1]
    if duration <= 0:
        raise ValueError("生成时长为 0：检查配速与里程")
    exact = cfg.get("distance_m")
    if exact is not None:
        target = number(cfg, "distance_m", 0.0, 10.0, 50000.0)
        if abs(total-target) > max(5.0, 0.02*target):
            raise ValueError(
                f"生成几何长度 {total:.1f} 米与 distance_m={target:.0f} 差超过 2%："
                "可调整打点或去掉 distance_m（manual 引擎不缩放几何，避免破坏锚点）")

    # 打点允许偏移，所以"经过打卡点"也按距离判（容差至少取打点容差）
    checkin_tol = max(CHECKIN_MATCH_M, float(profile["anchor_tolerance_m"]))
    hits = match_checkins(points, checkin_coords, checkin_tol)
    manage_list = []
    for i, hit in enumerate(hits):
        manage_list.append({"point": raw_checkins[i] if i < len(raw_checkins) else "",
                            "marked": "Y" if hit["matched"] else "N",
                            "index": str(hit["index"]) if hit["matched"] else ""})

    points_list = []
    for i, ((m, t, steps), p) in enumerate(zip(rows, points)):
        speed = '0.0' if i == 0 else client_speed(m-rows[i-1][0], t-rows[i-1][1])
        points_list.append({
            "point": f"{p[0]:.8f},{p[1]:.8f}", "runMileage": m, "runTime": t,
            "runStep": steps, "speed": speed, "runStatus": "1",
            "isFence": "Y", "isMock": False, "ts": "0",
        })
    data = {
        "pointsList": points_list, "duration": duration,
        "recordMileage": total/1000.0,
        "recodeCadence": points_list[-1]["runStep"]*60.0/duration,
        "recodePace": duration/60.0/(total/1000.0),
        "recodeDislikes": sum(1 for m in manage_list if m["marked"] == "Y"),
        "manageList": manage_list,
    }
    metadata = {
        "synthetic": True, "engine": "manual", "coordinate_system": coordinate_system,
        "seed": seed, "seed_text": str(seed),
        "anchors": len(anchors), "points": len(points_list),
        "spacing_m": round(diag["spacing_m"], 3),
        "mean_step_m": round(total/max(1, len(points_list)-1), 3),
        "geometry_chord_m": round(total, 3), "target_m": round(total, 3),
        "pace_min_km": round(duration/60.0/(total/1000.0), 3), "cadence_spm": cadence,
        "interval_s": round(duration/max(1, len(points_list)-1), 3),
        "max_offset_m": diag["max_offset_m"],
        # 打点不再要求逐字穿过：给出容差与"实际最大偏离"
        "anchor_error_m": diag["anchor_max_deviation_m"],
        "anchor_max_deviation_m": diag["anchor_max_deviation_m"],
        "anchor_tolerance_m": float(profile["anchor_tolerance_m"]),
        "checkin_tolerance_m": checkin_tol,
        "jitter": dict(profile),
        # 补圈与里程要求（面板据此判断"够不够要求"，approve 时会再核一次）
        "closed": closed, "laps": laps, "lap_length_m": round(lap_length, 3),
        "min_distance_m": min_m, "max_distance_m": max_m,
        "mileage_gap_m": round(gap_m, 1), "mileage_over_m": round(over_m, 1),
        "checkin_total": len(checkin_coords),
        "checkin_matched": sum(1 for h in hits if h["matched"]),
        "checkins": hits,
        "server_verified": False,
    }
    return {"code": 200, "metadata": metadata, "data": data}
