"""Synthetic hospital suitability model backing the local demonstration UI.

All geography and criterion values in this module are illustrative. The
network gravity score calls the same road-time helper as the analysis engine.
"""

from __future__ import annotations

import math

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString, Point

from engine.suitability.hospital import HospitalSuitabilityAnalyzer
from engine.suitability.network_access import (
    NetworkTravelTimeIndex,
    network_gravity_marginal_gain,
)


CRITERION_LABELS = {
    "health_demand": "Local health need",
    "gravity_catchment": "Added access for underserved residents",
    "access_resilience": "Weather and road disruption resilience",
    "road_access": "Access to local roads",
    "emergency_access": "Access to major roads",
    "flat_terrain": "Buildable terrain",
    "utility_coverage": "Water and sewer availability",
    "public_transit": "Public transit access",
    "land_cost_proxy": "Lower development intensity",
}

REFERENCE_WEIGHTS = dict(HospitalSuitabilityAnalyzer.DEFAULT_WEIGHTS)
CRITERIA = tuple(REFERENCE_WEIGHTS)


class HospitalDemoModel:
    """Small deterministic synthetic city used for interaction and demos."""

    def __init__(self) -> None:
        self.roads, self.map_roads = self._make_roads()
        self.network = NetworkTravelTimeIndex(
            self.roads,
            access_speed_kmh=HospitalSuitabilityAnalyzer.NETWORK_ACCESS_SPEED_KMH,
            max_snap_distance_m=HospitalSuitabilityAnalyzer.NETWORK_MAX_SNAP_DISTANCE_M,
        )
        self.origins, self.map_origins = self._make_origins()
        self.hospitals, self.map_hospitals = self._make_hospitals()
        self.existing_times = [
            self.network.travel_times_to(hospital["point"], self.origins["point"].tolist())
            for hospital in self.hospitals
        ]

    @staticmethod
    def _to_map(point: Point) -> tuple[float, float]:
        return 50.0 + point.x / 7.0, 50.0 + point.y / 7.0

    def _make_roads(self) -> tuple[gpd.GeoDataFrame, list[dict[str, object]]]:
        rows: list[dict[str, object]] = []
        map_roads: list[dict[str, object]] = []
        cols, row_count = 8, 6
        nodes: dict[tuple[int, int], tuple[int, Point]] = {}
        for row in range(row_count):
            for col in range(cols):
                point = Point(560 + col * 800, 420 + row * 680)
                nodes[(row, col)] = (100_000 + row * cols + col, point)

        road_id = 1
        for row in range(row_count):
            for col in range(cols):
                for next_row, next_col in ((row, col + 1), (row + 1, col)):
                    if next_row >= row_count or next_col >= cols:
                        continue
                    start_id, start = nodes[(row, col)]
                    end_id, end = nodes[(next_row, next_col)]
                    major = row == 2 or col == 3 or next_row == 2 or next_col == 3
                    road_class = "primary" if major else "residential"
                    speed = 55 if major else 30
                    geometry = LineString([start, end])
                    rows.append({
                        "road_id": road_id,
                        "osm_node_from": start_id,
                        "osm_node_to": end_id,
                        "road_class": road_class,
                        "speed_limit_kmh": speed,
                        "is_oneway": False,
                        "geometry": geometry,
                    })
                    sx, sy = self._to_map(start)
                    ex, ey = self._to_map(end)
                    map_roads.append({
                        "x1": sx,
                        "y1": sy,
                        "x2": ex,
                        "y2": ey,
                        "major": major,
                    })
                    road_id += 1
        return gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:3857"), map_roads

    def _make_origins(self) -> tuple[pd.DataFrame, list[dict[str, object]]]:
        samples = [
            (0, 0, 6_400, 0.23, 0.19), (0, 3, 4_900, 0.31, 0.15),
            (0, 6, 5_600, 0.18, 0.24), (1, 2, 7_100, 0.35, 0.21),
            (1, 6, 3_800, 0.22, 0.12), (2, 1, 8_400, 0.29, 0.25),
            (2, 5, 6_100, 0.39, 0.28), (3, 3, 9_200, 0.33, 0.18),
            (3, 7, 4_400, 0.26, 0.16), (4, 1, 5_800, 0.21, 0.31),
            (4, 5, 7_700, 0.37, 0.23), (5, 3, 5_200, 0.19, 0.14),
            (5, 7, 6_900, 0.28, 0.22),
        ]
        rows: list[dict[str, object]] = []
        symbols: list[dict[str, object]] = []
        for row, col, population, illness, elderly in samples:
            point = Point(560 + col * 800, 420 + row * 680)
            x, y = self._to_map(point)
            rows.append({
                "point": point,
                "population": population,
                "illness": illness,
                "elderly": elderly,
            })
            symbols.append({
                "x": x,
                "y": y,
                "population": population,
                "need": round(illness * 0.7 + elderly * 0.3, 3),
            })
        return pd.DataFrame(rows), symbols

    def _make_hospitals(self) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        entries = [((1, 5), 260), ((4, 2), 180)]
        hospitals: list[dict[str, object]] = []
        symbols: list[dict[str, object]] = []
        for (row, col), beds in entries:
            point = Point(560 + col * 800, 420 + row * 680)
            x, y = self._to_map(point)
            hospitals.append({"point": point, "beds": beds})
            symbols.append({"x": x, "y": y, "beds": beds})
        return hospitals, symbols

    def map_payload(self) -> dict[str, object]:
        return {
            "roads": self.map_roads,
            "demand": self.map_origins,
            "hospitals": self.map_hospitals,
            "label": "Synthetic demonstration city",
        }

    def score(
        self,
        map_x: float,
        map_y: float,
        weights: dict[str, float],
        *,
        policy_is_override: bool = False,
    ) -> dict[str, object]:
        if not math.isfinite(map_x) or not math.isfinite(map_y):
            raise ValueError("Map coordinates must be finite numbers.")
        if not (50.0 <= map_x <= 950.0 and 50.0 <= map_y <= 650.0):
            raise ValueError("Choose a point inside the map area.")
        candidate = Point((map_x - 50.0) * 7.0, (map_y - 50.0) * 7.0)
        origin_points = self.origins["point"].tolist()
        times = self.network.travel_times_to(candidate, origin_points)
        illness = self.origins["illness"].to_numpy(dtype=float)
        elderly = self.origins["elderly"].to_numpy(dtype=float)
        illness_range = max(float(illness.max() - illness.min()), 1e-9)
        elderly_range = max(float(elderly.max() - elderly.min()), 1e-9)
        demand_index = (
            0.5 * (illness - illness.min()) / illness_range
            + 0.5 * (elderly - elderly.min()) / elderly_range
        )
        demand = self.origins["population"].to_numpy(dtype=float) * (1.0 + demand_index)

        finite = np.isfinite(times)
        health_need = float(np.sum(demand[finite] / (1.0 + times[finite] / 30.0)))
        health_score = min(1.0, health_need / max(float(demand.sum()), 1.0))
        gravity_gain = network_gravity_marginal_gain(
            demand,
            [hospital["beds"] for hospital in self.hospitals],
            self.existing_times,
            200.0,
            times,
            beta=HospitalSuitabilityAnalyzer.GRAVITY_BETA,
            adequate_beds_per_1000=HospitalSuitabilityAnalyzer.REFERENCE_BEDS_PER_1000_ADEQUATE,
        )
        gravity_score = min(1.0, max(0.0, gravity_gain / max(float(demand.sum()), 1.0)))

        local_roads = np.asarray([
            float(row.geometry.distance(candidate)) for row in self.roads.itertuples()
        ])
        major_roads = np.asarray([
            float(row.geometry.distance(candidate))
            for row in self.roads[self.roads["road_class"] == "primary"].itertuples()
        ])
        road_access = max(0.0, 1.0 - float(local_roads.min()) / 800.0)
        emergency_access = max(0.0, 1.0 - float(major_roads.min()) / 1_600.0)
        terrain = min(1.0, max(0.0, 0.85 - 0.12 * abs(math.sin(candidate.x / 1_700.0) * math.cos(candidate.y / 1_300.0))))
        utility = min(1.0, max(0.0, 0.9 - 0.15 * abs(candidate.x / 6_300.0 - 0.55)))
        transit_points = [Point(1_360, 1_780), Point(3_760, 2_460), Point(5_360, 3_140)]
        transit_distance = min(candidate.distance(stop) for stop in transit_points)
        transit = max(0.0, 1.0 - float(transit_distance) / 2_500.0)
        density = min(1.0, max(0.0, 0.2 + 0.55 * candidate.x / 6_300.0 + 0.2 * candidate.y / 4_000.0))

        criteria_scores = {
            "health_demand": health_score,
            "gravity_catchment": gravity_score,
            "access_resilience": 0.60,
            "road_access": road_access,
            "emergency_access": emergency_access,
            "flat_terrain": terrain,
            "utility_coverage": utility,
            "public_transit": transit,
            "land_cost_proxy": 1.0 - density,
        }
        total_weight = sum(weights.values())
        normalized_weights = {key: value / total_weight for key, value in weights.items()}
        contributions = {
            criterion: criteria_scores[criterion] * normalized_weights[criterion]
            for criterion in CRITERIA
        }
        overall = sum(contributions.values())
        shortest = min((value for value in times if math.isfinite(value)), default=None)
        return {
            "x": map_x,
            "y": map_y,
            "score": round(overall * 100.0, 1),
            "criteria": [
                {
                    "key": criterion,
                    "label": CRITERION_LABELS[criterion],
                    "score": round(criteria_scores[criterion] * 100.0, 1),
                    "weight": round(normalized_weights[criterion] * 100.0, 1),
                    "contribution": round(contributions[criterion] * 100.0, 1),
                }
                for criterion in CRITERIA
            ],
            "network_minutes_to_nearest_demand": round(shortest, 1) if shortest is not None else None,
            "policy_source": "city override" if policy_is_override else "developer reference defaults",
            "assumptions": [
                "Synthetic map, hospitals, population, health need, utilities, terrain, and transit.",
                "Road-time estimate uses static posted/class speeds and nearest-node snapping; no live traffic, closures, weather, or EMS records.",
                "Weather and road disruption resilience is a neutral illustrative placeholder (0.60).",
                "The gravity calculation uses a reference adequacy threshold of 2 beds per 1,000 people; the policy form currently customizes weights only.",
            ],
        }
