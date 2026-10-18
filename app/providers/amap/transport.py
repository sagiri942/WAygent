"""Amap generation-day route estimates; requests are injected for offline tests."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import math
import os
import re
from urllib.parse import urlencode

from app.core.http import http_get_json_async
from app.core.transport import TransportOption, TransportUnavailable, yuan_cents
from app.providers.amap.walking import _coordinate

_CITY_CODES = {}


async def _request_payload(url, request, before_request):
    for attempt in range(3):
        if request is http_get_json_async:
            payload = await request(url, timeout=8, before_attempt=before_request)
        else:
            if before_request is not None:
                await before_request()
            payload = await request(url, timeout=8)
        if str(payload.get("infocode")) == "10021" and attempt < 2:
            await asyncio.sleep(attempt + 1)
            continue
        return payload


async def _citycode(city, key, query_date, request, before_request):
    if re.fullmatch(r"\d{3,4}", str(city)):
        return str(city)
    cache_key = (str(city), str(query_date))
    if request is http_get_json_async and cache_key in _CITY_CODES:
        return _CITY_CODES[cache_key]
    url = "https://restapi.amap.com/v3/geocode/geo?" + urlencode({"address": city, "key": key})
    payload = await _request_payload(url, request, before_request)
    values = payload.get("geocodes") or []
    code = str(values[0].get("citycode") or "") if values else ""
    if str(payload.get("status")) != "1" or not re.fullmatch(r"\d{3,4}", code):
        raise TransportUnavailable("公共交通缺少可核验城市编码")
    if request is http_get_json_async:
        if len(_CITY_CODES) >= 128:
            _CITY_CODES.clear()
        _CITY_CODES[cache_key] = code
    return code


def _minutes(value):
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise TransportUnavailable("供应商缺少有效耗时") from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise TransportUnavailable("供应商缺少有效耗时")
    return math.ceil(seconds / 60)


def _fare(value):
    try:
        return yuan_cents(value)
    except ValueError:
        return None


def _duration(part):
    return (part.get("cost") or {}).get("duration") or part.get("duration")


def _service_minute(value):
    text = str(value or "").replace(":", "")
    if len(text) != 4 or not text.isdigit():
        return None
    hour, minute = int(text[:2]), int(text[2:])
    return hour * 60 + minute if hour < 24 and minute < 60 else None


def _transit_option(plan, allowed_modes, queried_at, departure_min):
    segments, used, fares = [], set(), []
    elapsed = 0
    for segment in plan.get("segments") or []:
        if segment.get("railway") or segment.get("taxi"):
            return None
        walking = segment.get("walking") or {}
        if _duration(walking) and float(_duration(walking)) > 0:
            elapsed += _minutes(_duration(walking))
            segments.append({"mode": "walking", "minutes": _minutes(_duration(walking)),
                             "instructions": [step.get("instruction", "") for step in walking.get("steps") or []]})
        lines = (segment.get("bus") or {}).get("buslines") or []
        if lines:
            # buslines are alternatives for this segment, not sequential rides.
            eligible = []
            for line in lines[:1]:
                kind = str(line.get("type") or "")
                if "地铁" in kind or "轻轨" in kind:
                    mode = "subway"
                elif kind in {"普通公交线路", "公交线路", "专线公交线路", "旅游专线", "快速公交"}:
                    mode = "bus"
                else:
                    continue
                if mode in allowed_modes:
                    eligible.append((line, mode))
            if not eligible:
                return None
            line, mode = eligible[0]
            first, last = _service_minute(line.get("start_time")), _service_minute(line.get("end_time"))
            boarding = (departure_min + elapsed) % 1440
            if first is not None and last is not None:
                active = first <= boarding <= last if first <= last else boarding >= first or boarding <= last
                if not active:
                    return None
            used.add(mode)
            segments.append({"mode": mode, "name": line.get("name", ""),
                             "minutes": _minutes(_duration(line)),
                             "departure_stop": (line.get("departure_stop") or {}).get("name"),
                             "arrival_stop": (line.get("arrival_stop") or {}).get("name"),
                             "start_time": line.get("start_time"), "end_time": line.get("end_time")})
            fares.append(_fare((segment.get("cost") or {}).get("transit_fee", (line.get("cost") or {}).get("transit_fee"))))
            elapsed += _minutes(_duration(line))
    if not used:
        return None
    cost = _fare((plan.get("cost") or {}).get("transit_fee"))
    if cost is None and fares and all(fare is not None for fare in fares):
        cost = sum(fares)
    mode = next(iter(used)) if len(used) == 1 else "transit"
    return TransportOption(mode=mode, minutes=_minutes((plan.get("cost") or {}).get("duration")),
                           cost_cents=cost, segments=segments, queried_at=queried_at)


async def query_transport_options(origin, destination, *, mode, city, departure_min, query_date,
                                  allowed_modes, taxi_wait_min=5, taxi_boarding_min=3,
                                  api_key=None, request=http_get_json_async, before_request=None):
    key = (api_key if api_key is not None else os.getenv("AMAP_API_KEY", "")).strip()
    if not key:
        raise TransportUnavailable("高德服务未配置")
    endpoint = {"walking": "walking", "taxi": "driving", "transit": "transit/integrated"}[mode]
    params = {"origin": _coordinate(origin), "destination": _coordinate(destination),
              "key": key, "show_fields": "cost", "output": "JSON"}
    if mode == "transit":
        city_code = await _citycode(city, key, query_date, request, before_request)
        params.update(city1=city_code, city2=city_code, strategy=5 if "subway" not in allowed_modes else 8,
                      date=query_date.isoformat(), time=f"{departure_min // 60}-{departure_min % 60:02d}",
                      AlternativeRoute=10)
    elif mode == "taxi":
        params.update(strategy=38)
    else:
        params.update(alternative_route=3)
    url = f"https://restapi.amap.com/v5/direction/{endpoint}?{urlencode(params)}"
    payload = await _request_payload(url, request, before_request)
    if str(payload.get("status")) != "1" or str(payload.get("infocode")) != "10000":
        raise TransportUnavailable("高德未返回可用交通方案")
    queried_at = datetime.now(timezone.utc).isoformat()
    route = payload.get("route") or {}
    options = []
    for plan in route.get("transits" if mode == "transit" else "paths") or []:
        try:
            if mode == "transit":
                option = _transit_option(plan, allowed_modes, queried_at, departure_min)
            else:
                buffer = taxi_wait_min + taxi_boarding_min if mode == "taxi" else 0
                option = TransportOption(mode=mode, minutes=_minutes((plan.get("cost") or {}).get("duration")) + buffer,
                    cost_cents=_fare(route.get("taxi_cost")) if mode == "taxi" else 0,
                    buffer_min=buffer, queried_at=queried_at,
                    segments=[{"mode": mode, "minutes": _minutes((plan.get("cost") or {}).get("duration"))}])
            if option is not None:
                options.append(option)
        except (ValueError, TypeError, KeyError):
            continue
    if not options:
        raise TransportUnavailable("该交通方式没有完整可用方案")
    return options
