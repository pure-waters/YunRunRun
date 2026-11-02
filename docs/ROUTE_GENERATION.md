# 轨迹合成模式（deform）

此模式把一条**已有轨迹**形变成一条新的二维轨迹，并补齐上传所需的时间、累计距离、步数与配速。它不识别地图、不做路径规划，也不判断道路是否可通行：输出形状完全由来源轨迹 + 形变参数 + 种子决定。底图和参数不能证明实际运动发生，也不能保证服务端接受或成绩长期有效。

算法（`yun_route.py`）：按弧长把来源重采样成 5.5m 等间距折线 → 沿法线做 OU 低频游走（跨道摆动）→ 叠加 x/y 二维相关漂移（定位噪声尺度）→ 叠加稀疏、指数衰减的偶发大偏移（约 1.2 次/km）→ 可选把几何长度校准到来源的 +3%。该实现与历史演示包同源，回归基准钉在 `tests/test_yun_route.py::test_deform_matches_source_scale`（洛阳来源 `tests/fixtures/deform/ly_source.json`：2.56 km → 467 点 / 2.637 km）。

## 离线生成与配置

仅生成文件，无须账号，也不会读取 `config.ini`：

```powershell
python tools/generate_route.py --config examples/routes/my_route.json --output work_dir/route_preview.geojson
```

输出 GeoJSON 和同名 `route_preview.task.json`（逐点与汇总检查用，`ts=0` 是尚未运行的占位符）。输出文件已存在时会拒绝覆盖。可用支持 GeoJSON 的本地查看器预览，不要把调试文件放入 `tasks_*` 后通过旧打表入口运行。

`examples/routes/deform_*.json` 是配置模板。`source_json` 等相对**路线配置文件所在目录**解析；命令行 `--route-config` 相对**启动命令时所在目录**解析。

| 配置项 | 示例/默认 | 含义 |
|---|---|---|
| `source_json` | `../../tasks_ly/tasklist_0.json` | 形变来源，**任选**：tasklist（`data.pointsList[*].point`）、GeoJSON LineString/FeatureCollection、裸坐标列表 `[[lon,lat],…]` |
| `coordinate_system` | `GCJ-02` | 必须明确声明；不执行坐标转换。离线工具也支持 WGS84，当前主流程要求 GCJ-02 |
| `distance_m` | 不设置 | **精确**总里程（米）：给了就钉住该值、不再抽签；不得超过形变后可用长度 |
| `min_distance_m` / `max_distance_m` | 不设置 | 里程区间：不给 `distance_m` 时总里程 = 来源里程 ×(1±`length_offset_pct`) 抽签，必须落在区间内，否则换种子重抽，连续 5 次不中直接报错 |
| `pace_offset_pct` / `cadence_offset_pct` / `length_offset_pct` | 0.10 | 三个参数各自的抽签幅度（0~0.5）；设 0 关闭抽签 |
| `pace_min_km` | 来源实测值 | 配速基准：**写了以 cfg 为准**，没写则取来源轨迹实测总体配速；来源无节奏字段时用 6.0 |
| `cadence_spm` | 来源实测值 | 步频基准：同上；来源步频越界（合理区间 110~220 步/分）或字段缺失时退回 160 |
| `sample_seconds` | 1 | 发送间隔，整数 1～5 秒；与里程、配速严格自洽（每点里程 = 间隔 ÷ 速度） |
| `seed` | 31337 | 固定种子可复现；填 `"auto"` 每次新种子，实值写入 `metadata.seed`；重抽时按 `seed + 1000003×n` 递进 |
| `allowed_polygon_geojson` | 不设置 | 可选的单环 GeoJSON Polygon 围栏：给了就逐段校验，不给则不校验 |
| `deform_profile` | 不设置 | 覆盖形变参数（下表），未知键与越界值都会报错 |

### 三个参数的一致性规则

总里程、配速、步频各自**独立抽签**（默认各 ±10%），但生成出的点列必须自洽，不能逐点乱偏：

- 采样采用「等时间间隔 + 等里程增量」：`n = round(总里程 / (间隔 × 速度))`、`Δm = 总里程 / n`、
  `Δt = sample_seconds`、`duration = n × 间隔`。因此**每一点的配速（间隔/Δm）恒等于总体配速
  （总时长 / 总里程）**，`runMileage` 与 `runTime` 都严格等差，发送间隔与里程、配速三者
  互相算得出来（舍入误差 < 1 个采样间隔）。
- 步频同理：`runStep = round(t × 步频 / 60)`，末点步数 × 60 / 总时长 ≈ 名义步频。
- 地图上量到的折线弦长比总里程短 1~3%（形变的折返尖峰所致），在 `metadata.geometry_chord_m`
  如实给出；它不参与里程判定（判定用 `recordMileage`，即等差里程序列的末值 = 抽到的总里程）。

`deform_profile` 可覆盖的参数（默认值 = 演示包尺度 / 下界 / 上界）：

| 参数 | 默认 | 范围 | 含义 |
|---|---|---|---|
| `spacing_m` | 5.5 | 1–50 | 输出等弧长重采样间距 |
| `lane_sigma_m` / `lane_tau_m` | 2.8 / 280 | 0–20 / 10–5000 | 法向 OU 游走强度 / 相关长度（米） |
| `max_lane_m` | 5 | 0–20 | 法向游走硬限幅 |
| `drift_sigma_m` / `drift_tau_m` | 4.2 / 200 | 0–20 / 10–5000 | 二维定位漂移强度 / 相关长度 |
| `jump_sigma_m` / `jump_rate_per_km` / `jump_decay` | 14 / 1.2 / 0.90 | 0–60 / 0–20 / 0.5–1.0 | 偶发大偏移幅值 / 频次 / 逐点衰减 |
| `length_gain_pct` | 0.03 | 0–0.5 | 几何长度校准（相对来源；设 0 关闭） |
| `start_trim_m` / `end_trim_m` | 0 / 0 | 0–20000 | 按弧长从头/尾裁剪来源 |

注意：**2026-10 起 V4 引擎（`base_geojson`/`base_task`/`max_offset_m`/`lane_change_*`/`detour_*`/`telemetry_task` 等）已移除**。旧配置会得到明确的迁移报错（`V4 合成参数已移除：…`），不会静默按新语义执行。原 V4 的参数语义、复现基准与历史说明见 git 历史中的本文件旧版本。

`allowed_polygon_geojson` 需要由使用者按真实可通行范围提供，不能把简单矩形外包框当作跑道边界；它只证明"生成点都在给定多边形内"，不证明学校服务端围栏与该文件一致。当前只支持一个无洞多边形，不支持多区域与跨围栏桥接。

## 接入主流程

先用本地测试账号配置和本地 `getHomeRunInfo` fixture 做假传输演练：

```powershell
python main.py --dry-run -f tests/fixtures/test_config.ini --dry-home examples/routes/dry_home.json --route-config examples/routes/my_route.json
```

这条命令不联网、不登录、不等待真实跑步时长。fixture 仅用于演练，不代表学校实际要求。正式入口：

```powershell
python main.py -f config.ini --route-config examples/routes/my_route.json
```

交互模式（`python main.py` 登录后选"轨迹来源 → 合成轨迹"）等价于上面的 `--route-config`，菜单直接列出 `examples/routes/*.json`。`--route-config` 不能与 `-t` 或 `-d` 混用。路线文件不会自动写回账号配置。

生成与几何输入校验在账号配置读取、登录和建记录之前完成。取得学校任务后、`start` 前检查里程、步频和坐标系。**要求踩点的任务（`raDislikes > 0`）暂不支持，会停止；不会虚构踩点列表。** 人脸准入、拒绝停止以及结束时的 `isStandard → 尾批 → finish` 链沿用现有实现。任务下发的 `raSingleMileageMax` 会在到达上限时截断末点并进入结束链。

## 一致性与限制

- 时间采样按 `sample_seconds` 均匀推进，里程为等增量序列（`Δm = 总里程/n`），`speed` 字段实际是配速 min/km（APK `SportRunMapActivity.java:4140` 公式，1～900 两位小数）；汇总里程按公里、逐点里程按米。因为里程取的是等增量而非弦长累加，不再存在"时间采样导致几何长度损失"的问题。
- 形变的偶发大偏移会形成折返尖峰，地图上量到的折线弦长会比 `recordMileage` 短 1~3%（加密顶点无法改变，折返是路径本身的性质），故 `metadata.geometry_chord_m` 单独给出，供人工核对。
- 里程抽签失败（连续 5 次不落在给定区间）会直接报错，并在消息里给出"来源里程 ± 抽签幅度"的可达范围与区间交集占比，以及"可用 `distance_m` 精确指定"的提示；不会静默放宽区间。
- 几何不足（目标超过形变后可用长度）直接报错，不缩放场地、不首尾瞬移拼接；围栏不给就不校验，给了则逐段检查（含狭窄凹口）。
- 逐点等待、批次上传；网络与人脸处理延时纳入后续点的时间与配速，不做突发追赶。批次大小在服务端返回 `passPointNum` 时按 APK 规则确定（小于 10 回退 60，缓存严格超过阈值再发）；否则用 `[Run] split_count`。
- 同一种子 + 同一来源 + 同一 profile = 完全相同的结果；换种子或换来源才会改变形状与三个参数。**重复使用同一来源与种子仍可能保留可识别的相似性**，不代表规避服务端复审。
- 离线验证覆盖：演示基准复现、三参数抽签幅度与三向自洽、里程区间重抽与失败、来源步频闸门、配置校验（未知键/越界 profile/区间倒挂）、三种来源等价、围栏校验、种子可复现、网络延时、学校约束、生成失败不读取账号、尾批结束链。它不能证明实机人脸通过、记录有效或不会追溯取消。

---

# 手动打点引擎（manual）

`deform` 是把一条**已有轨迹**形变出新形状（不保证经过任何指定点）。`manual` 解决另一个问题：
用户在图上只点少量**锚点**（自己定的路 + 服务端打卡点），要一条**从这些锚点附近经过**、点间平滑抖动、
像真人跑出来的轨迹。默认允许偏离打点 5 米以内（`anchor_tolerance_m`），而不是要求每个点上都硬拐一下。

配置里用 `"engine": "manual"` 选择；不写 `engine` 的旧配置继续走 `deform`，一个字都不用改。

```json
{
  "engine": "manual",
  "coordinate_system": "GCJ-02",
  "anchors": [[112.417622, 34.612324], [112.420000, 34.612324], [112.420000, 34.615000]],
  "checkin_points": ["112.420000,34.612324"],
  "spacing_m": 6.0, "pace_min_km": 5.0, "cadence_spm": 174, "seed": 31337
}
```

| 配置项 | 默认 / 范围 | 含义 |
|---|---|---|
| `anchors` | 必填，≥2 点 | 打点（`"lon,lat"` 或 `[lon,lat]`）。相邻重复点自动忽略 |
| `checkin_points` | 可空 | 服务端下发的打卡点（原样字符串）。只用于核对与 `manageList`，不改变几何 |
| `coordinate_system` | 必填 | `GCJ-02` / `WGS84`，不转换。主流程要求 GCJ-02 |
| `spacing_m` | 6.0（4.0～7.0） | 相邻打点之间线性补点的间距 |
| `jitter_sigma_m` / `jitter_tau_m` / `jitter_max_m` | 1.2 / 80 / 3.0 | 法向 OU 游走强度 / 相关长度 / 硬限幅 |
| `drift_sigma_m` / `drift_tau_m` | 0.6 / 150 | 二维定位漂移强度 / 相关长度 |
| `anchor_fade_m` | 15（3～60） | 锚点附近收敛半径（该半径内偏移被压到容差内） |
| `anchor_tolerance_m` | **5**（0～20） | 打点处允许的最大偏移；`0` = 严格穿过打点 |
| `pace_min_km` / `cadence_spm` | 5.0 / 170 | 配速与步频；时间由配速从几何里程推出 |
| `seed` | 31337 或 `"auto"` | 同 seed + 同参数 = 完全相同的点列；`"auto"` 每次新种子（面板默认） |
| `distance_m` | 不设置 | 只做**校验**：几何长度与它相差 >2% 直接报错（引擎不缩放几何，缩放会破坏锚点） |
| `min_distance_m` / `max_distance_m` | 不设置 | 里程要求（米）。面板按「服务端区间 + config.ini 偏移」注入，前端改不了 |
| `auto_loop` | `true` | 首尾相接且不足 `min_distance_m` 时，沿环线继续跑，直到够里程 |
| `max_laps` | 12 | 补圈上限：防止把几十米的小圈补成几十圈 |

## 算法

1. **补点**：每两个相邻打点之间按 `spacing_m` 等分线性插值；打点本身始终保留为顶点，不参与插值。
2. **抖动**：沿法线的 OU 游走 + 二维相关漂移，再被一个以「到最近打点的弧长距离」为自变量的
   `smoothstep` 窗调幅：打点附近把合成偏移**夹到 `anchor_tolerance_m` 以内**（默认 5m），
   离开 `anchor_fade_m` 之后放开到正常幅度。所以路线是"贴着打点过"，不是"在打点上钉死"；
   `anchor_tolerance_m = 0` 才回到严格穿过（打点处位移为 0）。
3. **限幅**：法向游走被 `jitter_max_m` 夹住，单步位移另有 `0.35 × spacing_m` 的斜率限幅，
   相邻两点不会反向折返成尖刺；短打点段自动收敛（抖不起来）。
4. **时间**：里程 = 抖动后折线的累计几何长度；`runTime` 由 `里程/配速` 推出并取整为**严格递增**的整数秒，
   `runStep = floor(t × 步频 / 60)`，`speed` 逐点由 `Δm/Δt` 反算（min/km）。

与 deform 的差别要在心里记住：deform 的里程是**等增量序列**（几何弦长单独给出），manual 的里程是
**真实几何累计**，所以 manual 的逐点配速会有整数秒量化带来的一点点起伏，而总配速仍然准确。

## 里程要求与自动补圈

里程要求来自「服务端 `raSingleMileageMin/Max` + `config.ini` 的 `single_mileage_*_offset`」，
面板算好之后**注入引擎**（`min_distance_m` / `max_distance_m`），前端无法改小它——否则"必须满足跑步要求"就成了摆设。

- **首尾相接**（末点距首点 ≤30m，`CLOSE_TOL_M`）视为一圈：末点会被对齐到首点，多圈拼接处不留缝。
- 一圈不够 `min_distance_m` 时，沿同一条环线继续跑，**每圈换一个种子**（`seed + 1000003×圈号`），
  所以每圈的抖动都不一样，不是同一条线复制好几遍。
- 补到 `max_laps`（默认 12）圈仍不够 → 预览如实报 `mileage_gap_m`；**审核（落盘）会拒绝**，并提示"把单圈画长一点"。
- 未闭合（开放路线）且不够里程 → 预览照常出图但报缺口，落盘同样拒绝，提示"首尾相接可自动补圈，或继续打点"。
- 超过 `max_distance_m` → 预览警告（`mileage_over_m`）：`main` 在跑动中到达上限会截断末点，路线尾部会被切掉。
- 元数据里给出 `laps` / `lap_length_m` / `closed` / `mileage_gap_m` / `mileage_over_m`，面板直接展示。

## 打卡点与 manageList

- 吸附：`snap_anchors()` 把落在折线 `tolerance_m`（面板默认 25m）内的打卡点按弧长位置**插入锚点序列**，
  插入的是打卡点本身——所以"经过打卡点"是几何保证，不是事后标注。
- 匹配：按**到折线的最短距离**判定（容差 = `max(1.5m, anchor_tolerance_m)`）——路线从打卡点
  `≤5m` 处经过就记 `marked="Y"` 并写最近顶点下标；否则 `marked="N"`、`index=""`。
  `recodeDislikes` = 命中数，**不虚构踩点**。
- `main._validate_generated_route` 仍会拒绝 `raDislikes > 0` 的任务（走 `--route-config` 时）——
  这条既有策略没有为了 manual 放宽。

## 面板工作流（`python web.py`）

| 步骤 | 操作 | 说明 |
|---|---|---|
| ① | 「读取任务与打卡点」 | 复用 `main.set_args` + `YunClient` 调 `getHomeRunInfo`（**只读**，不建记录）；401 会提示先登录 |
| ① | 「账号密码登录」 | 复用 `tools.Login` + `apply_login_result` + `persist_session`，等价 `main.py --login`；会使手机端会话失效 |
| ② | **地图上方**「搜索并定位」 | 高德 Web 服务（POI → 地址解析），返回 **GCJ-02** 坐标，和底图/打卡点同一坐标系；需要 `[User] map_key` |
| ② | **左键**点击打点 / 拖动编号点微调 / 撤销 / 清空 | 打点按顺序连成参考线；服务端打卡点是红色圆点，围栏是橙色虚线 |
| ② | **右键按住拖动**地图 | 左键留给打点（Leaflet 默认的左键平移已关闭）；双击=放大且不会误加点（240ms 防抖） |
| ③ | 「生成预览」 | 参数：点间距 / 配速 / 步频 / **每次随机种子** / 抖动强度 / 漂移 / 锚点收敛；返回里程、圈数、里程要求、缺口、打卡点命中数、警告 |
| ④ | 勾选「人工审核通过」→「生成 my_route.json」 | 成功写两个文件（见下），并把 ③ 的命令切成**模式1回放**；里程不够/超出要求会拒绝 |
| ⑤ | 「去执行（模式1回放）」 | 跳到执行日志，dry run 默认勾选；取消勾选才真实发送 |

产物：

| 文件 | 作用 |
|---|---|
| `examples/routes/<名字>.json` | `engine: "manual"` 路线配置，`main.py --route-config` 可直接读，也出现在交互菜单里 |
| `work_dir/web_send/manual_<名字>/tasklist_0.json` | 由同一份配置复算出的打表任务表，供**模式1 打表回放**（`-a -t <目录> --task-file`） |

一致性护栏：预览返回 `preview_sha256`（点列指纹），审核时重算比对；落盘的 cfg 会**再复算一遍**并与预览比对，
不一致就删掉刚写的文件并拒绝生成回放命令——"看到什么就发什么"。

## 限制（同样不是"绕过"承诺）

- 不判断道路可通行性、不做坐标系转换；打点本身代表"你要跑的路"，引擎只负责"让它像人跑"。
- 抖动幅度是**视觉与统计尺度**上的近似，不是真人步态模型；服务端还有配速分布、请求节奏、TLS/HTTP 指纹等字段之外的检查。
- `distance_m` 只校验不缩放：想凑某个里程请调整打点，而不是让引擎把几何揉到目标长度。
- 自动补圈只对**闭合**路线生效：开放路线补不了圈（会绕回到起点），只能继续打点或改成首尾相接。
- 补圈的目标是"够下限"，所以最终里程会落在 `下限 ～ 下限+一圈` 之间；单圈过长时可能越过上限，此时只能把单圈画短。
- 手动打点仍受既有闸门约束：`raDislikes > 0` 的任务在 `--route-config` 路径会停；人脸任务需要照片/视频源。

