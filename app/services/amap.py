"""高德地图 Web 服务 API 封装（异步 httpx）。

所有方法在「未配置 key / 调用失败 / 返回异常」时返回 None 或空列表，
由上层 meeting_point 服务做降级（直线距离估算）。

高德坐标格式统一为 "经度,纬度"（lng,lat）。
"""
import httpx

from ..config import settings

# 停车场 POI 搜索关键词（高德 type 码 150900 在部分区域覆盖不全，用关键词更稳）
_PARKING_KEYWORD = "停车场"


class AmapClient:
    def __init__(self, key: str | None = None) -> None:
        self.key = key if key is not None else settings.amap_api_key
        self.base_url = settings.amap_base_url.rstrip("/")
        # 有真实 key 才算可用（.env 占位符 "your_..." 视为不可用）
        self.available = bool(self.key) and not self.key.startswith("your_")
        self.last_error: str | None = None
        self._city_cache: dict[tuple[float, float], str] = {}

    async def _get(self, path: str, params: dict) -> dict | None:
        self.last_error = None
        if not self.available:
            self.last_error = "后端未加载 AMAP_API_KEY"
            return None
        params = {**params, "key": self.key}
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self.base_url}{path}", params=params)
                resp.raise_for_status()
                data = resp.json()
        except httpx.TimeoutException:
            self.last_error = "高德接口请求超时"
            return None
        except httpx.HTTPError as exc:
            self.last_error = f"高德接口网络错误：{type(exc).__name__}"
            return None
        except ValueError:
            self.last_error = "高德接口返回了无法解析的数据"
            return None
        if data.get("status") != "1":
            self.last_error = f"高德接口失败：{data.get('info') or '未知错误'}（{data.get('infocode') or '无错误码'}）"
            return None
        return data

    async def _resolve_city(self, location: tuple) -> str | None:
        """把坐标解析为公交规划可接受的城市 adcode，并做进程内缓存。"""
        cache_key = (round(float(location[0]), 4), round(float(location[1]), 4))
        if cache_key in self._city_cache:
            return self._city_cache[cache_key]
        data = await self._get(
            "/v3/geocode/regeo",
            {"location": self._ll(*location), "extensions": "base", "radius": "1000"},
        )
        try:
            component = data["regeocode"]["addressComponent"]
            raw_citycode = component.get("citycode")
            citycode = raw_citycode[0] if isinstance(raw_citycode, list) and raw_citycode else raw_citycode
            city = str(citycode or component.get("adcode") or "")
        except (TypeError, KeyError):
            city = ""
        if city:
            self._city_cache[cache_key] = city
            return city
        if not self.last_error:
            self.last_error = "无法根据起点坐标确定公交城市"
        return None

    @staticmethod
    def _ll(lat: float, lng: float) -> str:
        """(lat, lng) -> "lng,lat" 高德格式。"""
        return f"{lng},{lat}"

    # ---- 耗时计算 ----

    async def driving_duration(self, origin: tuple, dest: tuple) -> float | None:
        """单点驾车耗时（分钟）。用 /v3/distance type=1（驾车距离/耗时）。"""
        data = await self._get(
            "/v3/distance",
            {
                "origins": self._ll(*origin),
                "destination": self._ll(*dest),
                "type": "1",
            },
        )
        return self._parse_distance(data)

    async def driving_durations_batch(self, origins: list[str], dest: tuple) -> dict[int, float | None]:
        """批量驾车耗时：多个起点 -> 一个终点，返回 {起点序号: 分钟}。

        origins 是高德格式 "lng,lat" 字符串列表；序号与传入顺序对应。
        """
        data = await self._get(
            "/v3/distance",
            {
                "origins": "|".join(origins),
                "destination": self._ll(*dest),
                "type": "1",
            },
        )
        result: dict[int, float | None] = {i: None for i in range(len(origins))}
        if not data:
            return result
        for item in data.get("results", []):
            idx = int(item.get("origin_id", "1")) - 1
            dur = self._duration_to_minutes(item.get("duration"))
            if idx in result:
                result[idx] = dur
        return result

    async def transit_duration(self, origin: tuple, dest: tuple) -> float | None:
        """公交耗时（分钟）。高德无批量接口，只能逐对调用。"""
        city = await self._resolve_city(origin)
        if not city:
            return None
        data = await self._get(
            "/v3/direction/transit/integrated",
            {
                "origin": self._ll(*origin),
                "destination": self._ll(*dest),
                "city": city,
                "extensions": "base",
            },
        )
        if not data:
            return None
        try:
            route = data["route"]
            if "transits" in route and route["transits"]:
                return self._duration_to_minutes(route["transits"][0].get("duration"))
            if "paths" in route and route["paths"]:
                return self._duration_to_minutes(route["paths"][0].get("duration"))
        except (KeyError, IndexError, TypeError):
            return None
        return None

    async def route_detail(self, origin: tuple, dest: tuple, mode: str) -> dict | None:
        """返回道路路线的耗时、距离和轨迹点；失败返回 None。"""
        if mode == "driving":
            data = await self._get(
                "/v3/direction/driving",
                {
                    "origin": self._ll(*origin), "destination": self._ll(*dest),
                    "strategy": "0", "extensions": "base",
                },
            )
            try:
                path = data["route"]["paths"][0]
                raw_steps = path.get("steps", [])
                polyline = self._collect_polylines(step.get("polyline") for step in raw_steps)
                return {
                    "duration_min": self._duration_to_minutes(path.get("duration")),
                    "distance_km": round(float(path.get("distance", 0)) / 1000, 2),
                    "polyline": polyline,
                    "steps": [{
                        "type": "driving", "instruction": step.get("instruction") or "继续行驶",
                        "road": step.get("road") or "", "distance_m": self._to_int(step.get("distance")),
                        "duration_min": self._duration_to_minutes(step.get("duration")),
                    } for step in raw_steps],
                }
            except (TypeError, KeyError, IndexError, ValueError):
                return None

        city = await self._resolve_city(origin)
        cityd = await self._resolve_city(dest)
        if not city or not cityd:
            return None
        data = await self._get(
            "/v3/direction/transit/integrated",
            {
                "origin": self._ll(*origin), "destination": self._ll(*dest),
                "city": city, "cityd": cityd, "extensions": "all",
            },
        )
        try:
            route = data["route"]
            transit = route["transits"][0]
            fragments = []
            instructions = []
            for segment in transit.get("segments", []):
                walking = segment.get("walking") or {}
                for step in walking.get("steps", []):
                    fragments.append(step.get("polyline"))
                    instructions.append({
                        "type": "walking", "instruction": step.get("instruction") or "步行",
                        "road": step.get("road") or "", "distance_m": self._to_int(step.get("distance")),
                    })
                bus = segment.get("bus") or {}
                for line in bus.get("buslines", []):
                    fragments.append(line.get("polyline"))
                    instructions.append({
                        "type": "transit", "line": line.get("name") or "公交/地铁",
                        "departure_stop": (line.get("departure_stop") or {}).get("name") or "",
                        "arrival_stop": (line.get("arrival_stop") or {}).get("name") or "",
                        "via_num": self._to_int(line.get("via_num")),
                        "duration_min": self._duration_to_minutes(line.get("duration")),
                        "distance_m": self._to_int(line.get("distance")),
                    })
                railway = segment.get("railway") or {}
                if railway.get("departure_stop") and railway.get("arrival_stop"):
                    fragments.append(
                        f"{railway['departure_stop'].get('location', '')};{railway['arrival_stop'].get('location', '')}"
                    )
                    instructions.append({
                        "type": "railway", "line": railway.get("name") or railway.get("trip") or "铁路",
                        "departure_stop": railway["departure_stop"].get("name") or "",
                        "arrival_stop": railway["arrival_stop"].get("name") or "",
                    })
            return {
                "duration_min": self._duration_to_minutes(transit.get("duration")),
                "distance_km": round(float(transit.get("distance") or route.get("distance") or 0) / 1000, 2),
                "polyline": self._collect_polylines(fragments),
                "steps": instructions,
            }
        except (TypeError, KeyError, IndexError, ValueError):
            return None

    # ---- POI / 地理编码 / 停车场 ----

    async def nearby_pois(
        self,
        location: tuple,
        radius: int = 3000,
        types: str | None = None,
        keywords: str | None = None,
    ) -> list[dict]:
        """周边 POI 搜索，返回结构化候选点列表。"""
        params: dict = {
            "location": self._ll(*location),
            "radius": str(radius),
            "offset": "20",
            "page": "1",
            "extensions": "base",
        }
        if types:
            params["types"] = types
        if keywords:
            params["keywords"] = keywords
        data = await self._get("/v3/place/around", params)
        if not data:
            return []
        pois: list[dict] = []
        for p in data.get("pois", []):
            try:
                lng, lat = p["location"].split(",")
                pois.append(
                    {
                        "name": p.get("name", ""),
                        "address": p.get("address", ""),
                        "lat": float(lat),
                        "lng": float(lng),
                        "category": p.get("type", ""),
                        "poi_id": p.get("id"),
                        "source": "poi",
                    }
                )
            except (KeyError, ValueError):
                continue
        return pois

    async def parking_nearby(self, location: tuple, radius: int = 1000) -> list[dict]:
        """目的地周边停车场（返回名称/收费/步行距离估算）。"""
        pois = await self.nearby_pois(location, radius=radius, keywords=_PARKING_KEYWORD)
        lots = []
        for p in pois[:3]:
            lots.append(
                {
                    "name": p["name"],
                    "address": p.get("address", ""),
                    "fee": "收费信息以现场为准",
                    # 粗略步行估算（直线距离 / 5km/h）
                    "walk_min": round(_haversine_km(location[0], location[1], p["lat"], p["lng"]) / 5.0 * 60),
                }
            )
        return lots

    async def geocode(self, address: str) -> dict | None:
        """地址 -> 坐标。"""
        data = await self._get("/v3/geocode/geo", {"address": address})
        if not data:
            return None
        geocodes = data.get("geocodes", [])
        if not geocodes:
            return None
        lng, lat = geocodes[0]["location"].split(",")
        return {"lat": float(lat), "lng": float(lng), "formatted": geocodes[0].get("formatted_address", "")}

    # ---- 解析辅助 ----

    @staticmethod
    def _duration_to_minutes(duration) -> float | None:
        if duration is None:
            return None
        try:
            # 高德部分接口会返回 "1551.0" 这类浮点数字符串。
            return round(float(duration) / 60.0, 1)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _to_int(value) -> int | None:
        try:
            return int(float(value)) if value not in (None, "") else None
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _parse_distance(data: dict | None) -> float | None:
        if not data:
            return None
        results = data.get("results", [])
        if not results:
            return None
        return AmapClient._duration_to_minutes(results[0].get("duration"))

    @staticmethod
    def _collect_polylines(fragments) -> list[list[float]]:
        points: list[list[float]] = []
        for fragment in fragments:
            if not fragment:
                continue
            for raw in str(fragment).split(";"):
                try:
                    lng, lat = raw.split(",")
                    point = [float(lng), float(lat)]
                except (ValueError, TypeError):
                    continue
                if not points or points[-1] != point:
                    points.append(point)
        return points


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    import math

    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


# 模块级单例
amap = AmapClient()
