"""Hospital suitability analyzer for the Metro Expansion Planner.

The gravity catchment component uses a directed road graph and static network
travel-time estimates to score the marginal access benefit of a candidate.
Other criteria continue to score health need, disruption resilience, roads,
terrain, utilities, transit, and development intensity. Production results
require locally validated data, policy settings, and travel-time inputs.
"""

from __future__ import annotations

import logging

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.ops import unary_union

from engine.projection.crs_strategy import OperationType
from .base import BaseSuitabilityAnalyzer, SuitabilityConfig
from .network_access import NetworkTravelTimeIndex, network_gravity_marginal_gain

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Zoning codes that permit healthcare / institutional use
# ---------------------------------------------------------------------------
HEALTHCARE_PERMITTED_ZONES = {
    "C1", "C2", "C4", "C6",
    "I",  "I1", "I2",
    "PF", "PF1",
    "M1",
}

# ---------------------------------------------------------------------------
# Health demand sub-weights (sum to 1.0)
# ---------------------------------------------------------------------------
HEALTH_DEMAND_SUB_WEIGHTS = {
    "disease_prevalence":   0.40,
    "elderly_ratio":        0.30,
    "primary_care_desert":  0.20,
    "ed_utilisation":       0.10,
}

# ---------------------------------------------------------------------------
# Flood severity classification
# Maps NOAA Storm Events / FEMA damage category codes to a 0–1 severity weight.
# Used when scoring historical flood events on road segments.
# ---------------------------------------------------------------------------
FLOOD_SEVERITY_WEIGHTS = {
    "minor":    0.2,   # road passable with difficulty, no closure
    "moderate": 0.5,   # road closed, alternate routes available
    "major":    0.8,   # neighbourhood isolated, all routes blocked
    "extreme":  1.0,   # multi-day isolation, sustained emergency impact
}


class HospitalSuitabilityAnalyzer(BaseSuitabilityAnalyzer):
    """
    Concrete analyzer for hospital and major healthcare facility placement.

    The gravity catchment score routes demand origins to existing and candidate
    sites over the vehicle road graph, estimates the increase in adequate access,
    and scales that gain by total weighted demand. Until observed travel-time
    data is configured, route impedance uses posted speeds or road-class defaults.
    """

    FACILITY_TYPE = "hospital"
    MIN_PARCEL_AREA_M2 = 10_000.0

    DEFAULT_WEIGHTS: dict[str, float] = {
        "health_demand":       0.20,
        "gravity_catchment":   0.20,
        "access_resilience":   0.15,
        "road_access":         0.12,
        "emergency_access":    0.12,
        "flat_terrain":        0.08,
        "utility_coverage":    0.08,
        "public_transit":      0.03,
        "land_cost_proxy":     0.02,
    }

    # -----------------------------------------------------------------------
    # Hard constraint thresholds
    # -----------------------------------------------------------------------
    FLOOD_ZONE_CODES = {"A", "AE", "AH", "AO", "VE"}
    MIN_AREA_M2 = 10_000.0
    MIN_DISTANCE_FROM_INDUSTRIAL_M = 200.0
    MAX_DISTANCE_TO_ROAD_M = 100.0

    # -----------------------------------------------------------------------
    # Health demand parameters
    # -----------------------------------------------------------------------
    HEALTH_DEMAND_RADIUS_M = 2_000.0
    PRIMARY_CARE_DESERT_RADIUS_M = 2_000.0
    DISEASE_WEIGHTS = {
        "prevalence_heart_disease":  0.25,
        "prevalence_diabetes":       0.20,
        "prevalence_copd":           0.20,
        "prevalence_stroke":         0.15,
        "prevalence_obesity":        0.10,
        "prevalence_mental_health":  0.10,
    }

    # -----------------------------------------------------------------------
    # Gravity catchment model parameters
    # Starting exponent for decay by modeled network travel time. Calibrate it
    # against local travel observations before using operationally.
    GRAVITY_BETA = 1.8

    # Static reference impedance settings until observed travel-time data is
    # available. The access speed applies to point-to-node connectors.
    NETWORK_ACCESS_SPEED_KMH = 10.0
    NETWORK_MAX_SNAP_DISTANCE_M = 1_000.0

    # Reference adequacy level for marginal access. Cities should validate this
    # against local service objectives before operational use.
    REFERENCE_BEDS_PER_1000_ADEQUATE = 2.0
    WHO_BEDS_PER_1000_ADEQUATE = REFERENCE_BEDS_PER_1000_ADEQUATE
    WHO_BEDS_PER_1000_CRITICAL = 0.5
    AVG_BEDS_PER_HOSPITAL = 200

    # -----------------------------------------------------------------------
    # Access resilience parameters
    # -----------------------------------------------------------------------
    # Radius around candidate parcel within which we evaluate road flood history
    ACCESS_RESILIENCE_RADIUS_M = 3_000.0

    # Number of years of historical flood event data to use
    FLOOD_HISTORY_YEARS = 20

    # Weight applied when flood event coincided with a declared medical emergency
    # (hurricane, major storm) — these are worst-case events where hospital
    # access AND demand both spike simultaneously
    FLOOD_DEMAND_CORRELATION_PENALTY = 1.5

    # -----------------------------------------------------------------------
    # Road / terrain parameters
    # -----------------------------------------------------------------------
    IDEAL_ROAD_DISTANCE_M = 50.0
    MAX_ROAD_DISTANCE_M = 500.0
    MAX_SLOPE_IDEAL_DEG = 2.0
    MAX_SLOPE_HARD_DEG = 15.0

    def __init__(self, config: SuitabilityConfig) -> None:
        if config.min_parcel_area_m2 == 1000.0:
            config.min_parcel_area_m2 = self.MIN_AREA_M2
        super().__init__(config)

    # =======================================================================
    # STEP 1 — Load layers
    # =======================================================================

    def load_layers(self) -> None:
        """
        Pull all spatial layers from PostGIS using the runtime projection strategy.
        Each query specifies the OperationType it will be used for so the base
        class applies the correct CRS automatically.
        """
        logger.info("Loading land parcels...")
        self.parcels = self.load_geodataframe("""
            SELECT p.parcel_id, p.zoning_code, p.land_use_category,
                   p.area_m2, p.development_density, p.geometry
            FROM land_parcels p
        """, operation=OperationType.AREA)   # area_m2 filter uses equal-area CRS

        logger.info("Loading routable vehicle road network with OSM topology...")
        self.layers["roads_all"] = self.load_geodataframe("""
            SELECT road_id, osm_node_from, osm_node_to, road_class,
                   speed_limit_kmh, is_oneway, geometry
            FROM road_network
            WHERE road_class IS NULL
               OR road_class NOT IN ('bridleway','cycleway','footway','path','pedestrian','steps')
        """, operation=OperationType.DISTANCE)
        self.layers["roads"] = self.layers["roads_all"][
            self.layers["roads_all"]["road_class"].isin(
                {"motorway", "trunk", "primary", "secondary"}
            )
        ].copy()

        logger.info("Loading existing hospitals with bed counts...")
        self.layers["hospitals"] = self.load_geodataframe(f"""
            SELECT facility_id, facility_name,
                   COALESCE(bed_count, {self.AVG_BEDS_PER_HOSPITAL}) AS bed_count,
                   geometry
            FROM existing_facilities
            WHERE facility_type = 'hospital'
        """, operation=OperationType.DISTANCE)

        logger.info("Loading flood zones...")
        self.layers["flood_zones"] = self.load_geodataframe(f"""
            SELECT zone_code, geometry
            FROM environmental_zones
            WHERE zone_type = 'flood'
            AND zone_code = ANY(ARRAY{list(self.FLOOD_ZONE_CODES)})
        """, operation=OperationType.DISTANCE)

        logger.info("Loading industrial buffer zones...")
        self.layers["industrial"] = self.load_geodataframe("""
            SELECT zone_id, geometry
            FROM environmental_zones
            WHERE zone_type = 'industrial_buffer'
        """, operation=OperationType.DISTANCE)

        logger.info("Loading health demand grid (CDC PLACES + demographics)...")
        self.layers["health_grid"] = self.load_geodataframe("""
            SELECT cell_id, population, elderly_population, ed_visit_rate,
                   prevalence_heart_disease, prevalence_diabetes, prevalence_copd,
                   prevalence_stroke, prevalence_obesity, prevalence_mental_health,
                   geometry
            FROM health_demand_grid
        """, operation=OperationType.DISTANCE)

        logger.info("Loading primary care facilities...")
        self.layers["primary_care"] = self.load_geodataframe("""
            SELECT facility_id, geometry
            FROM existing_facilities
            WHERE facility_type IN ('gp_clinic','community_health_centre','urgent_care')
        """, operation=OperationType.DISTANCE)

        logger.info("Loading historical road flood events...")
        # road_flood_events: one row per (road_segment, flood_event) pair.
        # Sourced from NOAA Storm Events database + FEMA NFIP claims.
        # Key columns:
        #   road_segment_id  — links to road_network
        #   event_date       — date of flood event
        #   severity_class   — 'minor','moderate','major','extreme'
        #   declared_emergency — bool: was this a declared medical emergency event?
        #   geometry         — road segment geometry (line)
        self.layers["road_flood_events"] = self.load_geodataframe(f"""
            SELECT rfe.road_segment_id, rfe.event_date, rfe.severity_class,
                   rfe.declared_emergency, rn.road_class,
                   rn.geometry
            FROM road_flood_events rfe
            JOIN road_network rn ON rfe.road_segment_id = rn.road_id
            WHERE rfe.event_date >= NOW() - INTERVAL '{self.FLOOD_HISTORY_YEARS} years'
        """, operation=OperationType.DISTANCE)

        logger.info("Loading utility coverage polygons...")
        self.layers["utilities"] = self.load_geodataframe("""
            SELECT utility_type, geometry
            FROM utility_coverage
            WHERE utility_type IN ('water','sewage')
        """, operation=OperationType.DISTANCE)

        logger.info("Loading public transit stops...")
        self.layers["transit"] = self.load_geodataframe("""
            SELECT stop_id, stop_type, geometry
            FROM existing_facilities
            WHERE facility_type IN ('bus_stop','metro_station','train_station')
        """, operation=OperationType.DISTANCE)

        logger.info("Loading terrain slope grid...")
        self.layers["slope"] = self.load_geodataframe("""
            SELECT cell_id, slope_degrees, geometry
            FROM terrain_grid
        """, operation=OperationType.DISTANCE)

        logger.info("All layers loaded. Parcel count: %d", len(self.parcels))

    # =======================================================================
    # STEP 2 — Hard constraints
    # =======================================================================

    def apply_hard_constraints(self, parcels: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        """
        Binary elimination. Cheapest attribute filters run first.
        """
        n_start = len(parcels)

        # Reproject parcels to distance CRS for spatial operations
        parcels_dist = self.proj.reproject(parcels, OperationType.DISTANCE)

        # 2a. Minimum area — use area CRS for correct m² calculation
        parcels_area = self.proj.reproject(parcels, OperationType.AREA)
        parcels_area["area_m2_calc"] = parcels_area.geometry.area
        valid_idx = parcels_area[parcels_area["area_m2_calc"] >= self.MIN_AREA_M2].index
        parcels = parcels.loc[valid_idx].copy()
        parcels_dist = parcels_dist.loc[valid_idx].copy()
        logger.info("After area filter: %d remain", len(parcels))

        # 2b. Zoning (attribute filter, no spatial op)
        mask = parcels["zoning_code"].isin(HEALTHCARE_PERMITTED_ZONES)
        parcels = parcels[mask].copy()
        parcels_dist = parcels_dist[mask].copy()
        logger.info("After zoning filter: %d remain", len(parcels))

        if len(parcels) == 0:
            return parcels

        # 2c. Flood zone exclusion
        if len(self.layers["flood_zones"]) > 0:
            flood_union = unary_union(self.layers["flood_zones"].geometry)
            mask = ~parcels_dist.geometry.centroid.within(flood_union)
            parcels = parcels[mask].copy()
            parcels_dist = parcels_dist[mask].copy()
        logger.info("After flood exclusion: %d remain", len(parcels))

        if len(parcels) == 0:
            return parcels

        # 2d. Industrial buffer
        if len(self.layers["industrial"]) > 0:
            ind_union = unary_union(self.layers["industrial"].geometry)
            dist = parcels_dist.geometry.centroid.distance(ind_union)
            mask = dist >= self.MIN_DISTANCE_FROM_INDUSTRIAL_M
            parcels = parcels[mask].copy()
            parcels_dist = parcels_dist[mask].copy()
        logger.info("After industrial buffer: %d remain", len(parcels))

        if len(parcels) == 0:
            return parcels

        # 2e. Road access
        if len(self.layers["roads"]) > 0:
            road_union = unary_union(self.layers["roads"].geometry)
            dist = parcels_dist.geometry.centroid.distance(road_union)
            mask = dist <= self.MAX_DISTANCE_TO_ROAD_M
            parcels = parcels[mask].copy()
        logger.info("After road access check: %d remain", len(parcels))

        logger.info(
            "Hard constraints complete: %d / %d remain (%.1f%%)",
            len(parcels), n_start,
            100 * len(parcels) / n_start if n_start > 0 else 0
        )
        return parcels

    # =======================================================================
    # STEP 3 — Score soft criteria
    # =======================================================================

    def score_criteria(self, parcels: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        """
        Add score_<criterion> columns. All scores normalised to [0,1].
        """
        parcels = parcels.copy()
        # Work in distance CRS for all proximity operations
        parcels_dist = self.proj.reproject(parcels, OperationType.DISTANCE)
        centroids = parcels_dist.geometry.centroid

        logger.info("Computing health demand scores...")
        parcels["score_health_demand"] = self._score_health_demand(
            centroids, parcels.index
        )

        logger.info("Computing gravity catchment scores...")
        parcels["score_gravity_catchment"] = self._score_gravity_catchment(
            centroids, parcels.index
        )

        logger.info("Computing access resilience scores...")
        parcels["score_access_resilience"] = self._score_access_resilience(
            centroids, parcels.index
        )

        all_road_union = unary_union(self.layers["roads_all"].geometry)
        major_road_union = unary_union(self.layers["roads"].geometry)
        dist_to_road = centroids.distance(all_road_union)
        dist_to_major_road = centroids.distance(major_road_union)

        parcels["score_road_access"] = self.distance_decay_score(
            dist_to_road, ideal_m=self.IDEAL_ROAD_DISTANCE_M,
            max_m=self.MAX_ROAD_DISTANCE_M
        )
        parcels["score_emergency_access"] = self.distance_decay_score(
            dist_to_major_road, ideal_m=100.0, max_m=800.0
        )

        parcels["score_flat_terrain"] = self._score_terrain(centroids, parcels.index)
        parcels["score_utility_coverage"] = self._score_utility_coverage(
            parcels_dist
        )
        parcels["score_land_cost_proxy"] = 1.0 - self.normalise_column(
            parcels["development_density"].fillna(0)
        )

        if len(self.layers["transit"]) > 0:
            transit_union = unary_union(self.layers["transit"].geometry)
            parcels["score_public_transit"] = self.distance_decay_score(
                centroids.distance(transit_union), ideal_m=200.0, max_m=1_500.0
            )
        else:
            parcels["score_public_transit"] = 0.5

        logger.info(
            "Scoring complete: %s",
            [c for c in parcels.columns if c.startswith("score_")]
        )
        return parcels

    # =======================================================================
    # Private scoring helpers
    # =======================================================================

    def _compute_health_demand_index(
        self,
        centroids: gpd.GeoSeries,
        parcel_index: pd.Index,
        radius_m: float
    ) -> pd.Series:
        """
        Core health demand computation reused by both _score_health_demand
        and _score_gravity_catchment.

        Returns a raw (non-normalised) demand index per parcel, representing
        the weighted healthcare need of the population within radius_m.
        """
        health_grid = self.layers["health_grid"]

        centroid_gdf = gpd.GeoDataFrame(
            {"parcel_idx": parcel_index},
            geometry=centroids.buffer(radius_m),
            crs=self.layers["health_grid"].crs
        )

        joined = gpd.sjoin(health_grid, centroid_gdf, how="inner", predicate="within")

        if len(joined) == 0:
            return pd.Series(np.full(len(parcel_index), 0.5), index=parcel_index)

        for cond in self.DISEASE_WEIGHTS:
            if cond not in joined.columns:
                joined[cond] = 0.0

        joined["weighted_prevalence"] = sum(
            self.DISEASE_WEIGHTS[c] * joined[c] for c in self.DISEASE_WEIGHTS
        )
        joined["pop_x_prev"] = joined["population"] * joined["weighted_prevalence"]

        grouped = joined.groupby("parcel_idx").agg(
            total_pop       =("population",        "sum"),
            total_pop_x_prev=("pop_x_prev",        "sum"),
            total_elderly   =("elderly_population", "sum"),
            mean_ed_rate    =("ed_visit_rate",      "mean"),
        )

        grouped["avg_disease_prev"] = np.where(
            grouped["total_pop"] > 0,
            grouped["total_pop_x_prev"] / grouped["total_pop"], 0.0
        )
        grouped["elderly_ratio"] = np.where(
            grouped["total_pop"] > 0,
            grouped["total_elderly"] / grouped["total_pop"], 0.0
        )

        grouped = grouped.reindex(parcel_index, fill_value=0)
        return grouped

    def _score_health_demand(
        self,
        centroids: gpd.GeoSeries,
        parcel_index: pd.Index
    ) -> pd.Series:
        """
        Composite healthcare demand score for each parcel catchment.
        Four sub-components: disease prevalence, elderly ratio,
        primary care desert, ED utilisation.
        """
        health_grid = self.layers["health_grid"]
        if len(health_grid) == 0:
            logger.warning("health_demand_grid empty — returning neutral scores")
            return pd.Series(np.full(len(parcel_index), 0.5), index=parcel_index)

        grouped = self._compute_health_demand_index(
            centroids, parcel_index, self.HEALTH_DEMAND_RADIUS_M
        )

        # Primary care desert
        if len(self.layers["primary_care"]) > 0:
            pc_union = unary_union(self.layers["primary_care"].geometry)
            desert = (centroids.distance(pc_union) > self.PRIMARY_CARE_DESERT_RADIUS_M)
            desert.index = parcel_index
        else:
            desert = pd.Series(np.ones(len(parcel_index), dtype=float), index=parcel_index)

        w = HEALTH_DEMAND_SUB_WEIGHTS
        composite = (
            w["disease_prevalence"]  * self.normalise_column(grouped["avg_disease_prev"]) +
            w["elderly_ratio"]       * self.normalise_column(grouped["elderly_ratio"]) +
            w["primary_care_desert"] * desert.astype(float) +
            w["ed_utilisation"]      * self.normalise_column(grouped["mean_ed_rate"].fillna(0))
        )
        return composite.reindex(parcel_index, fill_value=0.5)

    def _score_gravity_catchment(
        self,
        centroids: gpd.GeoSeries,
        parcel_index: pd.Index
    ) -> pd.Series:
        """Score network-time access improvement from adding a hospital.

        Demand cells and existing hospitals are routed over the directed
        vehicle network. The result estimates need-weighted marginal access,
        capped at the reference beds-per-population adequacy level. Until
        observed route-time data is connected, edge speeds come from posted
        limits or documented road-class defaults.
        """
        health_grid = self.layers["health_grid"]
        hospitals = self.layers["hospitals"]
        if len(health_grid) == 0:
            logger.warning("health_demand_grid empty - returning neutral gravity scores")
            return pd.Series(np.full(len(parcel_index), 0.5), index=parcel_index)

        hg = health_grid.copy()
        for condition in self.DISEASE_WEIGHTS:
            if condition not in hg.columns:
                hg[condition] = 0.0
        for column in ("population", "elderly_population"):
            if column not in hg.columns:
                hg[column] = 0.0
            hg[column] = hg[column].fillna(0.0)

        disease_prevalence = sum(
            self.DISEASE_WEIGHTS[c] * hg[c].fillna(0.0)
            for c in self.DISEASE_WEIGHTS
        )
        elderly_ratio = hg["elderly_population"] / hg["population"].replace(0, np.nan)
        hg["demand_index"] = (
            0.5 * self.normalise_column(disease_prevalence) +
            0.5 * self.normalise_column(elderly_ratio.fillna(0.0))
        )
        demand_weights = (hg["population"] * (1.0 + hg["demand_index"])).to_numpy(dtype=float)
        demand_origins = list(hg.geometry.centroid)
        roads = self.layers.get("roads_all", self.layers.get("roads"))

        try:
            network = NetworkTravelTimeIndex(
                roads,
                access_speed_kmh=self.NETWORK_ACCESS_SPEED_KMH,
                max_snap_distance_m=self.NETWORK_MAX_SNAP_DISTANCE_M,
            )
        except (ValueError, TypeError):
            logger.exception("Cannot build routable road graph for gravity scoring")
            return pd.Series(np.zeros(len(parcel_index)), index=parcel_index)

        existing_beds: list[float] = []
        existing_times: list[np.ndarray] = []
        for hospital in hospitals.itertuples():
            beds = hospital.bed_count
            existing_beds.append(
                float(beds) if pd.notna(beds) else float(self.AVG_BEDS_PER_HOSPITAL)
            )
            existing_times.append(
                network.travel_times_to(hospital.geometry.centroid, demand_origins)
            )

        raw_scores = pd.Series(np.zeros(len(parcel_index)), index=parcel_index)
        for position, parcel_id in enumerate(parcel_index):
            candidate_times = network.travel_times_to(
                centroids.iloc[position], demand_origins
            )
            raw_scores.loc[parcel_id] = network_gravity_marginal_gain(
                demand_weights,
                existing_beds,
                existing_times,
                self.AVG_BEDS_PER_HOSPITAL,
                candidate_times,
                beta=self.GRAVITY_BETA,
                adequate_beds_per_1000=self.REFERENCE_BEDS_PER_1000_ADEQUATE,
            )

        total_weighted_demand = max(float(demand_weights.sum()), 1.0)
        return (raw_scores / total_weighted_demand).clip(0.0, 1.0)

    def _score_access_resilience(
        self,
        centroids: gpd.GeoSeries,
        parcel_index: pd.Index
    ) -> pd.Series:
        """
        Flood-adjusted access reliability for the road network surrounding
        each candidate site.

        What this measures
        ------------------
        A hospital on a dry parcel surrounded by roads that flood every 3 years
        is operationally unreliable. During exactly the events that spike hospital
        demand (hurricanes, major storms), the site becomes inaccessible.

        This score answers: "how reliably can patients and emergency vehicles
        reach this site across historical weather conditions?"

        Method
        ------
        1.  Find all road segments within ACCESS_RESILIENCE_RADIUS_M.
        2.  For each road segment retrieve its historical flood events from
            the road_flood_events layer (NOAA Storm Events + FEMA NFIP).
        3.  Compute a flood disruption score per road segment:
                disruption = Σ (severity_weight × event_count_weight)
                event_count_weight accounts for recurrence — a road that
                floods every year is far worse than one that flooded once.
        4.  Apply a declared-emergency multiplier:
                events where floods coincided with declared medical emergencies
                (hurricanes) are weighted by FLOOD_DEMAND_CORRELATION_PENALTY
                because those are the exact moments when hospital access is
                most critical.
        5.  Aggregate road disruption scores across the neighbourhood,
                weighted by road classification (arterials matter more than
                local streets).
        6.  Convert neighbourhood flood disruption to access reliability:
                reliability = 1 - normalised_disruption
                High disruption history → low reliability → LOW score

        Parameters
        ----------
        centroids : projected centroids of candidate parcels
        parcel_index : pandas Index aligned to centroids

        Returns
        -------
        pd.Series of [0,1] access resilience scores, indexed by parcel_index.
        1.0 = roads around this site have zero historical flood events (most reliable)
        0.0 = roads around this site have severe and frequent flood history
        """
        flood_events = self.layers["road_flood_events"]

        # If no historical flood data available, we cannot penalise — return
        # a moderate score (0.6) rather than 1.0 to flag data uncertainty
        if len(flood_events) == 0:
            logger.warning(
                "road_flood_events layer is empty. "
                "Returning cautionary score of 0.6 — data gap, not confirmed safe."
            )
            return pd.Series(np.full(len(parcel_index), 0.6), index=parcel_index)

        # Road class importance weights — arterials/highways matter most for
        # hospital access; local streets matter less
        road_class_weights = {
            "motorway":  1.0,
            "trunk":     0.9,
            "primary":   0.8,
            "secondary": 0.6,
            "tertiary":  0.3,
            "local":     0.1,
        }

        # Pre-compute: severity weight × recurrence weight for every flood event
        # Recurrence is normalised over the analysis period so more-frequent
        # events score higher
        flood_events = flood_events.copy()

        flood_events["severity_w"] = flood_events["severity_class"].map(
            FLOOD_SEVERITY_WEIGHTS
        ).fillna(0.3)  # unknown severity → moderate assumption

        # Declared emergency multiplier
        flood_events["emergency_mult"] = np.where(
            flood_events["declared_emergency"].fillna(False),
            self.FLOOD_DEMAND_CORRELATION_PENALTY,
            1.0
        )

        flood_events["event_disruption"] = (
            flood_events["severity_w"] * flood_events["emergency_mult"]
        )

        # Aggregate to road-segment level: sum of disruption scores
        segment_disruption = (
            flood_events.groupby("road_segment_id")["event_disruption"]
            .sum()
            .rename("total_disruption")
        )

        # Join disruption back to the geometry layer for spatial ops
        road_disruption_gdf = flood_events[
            ["road_segment_id", "geometry"]
        ].drop_duplicates("road_segment_id").copy()

        road_disruption_gdf = road_disruption_gdf.merge(
            segment_disruption, on="road_segment_id", how="left"
        ).fillna({"total_disruption": 0.0})

        # Road class weight (default 0.3 if class not in table)
        if "road_class" in flood_events.columns:
            class_map = (
                flood_events[["road_segment_id", "road_class"]]
                .drop_duplicates("road_segment_id")
                .set_index("road_segment_id")["road_class"]
            )
            road_disruption_gdf["road_class_w"] = (
                road_disruption_gdf["road_segment_id"]
                .map(class_map)
                .map(road_class_weights)
                .fillna(0.3)
            )
        else:
            road_disruption_gdf["road_class_w"] = 0.5

        # Weighted disruption per road segment
        road_disruption_gdf["weighted_disruption"] = (
            road_disruption_gdf["total_disruption"] *
            road_disruption_gdf["road_class_w"]
        )

        # ----------------------------------------------------------------
        # For each candidate parcel: aggregate weighted disruption of all
        # road segments within ACCESS_RESILIENCE_RADIUS_M
        # ----------------------------------------------------------------
        centroid_gdf = gpd.GeoDataFrame(
            {"parcel_idx": parcel_index},
            geometry=centroids.buffer(self.ACCESS_RESILIENCE_RADIUS_M),
            crs=flood_events.crs
        )

        joined = gpd.sjoin(
            road_disruption_gdf[["road_segment_id", "weighted_disruption",
                                  "road_class_w", "geometry"]],
            centroid_gdf,
            how="inner",
            predicate="intersects"   # road SEGMENTS — use intersects not within
        )

        if len(joined) == 0:
            # No flood history on roads within radius — full resilience score
            return pd.Series(np.ones(len(parcel_index)), index=parcel_index)

        # Neighbourhood disruption = sum of weighted disruption / sum of road weights
        # (normalised so areas with more roads aren't unfairly penalised)
        neighbourhood_stats = joined.groupby("parcel_idx").agg(
            total_weighted_disruption=("weighted_disruption", "sum"),
            total_road_weight        =("road_class_w",         "sum"),
        )
        neighbourhood_stats["normalised_disruption"] = (
            neighbourhood_stats["total_weighted_disruption"] /
            neighbourhood_stats["total_road_weight"].clip(lower=1e-9)
        )

        disruption = neighbourhood_stats["normalised_disruption"].reindex(
            parcel_index, fill_value=0.0
        )

        # Convert disruption → reliability (invert and normalise)
        # High disruption = low score
        reliability = 1.0 - self.normalise_column(disruption)

        logger.debug(
            "Access resilience | mean=%.3f | worst=%.3f | "
            "high-risk parcels (score<0.3): %d",
            reliability.mean(), reliability.min(),
            int((reliability < 0.3).sum())
        )

        return reliability

    def _score_terrain(
        self,
        centroids: gpd.GeoSeries,
        parcel_index: pd.Index
    ) -> pd.Series:
        """Flatter terrain scores higher. Hospitals need level building footprints."""
        slope_grid = self.layers["slope"]
        if len(slope_grid) == 0:
            return pd.Series(np.full(len(parcel_index), 0.5), index=parcel_index)

        centroid_gdf = gpd.GeoDataFrame(
            {"parcel_idx": parcel_index},
            geometry=centroids,
            crs=slope_grid.crs
        )
        joined = gpd.sjoin(
            centroid_gdf, slope_grid[["slope_degrees", "geometry"]],
            how="left", predicate="within"
        )
        joined["slope_degrees"] = joined["slope_degrees"].fillna(
            slope_grid["slope_degrees"].median()
        )
        slope_vals = (
            joined.set_index("parcel_idx")["slope_degrees"]
            .pipe(lambda s: s[~s.index.duplicated(keep="first")])
            .reindex(parcel_index, fill_value=self.MAX_SLOPE_HARD_DEG)
        )
        scores = np.where(
            slope_vals <= self.MAX_SLOPE_IDEAL_DEG, 1.0,
            np.where(
                slope_vals >= self.MAX_SLOPE_HARD_DEG, 0.0,
                1.0 - (slope_vals - self.MAX_SLOPE_IDEAL_DEG) /
                      (self.MAX_SLOPE_HARD_DEG - self.MAX_SLOPE_IDEAL_DEG)
            )
        )
        return pd.Series(scores, index=parcel_index)

    def _score_utility_coverage(self, parcels_dist: gpd.GeoDataFrame) -> pd.Series:
        """
        Utility availability on the parcel itself.
        1.0 = water + sewage both present, 0.5 = one, 0.0 = neither.
        Note: flood-based ACCESS to utilities is handled separately in
        score_access_resilience which penalises the whole neighbourhood's
        road network reliability.
        """
        utilities  = self.layers["utilities"]
        water_poly = utilities[utilities["utility_type"] == "water"]
        sewer_poly = utilities[utilities["utility_type"] == "sewage"]

        def has_coverage(geom, cov_gdf) -> bool:
            return len(cov_gdf) > 0 and cov_gdf.geometry.intersects(geom).any()

        water_cov = parcels_dist.geometry.apply(
            lambda g: has_coverage(g, water_poly)
        ).astype(float)
        sewer_cov = parcels_dist.geometry.apply(
            lambda g: has_coverage(g, sewer_poly)
        ).astype(float)

        return (water_cov + sewer_cov) / 2.0
