"""Static network travel-time and network gravity helpers.

The travel-time index uses the OSM node topology stored with ``road_network``.
Until a city supplies observed traffic or EMS speeds, travel time is estimated
from the posted speed limit or a documented road-class default. Points are
connected to their nearest OSM node with a short, straight access connector.

The accessibility calculation is an enhanced floating-catchment/gravity
measure. It estimates the marginal increase in adequately served demand when a
candidate facility is added to the existing system. It is not a forecast of
actual hospital utilization.
"""

from __future__ import annotations

import heapq
import logging
import math
from numbers import Integral
from typing import Iterable, Sequence

import numpy as np
import pandas as pd
from shapely.geometry import Point
from shapely.strtree import STRtree

logger = logging.getLogger(__name__)


DEFAULT_SPEEDS_KMH: dict[str, float] = {
    "motorway": 95.0,
    "motorway_link": 55.0,
    "trunk": 80.0,
    "trunk_link": 50.0,
    "primary": 60.0,
    "primary_link": 45.0,
    "secondary": 50.0,
    "secondary_link": 40.0,
    "tertiary": 40.0,
    "tertiary_link": 30.0,
    "unclassified": 30.0,
    "residential": 30.0,
    "living_street": 15.0,
    "service": 15.0,
    "road": 30.0,
}

NON_VEHICLE_ROAD_CLASSES = {
    "bridleway", "cycleway", "footway", "path", "pedestrian", "steps",
}


def _is_missing(value: object) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _as_node_id(value: object) -> object | None:
    if _is_missing(value):
        return None
    if isinstance(value, (float, np.floating)) and float(value).is_integer():
        return int(value)
    return value


def _as_bool(value: object) -> bool:
    if _is_missing(value):
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes", "y"}
    return bool(value)


class NetworkTravelTimeIndex:
    """Route points over a directed road graph and return minutes to a target.

    ``roads`` must be in a projected CRS with metre units and include OSM
    ``osm_node_from``/``osm_node_to`` identifiers, ``road_class``,
    ``speed_limit_kmh``, ``is_oneway``, and LineString geometry. Point-to-road
    access uses the nearest endpoint node; this is a transparent prototype
    approximation until driveway/entrance snapping is available.
    """

    def __init__(
        self,
        roads,
        *,
        access_speed_kmh: float = 10.0,
        max_snap_distance_m: float = 1_000.0,
        speed_defaults_kmh: dict[str, float] | None = None,
    ) -> None:
        self.access_speed_kmh = max(float(access_speed_kmh), 1.0)
        self.max_snap_distance_m = max(float(max_snap_distance_m), 0.0)
        self.speed_defaults_kmh = speed_defaults_kmh or DEFAULT_SPEEDS_KMH
        self.adjacency: dict[object, list[tuple[object, float]]] = {}
        self.reverse_adjacency: dict[object, list[tuple[object, float]]] = {}
        self.node_points: dict[object, Point] = {}
        self.edge_rows: list[dict[str, object]] = []

        for row in roads.itertuples(index=False):
            source = _as_node_id(getattr(row, "osm_node_from", None))
            target = _as_node_id(getattr(row, "osm_node_to", None))
            geometry = getattr(row, "geometry", None)
            if source is None or target is None or geometry is None or geometry.is_empty:
                continue
            if geometry.geom_type != "LineString" or len(geometry.coords) < 2:
                continue

            road_class = str(getattr(row, "road_class", "unclassified") or "unclassified")
            if road_class in NON_VEHICLE_ROAD_CLASSES:
                continue

            length_m = float(geometry.length)
            if not math.isfinite(length_m) or length_m <= 0:
                continue

            raw_speed = getattr(row, "speed_limit_kmh", None)
            try:
                speed_kmh = float(raw_speed) if not _is_missing(raw_speed) else 0.0
            except (TypeError, ValueError):
                speed_kmh = 0.0
            if not math.isfinite(speed_kmh) or speed_kmh <= 0:
                speed_kmh = self.speed_defaults_kmh.get(road_class, 25.0)

            # The OSM ingestion schema records from/to node IDs in line order.
            coordinates = list(geometry.coords)
            self.node_points.setdefault(source, Point(coordinates[0]))
            self.node_points.setdefault(target, Point(coordinates[-1]))

            minutes = length_m / (speed_kmh * 1_000.0 / 60.0)
            one_way = _as_bool(getattr(row, "is_oneway", False))
            road_id = getattr(row, "road_id", len(self.edge_rows))
            self.edge_rows.append({
                "road_id": road_id,
                "source": source,
                "target": target,
                "length_m": length_m,
                "minutes": minutes,
                "road_class": road_class,
                "geometry": geometry,
            })
            self._add_arc(source, target, minutes)
            if not one_way:
                self._add_arc(target, source, minutes)

        if not self.node_points:
            raise ValueError(
                "The road network has no routable OSM topology. "
                "Load vehicle roads with osm_node_from/osm_node_to values."
            )

        self._node_ids = list(self.node_points)
        self._node_geometries = [self.node_points[node_id] for node_id in self._node_ids]
        self._geometry_indexes = {
            id(geometry): index
            for index, geometry in enumerate(self._node_geometries)
        }
        self._tree = STRtree(self._node_geometries)

    def _add_arc(self, source: object, target: object, minutes: float) -> None:
        self.adjacency.setdefault(source, []).append((target, minutes))
        # Reverse adjacency lets us run one Dijkstra pass from a destination
        # and obtain origin-to-destination times for every demand cell.
        self.reverse_adjacency.setdefault(target, []).append((source, minutes))

    def _nearest_node(self, point: Point) -> tuple[object, float] | None:
        try:
            nearest = self._tree.nearest(point)
            if isinstance(nearest, Integral):
                index = int(nearest)
            else:
                index = self._geometry_indexes.get(id(nearest), -1)
                if index < 0:
                    # Some Shapely versions can return an equivalent geometry
                    # object rather than the indexed instance.
                    index = min(
                        range(len(self._node_geometries)),
                        key=lambda i: self._node_geometries[i].distance(point),
                    )
        except (AttributeError, TypeError, ValueError):
            index = min(
                range(len(self._node_geometries)),
                key=lambda i: self._node_geometries[i].distance(point),
            )

        node_id = self._node_ids[index]
        distance_m = float(self._node_geometries[index].distance(point))
        if distance_m > self.max_snap_distance_m:
            return None
        return node_id, distance_m

    @staticmethod
    def _dijkstra_to_target(
        target_node: object,
        reverse_adjacency: dict[object, list[tuple[object, float]]],
    ) -> dict[object, float]:
        """Return shortest directed travel time from every reachable node."""
        distances = {target_node: 0.0}
        queue: list[tuple[float, object]] = [(0.0, target_node)]
        while queue:
            current_minutes, node = heapq.heappop(queue)
            if current_minutes != distances.get(node):
                continue
            for predecessor, edge_minutes in reverse_adjacency.get(node, ()):
                candidate = current_minutes + edge_minutes
                if candidate < distances.get(predecessor, math.inf):
                    distances[predecessor] = candidate
                    heapq.heappush(queue, (candidate, predecessor))
        return distances

    def travel_times_to(
        self,
        destination: Point,
        origins: Iterable[Point],
    ) -> np.ndarray:
        """Return directed network travel times in minutes for each origin."""
        origins = list(origins)
        target_snap = self._nearest_node(destination)
        if target_snap is None:
            return np.full(len(origins), np.inf, dtype=float)

        target_node, target_access_m = target_snap
        network_times = self._dijkstra_to_target(target_node, self.reverse_adjacency)
        target_access_minutes = target_access_m / (self.access_speed_kmh * 1_000.0 / 60.0)

        output: list[float] = []
        for origin in origins:
            origin_snap = self._nearest_node(origin)
            if origin_snap is None:
                output.append(math.inf)
                continue
            origin_node, origin_access_m = origin_snap
            route_minutes = network_times.get(origin_node, math.inf)
            if not math.isfinite(route_minutes):
                output.append(math.inf)
                continue
            origin_access_minutes = (
                origin_access_m / (self.access_speed_kmh * 1_000.0 / 60.0)
            )
            output.append(
                origin_access_minutes + route_minutes + target_access_minutes
            )
        return np.asarray(output, dtype=float)


def network_gravity_marginal_gain(
    demand_weights: Sequence[float],
    existing_beds: Sequence[float],
    existing_travel_times: Sequence[Sequence[float]],
    candidate_beds: float,
    candidate_travel_times: Sequence[float],
    *,
    beta: float = 1.8,
    adequate_beds_per_1000: float = 2.0,
) -> float:
    """Estimate need-weighted marginal access gained by adding one hospital.

    This continuous 2SFCA/gravity calculation:
      1. Computes a bed-to-weighted-demand ratio for each hospital using its
         network travel-time catchment.
      2. Sums those ratios at each demand origin to estimate current access.
      3. Adds the candidate's contribution and scores only the improvement up
         to the reference adequacy level.

    With no existing hospitals, candidate demand still decays with network
    travel time; it does not become 100% captured everywhere in a radius.
    """
    demand = np.asarray(demand_weights, dtype=float)
    candidate_times = np.asarray(candidate_travel_times, dtype=float)
    if demand.size == 0 or candidate_times.size != demand.size:
        return 0.0

    def impedance(times: np.ndarray) -> np.ndarray:
        finite = np.isfinite(times) & (times >= 0)
        values = np.zeros(times.shape, dtype=float)
        # A one-minute floor avoids singular attraction at a colocated point.
        values[finite] = 1.0 / np.power(1.0 + np.maximum(times[finite], 0.0), beta)
        return values

    existing_access = np.zeros(demand.shape, dtype=float)
    for beds, times in zip(existing_beds, existing_travel_times):
        time_values = np.asarray(times, dtype=float)
        if time_values.size != demand.size:
            continue
        time_weight = impedance(time_values)
        weighted_catchment_demand = float(np.sum(demand * time_weight))
        if weighted_catchment_demand <= 0 or beds <= 0:
            continue
        facility_supply_ratio = float(beds) / weighted_catchment_demand
        existing_access += facility_supply_ratio * time_weight

    candidate_weight = impedance(candidate_times)
    candidate_catchment_demand = float(np.sum(demand * candidate_weight))
    if candidate_catchment_demand <= 0 or candidate_beds <= 0:
        return 0.0

    candidate_supply_ratio = float(candidate_beds) / candidate_catchment_demand
    adequacy = max(float(adequate_beds_per_1000), 1e-9) / 1_000.0
    coverage_before = np.clip(existing_access / adequacy, 0.0, 1.0)
    coverage_after = np.clip(
        (existing_access + candidate_supply_ratio * candidate_weight) / adequacy,
        0.0,
        1.0,
    )
    return float(np.sum(demand * np.maximum(coverage_after - coverage_before, 0.0)))
