"""规划结果的路线优化、POI 搜索与手动编辑路由。"""

from __future__ import annotations

from app.planning.revision_scope import local_revision_day

from itertools import permutations

from fastapi import APIRouter, Header, HTTPException

from app.core.auth import decode_token
from app.core.cache import POI_TTL, get_cached, poi_cache_key, set_cached
from app.core.itinerary_storage import itinerary_connection
from app.core.memory import (
    itinerary_owner,
    load_itinerary,
    update_plan_json,
)
from app.planning.helpers import amap_key, haversine_km, restaurant_to_dict
from app.planning.optimizer import meal_coverage_for_visit
from app.planning.restaurant_enrichment import (
    generate_restaurant_enrichment,
    load_restaurant_enrichment,
    route_fingerprint,
    save_restaurant_enrichment,
)
from app.planning.shadow_profiles import record_route_edit
from app.providers.amap.poi import (
    ATTRACTION_TYPE,
    normalize_address,
    poi_to_spot,
    search_around_pois,
    search_city_pois,
)
from pydantic import BaseModel

router = APIRouter()


class RestaurantEnrichmentRequest(BaseModel):
    refresh: bool = False


@router.post("/api/plan/{plan_id}/restaurant-enrichment")
def create_restaurant_enrichment(
    plan_id: str,
    req: RestaurantEnrichmentRequest,
    authorization: str | None = Header(default=None),
):
    """Explicit, independent restaurant task; never mutates core plan_json."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="需要登录")
    user_id = decode_token(authorization[7:])
    if not user_id:
        raise HTTPException(status_code=401, detail="token 无效或已过期")
    with itinerary_connection() as conn:
        owner = itinerary_owner(plan_id, conn)
        if not owner:
            raise HTTPException(status_code=404, detail="行程不存在")
        if owner != user_id:
            raise HTTPException(status_code=403, detail="无权访问")
        data = load_itinerary(plan_id, conn)
        plan = data["plan"]
        fingerprint = route_fingerprint(plan)
        existing = load_restaurant_enrichment(plan_id, user_id, fingerprint, conn)
        local_day = local_revision_day(data.get("planner_state") or {})
        existing_days = (existing or {}).get("days") or []
        target_cached = local_day is not None and any(
            int(day.get("day") or 0) == local_day for day in existing_days
        )
        if existing and not req.refresh and (local_day is None or target_cached):
            return {"enrichment": existing, "cached": True}
        generation_plan = (
            {**plan, "days": [
                day for day in plan.get("days") or []
                if int(day.get("day") or 0) == local_day
            ]}
            if local_day is not None else plan
        )
    payload = generate_restaurant_enrichment(
        generation_plan, api_key=amap_key(), search_around=search_around_pois
    )
    if local_day is not None:
        payload["route_fingerprint"] = fingerprint
        with itinerary_connection() as conn:
            latest = load_restaurant_enrichment(plan_id, user_id, fingerprint, conn)
            preserved = [
                dict(day) for day in (latest or {}).get("days") or []
                if int(day.get("day") or 0) != local_day
            ]
            target = [
                dict(day) for day in payload.get("days") or []
                if int(day.get("day") or 0) == local_day
            ]
            payload["days"] = sorted(preserved + target, key=lambda day: int(day.get("day") or 0))
            save_restaurant_enrichment(plan_id, user_id, payload, conn)
    else:
        with itinerary_connection() as conn:
            save_restaurant_enrichment(plan_id, user_id, payload, conn)
    return {"enrichment": payload, "cached": False}


@router.get("/api/plan/{plan_id}/restaurant-enrichment")
def get_restaurant_enrichment(
    plan_id: str,
    authorization: str | None = Header(default=None),
):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="需要登录")
    user_id = decode_token(authorization[7:])
    if not user_id:
        raise HTTPException(status_code=401, detail="token 无效或已过期")
    with itinerary_connection() as conn:
        owner = itinerary_owner(plan_id, conn)
        if not owner:
            raise HTTPException(status_code=404, detail="行程不存在")
        if owner != user_id:
            raise HTTPException(status_code=403, detail="无权访问")
        data = load_itinerary(plan_id, conn)
        payload = load_restaurant_enrichment(
            plan_id, user_id, route_fingerprint(data["plan"]), conn
        )
    return {"enrichment": payload}


# ─── 路线优化（暴力枚举最短路径）────────────────────────────


def _path_km(spots: list[dict]) -> float:
    """按顺序计算景点列表的总行驶路程（km）。"""
    total = 0.0
    for i in range(len(spots) - 1):
        a = spots[i].get("location")
        b = spots[i + 1].get("location")
        if a and b:
            total += haversine_km(a, b)
    return total


def _optimize_day_timeline(timeline: list[dict]) -> tuple[list[dict], float, float]:
    """
    对单天 timeline 做路线优化：
    - 暴力枚举 daytime attractions 的全排列（evening 景点固定末位）
    - 路程目标只计算景点（daytime + evening）之间的距离，meals 不参与路程评分
      → 原始排列是候选项之一，保证 best_km ≤ original_km，不会越优化越差
    - meals 保持原始相对位置（排在第几个 daytime 景点之后），在最终 timeline 中插回
    - 重算 dist_from_prev_km
    返回 (optimized_timeline, original_km, optimized_km)
    """
    daytime = [t for t in timeline if t["type"] == "attraction" and t.get("period") != "evening"]
    evening = [t for t in timeline if t["type"] == "attraction" and t.get("period") == "evening"]
    lunch   = next((t for t in timeline if t["type"] == "lunch"), None)
    dinner  = next((t for t in timeline if t["type"] == "dinner"), None)

    # 景点不足两个，无需枚举
    if len(daytime) < 2:
        return timeline, _path_km(timeline), _path_km(timeline)

    # ── 记录午/晚餐在原始 timeline 中的相对位置 ─────────────────
    # lunch_after = 在它之前已出现的 daytime 景点数（0 = 排在第 1 个景点之前）
    lunch_after = dinner_after = len(daytime)   # 默认：排在所有 daytime 景点之后
    daytime_seen = 0
    for item in timeline:
        if item["type"] == "attraction" and item.get("period") != "evening":
            daytime_seen += 1
        elif item["type"] == "lunch" and lunch_after == len(daytime):
            lunch_after = daytime_seen
        elif item["type"] == "dinner" and dinner_after == len(daytime):
            dinner_after = daytime_seen

    def build_sequence(perm: list[dict]) -> list[dict]:
        """按原始相对位置插入 lunch/dinner，构建完整序列（含 evening）。"""
        seq: list[dict] = []
        lunch_inserted = dinner_inserted = False

        # 餐厅排在第 1 个景点之前（*_after == 0）
        if lunch and lunch_after == 0:
            seq.append(lunch)
            lunch_inserted = True
        if dinner and dinner_after == 0:
            seq.append(dinner)
            dinner_inserted = True

        for i, spot in enumerate(perm):
            seq.append(spot)
            if lunch and not lunch_inserted and i + 1 == lunch_after:
                seq.append(lunch)
                lunch_inserted = True
            if dinner and not dinner_inserted and i + 1 == dinner_after:
                seq.append(dinner)
                dinner_inserted = True

        seq.extend(evening)

        # 未插入的餐厅补到末尾
        if lunch and not lunch_inserted:
            seq.append(lunch)
        if dinner and not dinner_inserted:
            seq.append(dinner)

        return seq

    # 原始路程（只计算景点 daytime + evening，餐厅不参与评分）
    original_km = _path_km(daytime + evening)

    # 暴力枚举 daytime 全排列，只用景点序列评估路程
    # 原始排列也在候选内，故 best_km ≤ original_km 恒成立
    best_perm = list(daytime)
    best_km   = original_km
    for perm in permutations(daytime):
        km = _path_km(list(perm) + evening)
        if km < best_km - 1e-9:
            best_km = km
            best_perm = list(perm)

    # 按最优排列构建结果 timeline（各项做浅拷贝）
    result: list[dict] = [dict(item) for item in build_sequence(best_perm)]

    # ── 重算 dist_from_prev_km ──────────────────────────────────
    for i in range(len(result)):
        if i == 0:
            result[i].pop("dist_from_prev_km", None)
        else:
            prev_loc = result[i - 1].get("location")
            cur_loc  = result[i].get("location")
            if prev_loc and cur_loc:
                result[i]["dist_from_prev_km"] = round(haversine_km(prev_loc, cur_loc), 2)
            else:
                result[i].pop("dist_from_prev_km", None)

    # ── 按位置交换时段：原 daytime 第 i 个时段赋给优化后第 i 个 daytime 景点 ──
    # evening 景点和 meals 保留原始时间不动
    time_slots = [
        {"start_time": t.get("start_time"), "end_time": t.get("end_time"), "period": t.get("period")}
        for t in timeline
        if t["type"] == "attraction" and t.get("period") != "evening"
    ]
    slot_idx = 0
    for item in result:
        if item["type"] == "attraction" and item.get("period") != "evening":
            if slot_idx < len(time_slots):
                item["start_time"] = time_slots[slot_idx]["start_time"]
                item["end_time"]   = time_slots[slot_idx]["end_time"]
                item["period"]     = time_slots[slot_idx]["period"]
                slot_idx += 1

    # A moved food street must not retain stale coverage from its old slot.
    for item in result:
        if item.get("type") != "attraction":
            continue
        try:
            start_h, start_m = str(item.get("start_time") or "").split(":", 1)
            end_h, end_m = str(item.get("end_time") or "").split(":", 1)
            start = int(start_h) * 60 + int(start_m)
            end = int(end_h) * 60 + int(end_m)
        except (ValueError, AttributeError):
            item["meal_coverage"] = None
            continue
        item["meal_coverage"] = meal_coverage_for_visit(
            str(item.get("meal_scene") or "none"), start, end
        )

    return result, original_km, best_km


class OptimizeDayRequest(BaseModel):
    plan_id: str
    day: int   # 1-based，第几天


@router.post("/api/plan/optimize_day")
def optimize_day(req: OptimizeDayRequest, authorization: str | None = Header(default=None)):
    """对行程中某一天的景点顺序做暴力枚举最优化（最短路程），evening 景点固定末位。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="需要登录")
    user_id = decode_token(authorization[7:])
    if not user_id:
        raise HTTPException(status_code=401, detail="token 无效或已过期")

    with itinerary_connection() as conn:
        # 校验所有权
        owner = itinerary_owner(req.plan_id, conn)
        if not owner:
            raise HTTPException(status_code=404, detail="行程不存在")
        if owner != user_id:
            raise HTTPException(status_code=403, detail="无权访问")

        data = load_itinerary(req.plan_id, conn)

    plan = data["plan"]
    expected_lock_version = data["lock_version"]
    days = plan.get("days", [])

    # 找对应天（day 字段 1-based）
    day_obj = next((d for d in days if d.get("day") == req.day), None)
    if not day_obj:
        raise HTTPException(status_code=400, detail=f"第 {req.day} 天不存在")

    timeline = day_obj.get("timeline", [])
    optimized_timeline, original_km, optimized_km = _optimize_day_timeline(timeline)

    # 原地更新 plan 并写回 DB
    day_obj["timeline"] = optimized_timeline
    with itinerary_connection() as conn:
        ok = update_plan_json(
            req.plan_id, user_id, plan, conn,
            expected_lock_version=expected_lock_version,
        )
        if ok:
            record_route_edit(req.plan_id, plan, conn)
    if not ok:
        raise HTTPException(status_code=409, detail="行程已被其他操作更新，请刷新后重试")

    return {
        "optimized_day": day_obj,
        "original_km":   round(original_km, 2),
        "optimized_km":  round(optimized_km, 2),
        "improved":      optimized_km < original_km - 0.05,
    }


class RevertDayRequest(BaseModel):
    plan_id: str
    day: int            # 1-based
    original_timeline: list[dict]


@router.post("/api/plan/revert_day")
def revert_day(req: RevertDayRequest, authorization: str | None = Header(default=None)):
    """将某天路线回退到优化前的顺序（前端传入原始 timeline）。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="需要登录")
    user_id = decode_token(authorization[7:])
    if not user_id:
        raise HTTPException(status_code=401, detail="token 无效或已过期")

    with itinerary_connection() as conn:
        owner = itinerary_owner(req.plan_id, conn)
        if not owner:
            raise HTTPException(status_code=404, detail="行程不存在")
        if owner != user_id:
            raise HTTPException(status_code=403, detail="无权访问")

        data = load_itinerary(req.plan_id, conn)

    plan = data["plan"]
    expected_lock_version = data["lock_version"]
    day_obj = next((d for d in plan.get("days", []) if d.get("day") == req.day), None)
    if not day_obj:
        raise HTTPException(status_code=400, detail=f"第 {req.day} 天不存在")

    day_obj["timeline"] = req.original_timeline
    with itinerary_connection() as conn:
        ok = update_plan_json(
            req.plan_id, user_id, plan, conn,
            expected_lock_version=expected_lock_version,
        )
        if ok:
            record_route_edit(req.plan_id, plan, conn)
    if not ok:
        raise HTTPException(status_code=409, detail="行程已被其他操作更新，请刷新后重试")

    return {"reverted_day": day_obj}


# ─── 手动编辑：POI 搜索代理 ──────────────────────────────────


@router.get("/api/poi/search")
def poi_search(
    city: str,
    kw: str,
    kind: str = "attraction",
    authorization: str | None = Header(default=None),
):
    """手动换点/加点的搜索代理：高德 Key 不出服务端，结果走 Redis 缓存。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="需要登录")
    if not decode_token(authorization[7:]):
        raise HTTPException(status_code=401, detail="token 无效或已过期")
    if kind not in ("attraction", "restaurant"):
        raise HTTPException(status_code=400, detail="kind 须为 attraction 或 restaurant")

    # 输入防御：清洗 city/kw 并检查长度
    city = city.strip()
    kw = kw.strip().replace("\n", "").replace("\r", "").replace("\x00", "")
    if not city or not kw:
        raise HTTPException(status_code=400, detail="city 和 kw 不能为空")
    if len(city) > 50 or len(kw) > 100:
        raise HTTPException(status_code=400, detail="搜索词过长")

    types = ATTRACTION_TYPE if kind == "attraction" else "餐饮服务"
    cache_key = poi_cache_key(
        city, kw, types=types, offset=8, response_kind=f"manual-photos-v2:{kind}",
    )
    cached = get_cached(cache_key)
    if cached is not None:
        return {"results": cached}

    try:
        raw = search_city_pois(city, amap_key(), keywords=kw, types=types, offset=8)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))

    results: list[dict] = []
    for poi in raw:
        parsed = poi_to_spot(poi) if kind == "attraction" else restaurant_to_dict(poi)
        if not parsed:
            continue
        if kind == "attraction":
            # poi_to_spot 不含地址，搜索结果需要地址帮用户分辨同名地点
            parsed["address"] = normalize_address(poi.get("address"))
        results.append(parsed)

    set_cached(cache_key, results, POI_TTL)
    return {"results": results}


# ─── 手动编辑：保存逐天 timeline ─────────────────────────────────


def _valid_location(loc) -> bool:
    """location 必须是含数值 lat/lng 的 dict，残缺对象不参与距离计算。"""
    return (
        isinstance(loc, dict)
        and isinstance(loc.get("lat"), (int, float))
        and isinstance(loc.get("lng"), (int, float))
    )


def _recalc_dists(timeline: list[dict]) -> None:
    """服务端重算相邻条目距离，不信任前端传入的 dist_from_prev_km。"""
    for i, item in enumerate(timeline):
        if i == 0:
            item.pop("dist_from_prev_km", None)
            continue
        prev_loc = timeline[i - 1].get("location")
        cur_loc = item.get("location")
        if _valid_location(prev_loc) and _valid_location(cur_loc):
            item["dist_from_prev_km"] = round(haversine_km(prev_loc, cur_loc), 2)
        else:
            item.pop("dist_from_prev_km", None)


class TimelineDayPayload(BaseModel):
    day: int                  # 1-based
    timeline: list[dict]


class SaveTimelineRequest(BaseModel):
    days: list[TimelineDayPayload]


@router.put("/api/plan/{plan_id}/timeline")
def save_timeline(
    plan_id: str,
    req: SaveTimelineRequest,
    authorization: str | None = Header(default=None),
):
    """保存手动编辑后的逐天 timeline。只合并 timeline，不允许前端覆盖 plan 其他字段。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="需要登录")
    user_id = decode_token(authorization[7:])
    if not user_id:
        raise HTTPException(status_code=401, detail="token 无效或已过期")

    with itinerary_connection() as conn:
        owner = itinerary_owner(plan_id, conn)
        if not owner:
            raise HTTPException(status_code=404, detail="行程不存在")
        if owner != user_id:
            raise HTTPException(status_code=403, detail="无权访问")
        data = load_itinerary(plan_id, conn)

    import copy
    from app.planning.manual_edit import ManualEditRejected, reconcile_manual_edit
    from app.providers.amap import poi as poi_provider

    plan = data["plan"]
    previous_plan = copy.deepcopy(plan)
    expected_lock_version = data["lock_version"]
    day_by_no = {d.get("day"): d for d in plan.get("days", [])}
    for payload in req.days:
        day_obj = day_by_no.get(payload.day)
        if not day_obj:
            raise HTTPException(status_code=400, detail=f"第 {payload.day} 天不存在")
        for item in payload.timeline:
            if not isinstance(item, dict) or not item.get("type"):
                raise HTTPException(status_code=422, detail="timeline 条目缺少 type")
            if item["type"] == "attraction" and not item.get("name"):
                raise HTTPException(status_code=422, detail="景点条目缺少 name")
            if plan.get("scoring_profile") and item["type"] != "attraction":
                raise HTTPException(status_code=422, detail="核心路线只允许 attraction；餐厅请使用独立推荐")
            if item["type"] == "attraction":
                has_times = bool(item.get("start_time") and item.get("end_time"))
                if not has_times and not plan.get("scoring_profile"):
                    item["meal_coverage"] = None
                    continue
                try:
                    start_h, start_m = str(item.get("start_time") or "").split(":", 1)
                    end_h, end_m = str(item.get("end_time") or "").split(":", 1)
                    item["meal_coverage"] = meal_coverage_for_visit(
                        str(item.get("meal_scene") or "none"),
                        int(start_h) * 60 + int(start_m),
                        int(end_h) * 60 + int(end_m),
                    )
                except (ValueError, AttributeError):
                    raise HTTPException(status_code=422, detail="景点时间格式必须为 HH:MM")
        _recalc_dists(payload.timeline)
        day_obj["timeline"] = payload.timeline

    planner_state = data.get("planner_state")
    if planner_state:
        try:
            planner_state = reconcile_manual_edit(
                plan, previous_plan, planner_state,
                search_pois=lambda name: poi_provider.search_city_pois(
                    plan.get("destination") or planner_state.get("destination") or "",
                    amap_key(), keywords=name, types="", offset=25,
                ),
            )
        except ManualEditRejected as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail="地点核验暂时不可用，请稍后重试") from exc
        for day in plan.get("days", []):
            if day.get("day") in {payload.day for payload in req.days}:
                _recalc_dists(day["timeline"])

    with itinerary_connection() as conn:
        ok = update_plan_json(
            plan_id, user_id, plan, conn,
            expected_lock_version=expected_lock_version,
            new_planner_state=planner_state,
        )
        if ok:
            record_route_edit(plan_id, plan, conn)
    if not ok:
        raise HTTPException(status_code=409, detail="行程已被其他操作更新，请刷新后重试")

    return {"plan": plan}


# ─── 周边搜索 ────────────────────────────────────────────────────


@router.get("/api/poi/nearby")
def poi_nearby(
    lat: float,
    lng: float,
    type: str,
    radius: int = 1500,
    authorization: str | None = Header(default=None),
):
    """周边 POI 搜索，按距离排序。type=风景名胜|餐饮服务，radius 最大 5000m。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="需要登录")
    if not decode_token(authorization[7:]):
        raise HTTPException(status_code=401, detail="token 无效或已过期")

    type = type.strip()
    if type not in ("风景名胜", "餐饮服务"):
        raise HTTPException(status_code=400, detail="type 须为 风景名胜 或 餐饮服务")
    if not (1 <= radius <= 5000):
        raise HTTPException(status_code=400, detail="radius 须在 1–5000 之间")

    try:
        raw_pois = search_around_pois(
            {"lat": lat, "lng": lng},
            amap_key(),
            types=type,
            radius=radius,
            offset=20,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))

    results: list[dict] = []
    for poi in raw_pois:
        parsed = poi_to_spot(poi) if type == "风景名胜" else restaurant_to_dict(poi)
        if not parsed:
            continue
        dist = poi.get("distance")
        if dist is not None:
            try:
                parsed["distance"] = int(dist)
            except (ValueError, TypeError):
                pass
        results.append(parsed)

    results.sort(key=lambda x: x.get("distance", 999999))
    return {"results": results}


# ─── 行程元数据保存（hotel / notes / day_themes） ────────────────


class MetadataRequest(BaseModel):
    hotel: str | None = None
    notes: str | None = None
    day_themes: dict[str, str] | None = None


@router.put("/api/plan/{plan_id}/metadata")
def save_plan_metadata(
    plan_id: str,
    req: MetadataRequest,
    authorization: str | None = Header(default=None),
):
    """保存 hotel / notes / day_themes（每天主题）到 final_plan JSON，不影响 timeline。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="需要登录")
    user_id = decode_token(authorization[7:])
    if not user_id:
        raise HTTPException(status_code=401, detail="token 无效或已过期")

    with itinerary_connection() as conn:
        owner = itinerary_owner(plan_id, conn)
        if not owner:
            raise HTTPException(status_code=404, detail="行程不存在")
        if owner != user_id:
            raise HTTPException(status_code=403, detail="无权访问")
        data = load_itinerary(plan_id, conn)

    plan = data["plan"]
    expected_lock_version = data["lock_version"]
    if req.hotel is not None:
        plan["hotel"] = req.hotel
    if req.notes is not None:
        plan["notes"] = req.notes
    if req.day_themes:
        day_map = {d.get("day"): d for d in plan.get("days", [])}
        for day_no_str, theme in req.day_themes.items():
            try:
                day_no = int(day_no_str)
            except ValueError:
                continue
            day_obj = day_map.get(day_no)
            if day_obj is not None:
                day_obj["theme"] = theme

    with itinerary_connection() as conn:
        ok = update_plan_json(
            plan_id, user_id, plan, conn,
            expected_lock_version=expected_lock_version,
        )
    if not ok:
        raise HTTPException(status_code=409, detail="行程已被其他操作更新，请刷新后重试")

    return {"ok": True}


# ─── 步行路线规划（高德 REST API → polyline 坐标列表） ──────────────


@router.get("/api/route/walking")
def route_walking(
    origin_lng: float,
    origin_lat: float,
    dest_lng: float,
    dest_lat: float,
    authorization: str | None = Header(default=None),
):
    """调用高德步行路线 REST API，返回解码后的坐标数组供前端绘制。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="需要登录")
    if not decode_token(authorization[7:]):
        raise HTTPException(status_code=401, detail="token 无效或已过期")

    import httpx
    key = amap_key()
    if not key:
        raise HTTPException(status_code=503, detail="未配置 AMAP_API_KEY")

    url = "https://restapi.amap.com/v3/direction/walking"
    params = {
        "key": key,
        "origin": f"{origin_lng},{origin_lat}",
        "destination": f"{dest_lng},{dest_lat}",
        "output": "json",
    }
    try:
        resp = httpx.get(url, params=params, timeout=8)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"高德请求失败: {e}")

    if data.get("status") != "1" or not data.get("route", {}).get("paths"):
        raise HTTPException(status_code=502, detail="步行路线规划失败")

    # 拼合所有 step 的 polyline，解码为 [[lng, lat], ...] 坐标列表
    path = data["route"]["paths"][0]
    coords: list[list[float]] = []
    for step in path.get("steps", []):
        for pair in step.get("polyline", "").split(";"):
            parts = pair.strip().split(",")
            if len(parts) == 2:
                try:
                    coords.append([float(parts[0]), float(parts[1])])
                except ValueError:
                    pass

    return {"coords": coords, "distance": path.get("distance"), "duration": path.get("duration")}


@router.post("/api/plan/{retired_action}", include_in_schema=False)
async def removed_plan_action(retired_action: str):
    """Prevent removed plan actions from falling through to the static mount."""
    raise HTTPException(status_code=404, detail="Not Found")
