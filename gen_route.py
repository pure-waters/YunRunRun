# -*- coding: utf-8 -*-
"""手动生成简化 tasklist：6 个打卡点跑 7 圈，不依赖高德 API。"""
import json
import os

# 6 个打卡点（服务端下发的顺序）
points = [
    "112.417622,34.612324",  # A 起点
    "112.417453,34.612929",  # B
    "112.417372,34.613208",  # C
    "112.418287,34.613408",  # D
    "112.418368,34.613128",  # E
    "112.418457,34.612537",  # F
]

# 相邻打卡点之间的距离（米），按 A→B→C→D→E→F→A 顺序
dists = [69, 32, 87, 32, 66, 80]

# 配速：300 s/km = 5 min/km
pace = 300

# 跑 7 圈
laps = 7

points_list = []
cum_d = 0.0
cum_t = 0.0

# 第一个点：起点，里程 0，时间 0
points_list.append({
    "point": points[0],
    "runStatus": "1",
    "speed": "5.00",
    "isFence": "Y",
    "isMock": False,
    "runMileage": 0.0,
    "runTime": 0,
})

for lap in range(laps):
    # 依次经过 B、C、D、E、F
    for i in range(1, 6):
        d = dists[i - 1]
        cum_d += d
        cum_t += d * pace / 1000
        points_list.append({
            "point": points[i],
            "runStatus": "1",
            "speed": "5.00",
            "isFence": "Y",
            "isMock": False,
            "runMileage": round(cum_d, 1),
            "runTime": int(round(cum_t)),
        })
    # 回到起点 A
    d = dists[5]
    cum_d += d
    cum_t += d * pace / 1000
    points_list.append({
        "point": points[0],
        "runStatus": "1",
        "speed": "5.00",
        "isFence": "Y",
        "isMock": False,
        "runMileage": round(cum_d, 1),
        "runTime": int(round(cum_t)),
    })

total_km = cum_d / 1000
total_s = int(round(cum_t))
recode_pace = total_s / 60 / total_km

task = {
    "msg": "操作成功",
    "code": 200,
    "data": {
        "recordMileage": round(total_km, 2),
        "recodePace": round(recode_pace, 2),
        "recodeCadence": 174,
        "recodeDislikes": 6,
        "duration": total_s,
        "pointsList": points_list,
        "schoolId": 197,
        # index 必须对应服务端 getHomeRunInfo 下发 points 的顺序（2026-10-31 实测）：
        # 服务端顺序 = A, F, C, D, E, B（map.json 里是 A,B,C,D,E,F）
        "manageList": [
            {"point": p, "marked": "Y", "index": str(i)}
            for i, p in enumerate([points[0], points[5], points[2],
                                   points[3], points[4], points[1]])
        ],
    },
}

os.makedirs("tasks_ly", exist_ok=True)
with open("tasks_ly/tasklist_0.json", "w", encoding="utf-8") as f:
    json.dump(task, f, ensure_ascii=False, indent=2)

print(f"生成完成: {total_km:.2f}km, {total_s}s, {len(points_list)} 点")