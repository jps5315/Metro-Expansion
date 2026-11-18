"""
engine/suitability/base.py

Abstract base class for all facility suitability analyzers in the
Metro Expansion Planner (MEP) system.

Design philosophy:
- Each facility type (hospital, school, factory, etc.) subclasses this.
- The base class enforces a consistent pipeline:
    1. load_layers()   — pull relevant spatial layers from PostGIS
    2. apply_hard_constraints() — eliminate parcels that CANNOT be used (binary)
    3. score_criteria()         — score remaining parcels on soft criteria (0–1)
    4. compute_weighted_score() — combine scores using configured weights
    5. rank_candidates()        — return sorted GeoDataFrame of results
- Subclasses override `HARD_CONSTRAINTS`, `SCORING_CRITERIA`, and `DEFAULT_WEIGHTS`.

Author: MEP Team
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

import geopandas as gpd
import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from engine.projection.crs_strategy import OperationType, ProjectionStrategy

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class SuitabilityConfig:
    """
    Runtime configuration for a suitability analysis run.

    Attributes
    ----------
    db_url : str
        SQLAlchemy connection string for the PostGIS database.
    projection : ProjectionStrategy
        Runtime-selected CRS strategy derived from the study area geometry.
        Provides separate projections for distance, area, angle, and display
        operations. Never hardcode a CRS — always derive from the study area.
        See engine/projection/crs_strategy.py.
    weights : dict
        Override the facility-type defaults. Keys are criterion names,
        values are relative weights (they will be normalised to sum=1).
    min_parcel_area_m2 : float
        Hard minimum parcel size in square metres.
    scenario_id : str
        Identifier for this analysis run — used when persisting results.
    """
    db_url: str
    projection: ProjectionStrategy
    weights: dict[str, float] = field(default_factory=dict)
    min_parcel_area_m2: float = 1000.0
    scenario_id: str = "default"


@dataclass
class SuitabilityResult:
    """
    Container for the output of a completed suitability run.

    Attributes
    ----------
    candidates : gpd.GeoDataFrame
        Ranked parcel candidates with all intermediate scores attached.
        Columns guaranteed: parcel_id, geometry, final_score, rank.
    facility_type : str
        The facility category this result was generated for.
    scenario_id : str
        Links back to the scenario that produced this result.
    n_input_parcels : int
        How many parcels entered the pipeline.
    n_after_hard_filter : int
        How many survived the hard constraints filter.
    metadata : dict
        Weights used, run timestamp, CRS, and other audit fields.
    """
    candidates: gpd.GeoDataFrame
    facility_type: str
    scenario_id: str
    n_input_parcels: int
    n_after_hard_filter: int
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Base analyser
# ---------------------------------------------------------------------------

class BaseSuitabilityAnalyzer(ABC):
    """
    Abstract base for all MEP suitability analysers.

    Subclasses MUST define:
        FACILITY_TYPE : str              e.g. "hospital"
        DEFAULT_WEIGHTS : dict[str,float] criterion_name -> weight
        MIN_PARCEL_AREA_M2 : float       facility-specific minimum area

    Subclasses MUST implement:
        load_layers()
        apply_hard_constraints()
        score_criteria()
    """

    FACILITY_TYPE: str = ""
    DEFAULT_WEIGHTS: dict[str, float] = {}
    MIN_PARCEL_AREA_M2: float = 1000.0

    def __init__(self, config: SuitabilityConfig) -> None:
        self.config = config
        self._engine: Engine | None = None

        # Merge default weights with any caller-supplied overrides
        self.weights = {**self.DEFAULT_WEIGHTS, **(config.weights or {})}
        self._normalise_weights()

        # Will be populated by load_layers()
        self.parcels: gpd.GeoDataFrame | None = None
        self.layers: dict[str, gpd.GeoDataFrame] = {}

        # Convenience shorthand used throughout subclasses
        self.proj = config.projection

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> SuitabilityResult:
        """
        Execute the full suitability pipeline and return ranked candidates.
        """
        logger.info(
            "Starting suitability analysis | facility=%s | scenario=%s",
            self.FACILITY_TYPE, self.config.scenario_id
        )

        self._engine = create_engine(self.config.db_url)

        # Step 1 — load data
        self.load_layers()
        n_input = len(self.parcels)
        logger.info("Loaded %d parcels", n_input)

        # Step 2 — hard constraints (binary filter)
        self.parcels = self.apply_hard_constraints(self.parcels)
        n_filtered = len(self.parcels)
        logger.info(
            "After hard constraints: %d parcels remain (removed %d)",
            n_filtered, n_input - n_filtered
        )

        if n_filtered == 0:
            logger.warning("No parcels survived hard constraints — returning empty result")
            return self._empty_result(n_input)

        # Step 3 — score soft criteria
        self.parcels = self.score_criteria(self.parcels)

        # Step 4 — weighted composite score
        self.parcels = self.compute_weighted_score(self.parcels)

        # Step 5 — rank
        candidates = self.rank_candidates(self.parcels)

        return SuitabilityResult(
            candidates=candidates,
            facility_type=self.FACILITY_TYPE,
            scenario_id=self.config.scenario_id,
            n_input_parcels=n_input,
            n_after_hard_filter=n_filtered,
            metadata={
                "weights_used": self.weights,
                "crs": self.config.target_crs,
                "facility_type": self.FACILITY_TYPE,
            }
        )

    # ------------------------------------------------------------------
    # Abstract methods — subclasses implement these
    # ------------------------------------------------------------------

    @abstractmethod
    def load_layers(self) -> None:
        """
        Pull all required spatial layers from PostGIS into self.parcels
        and self.layers. Must reproject everything to self.config.target_crs.
        """

    @abstractmethod
    def apply_hard_constraints(self, parcels: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        """
        Remove parcels that are categorically unsuitable.
        Returns a filtered GeoDataFrame. Must NOT modify in place.

        Examples of hard constraints:
        - Parcel is in a 100-year flood zone
        - Parcel is zoned residential-only
        - Parcel area < minimum required
        - Parcel overlaps a protected environmental area
        """

    @abstractmethod
    def score_criteria(self, parcels: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        """
        Add score columns (named score_<criterion>) to the parcels frame.
        All score columns must be normalised to [0, 1] where 1 = most suitable.

        Examples: score_road_proximity, score_population_density, etc.
        """

    # ------------------------------------------------------------------
    # Shared concrete methods
    # ------------------------------------------------------------------

    def compute_weighted_score(self, parcels: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        """
        Combine individual criterion scores into a single composite score
        using the configured weights. Formula: Σ(weight_i * score_i).

        Score columns are expected to follow the naming convention: score_<name>
        where <name> matches a key in self.weights.
        """
        parcels = parcels.copy()
        composite = pd.Series(np.zeros(len(parcels)), index=parcels.index)

        for criterion, weight in self.weights.items():
            col = f"score_{criterion}"
            if col not in parcels.columns:
                logger.warning("Score column '%s' missing — skipping in composite", col)
                continue
            composite += weight * parcels[col]

        parcels["final_score"] = composite
        return parcels

    def rank_candidates(self, parcels: gpd.GeoDataFrame, top_n: int = 50) -> gpd.GeoDataFrame:
        """
        Sort by final_score descending and assign integer ranks (1 = best).
        Returns top_n candidates.
        """
        ranked = (
            parcels
            .sort_values("final_score", ascending=False)
            .head(top_n)
            .copy()
        )
        ranked["rank"] = range(1, len(ranked) + 1)
        return ranked.reset_index(drop=True)

    # ------------------------------------------------------------------
    # Utility methods available to subclasses
    # ------------------------------------------------------------------

    def load_geodataframe(
        self,
        query: str,
        operation: OperationType = OperationType.DISTANCE,
        geom_col: str = "geometry"
    ) -> gpd.GeoDataFrame:
        """
        Execute a spatial SQL query and return a GeoDataFrame reprojected
        to the correct CRS for the specified operation type.

        Parameters
        ----------
        query : str
            SQL query — must SELECT a geometry column named geom_col.
            Do NOT include ST_Transform in the query — this method handles it.
        operation : OperationType
            What spatial operation the data will be used for.
            Determines which CRS is applied:
                DISTANCE → crs_distance  (UTM or AEQD — metres, equidistant)
                AREA     → crs_area      (LAEA — equal-area)
                ANGLE    → crs_angle     (conformal — angle-preserving)
                DISPLAY  → crs_display   (EPSG:3857 — Web Mercator)
        geom_col : str
            Name of the geometry column in the query result.

        Notes
        -----
        PostGIS stores all data in EPSG:4326 (WGS84). We pull it in
        geographic coordinates and reproject in Python via pyproj.
        For very large tables, consider doing ST_Transform server-side.
        """
        gdf = gpd.read_postgis(
            sql=text(query),
            con=self._engine,
            geom_col=geom_col,
            crs="EPSG:4326"
        )
        return self.proj.reproject(gdf, operation)

    @staticmethod
    def normalise_column(series: pd.Series) -> pd.Series:
        """
        Min-max normalise a Series to [0, 1].
        Returns zeros if the range is zero (all values identical).
        """
        min_val, max_val = series.min(), series.max()
        if max_val == min_val:
            return pd.Series(np.zeros(len(series)), index=series.index)
        return (series - min_val) / (max_val - min_val)

    @staticmethod
    def distance_decay_score(distances_m: pd.Series, ideal_m: float, max_m: float) -> pd.Series:
        """
        Convert raw distances (metres) to a [0,1] score where:
        - distances <= ideal_m  score = 1.0
        - distances >= max_m    score = 0.0
        - in between            linear decay

        Useful for "proximity to X is desirable" criteria.
        """
        scores = np.where(
            distances_m <= ideal_m, 1.0,
            np.where(
                distances_m >= max_m, 0.0,
                1.0 - (distances_m - ideal_m) / (max_m - ideal_m)
            )
        )
        return pd.Series(scores, index=distances_m.index)

    @staticmethod
    def buffer_exclusion_score(
        distances_m: pd.Series,
        min_buffer_m: float
    ) -> pd.Series:
        """
        Hard buffer expressed as a soft score for cases where you want
        graduated penalty rather than binary exclusion.
        Score = 0 if within min_buffer, else linearly rises to 1.
        """
        scores = np.where(
            distances_m < min_buffer_m, 0.0,
            np.minimum(1.0, (distances_m - min_buffer_m) / min_buffer_m)
        )
        return pd.Series(scores, index=distances_m.index)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _normalise_weights(self) -> None:
        """Ensure weights sum to 1.0."""
        total = sum(self.weights.values())
        if total == 0:
            raise ValueError(f"Weights for {self.FACILITY_TYPE} sum to zero")
        self.weights = {k: v / total for k, v in self.weights.items()}

    def _empty_result(self, n_input: int) -> SuitabilityResult:
        return SuitabilityResult(
            candidates=gpd.GeoDataFrame(),
            facility_type=self.FACILITY_TYPE,
            scenario_id=self.config.scenario_id,
            n_input_parcels=n_input,
            n_after_hard_filter=0,
            metadata={"weights_used": self.weights}
        )
