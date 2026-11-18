"""
engine/projection/crs_strategy.py

Runtime CRS (Coordinate Reference System) strategy for the Metro Expansion
Planner (MEP). Automatically selects the correct projection for each spatial
operation type based on the geographic location of the study area.

Background
----------
No single map projection preserves all spatial properties simultaneously.
Tissot's theorem proves it is mathematically impossible. The three properties
we care about in GIS analysis are:

    Distance (equidistant)  — preserved along specific lines from a centre point
    Area     (equal-area)   — shape is distorted but areas are correct
    Angle    (conformal)    — local shapes are correct, angles are preserved

Each operation in the suitability engine must use the right CRS for the
property it depends on. Using the wrong projection introduces silent errors
that are very hard to detect — a distance buffer in EPSG:4326 (degrees) will
be wildly wrong near the poles and subtly wrong even at mid-latitudes.

Design
------
ProjectionStrategy is instantiated once per analysis run from the study area
geometry (city boundary or bounding box). It exposes three CRS properties:

    .crs_distance   UTM zone auto-selected from centroid longitude, or
                    Azimuthal Equidistant for large / cross-zone study areas.

    .crs_area       Lambert Azimuthal Equal-Area centred on study area centroid.
                    Custom proj4 string built at runtime — no fixed EPSG code.

    .crs_display    Always EPSG:3857 (Web Mercator). All frontend map libraries
                    expect this. Never used for analysis.

Usage in suitability engine
---------------------------
    strategy = ProjectionStrategy.from_geometry(city_boundary_wgs84)

    # Reproject for distance-based ops (buffers, proximity scoring)
    gdf_dist = gdf.to_crs(strategy.crs_distance)

    # Reproject for area-based ops (parcel area calculation, coverage %)
    gdf_area = gdf.to_crs(strategy.crs_area)

    # Reproject for frontend output
    gdf_out = result.to_crs(strategy.crs_display)

Author: MEP Team
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

import geopandas as gpd
import numpy as np
from pyproj import CRS, Transformer
from shapely.geometry import MultiPolygon, Polygon, box
from shapely.geometry.base import BaseGeometry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# UTM covers ±80° latitude reliably. Beyond this we need polar projections.
UTM_MAX_LATITUDE = 80.0

# If the study area spans more than this many degrees of longitude, a single
# UTM zone introduces significant distortion at the edges. We switch to
# Azimuthal Equidistant centred on the centroid instead.
UTM_MAX_SPAN_DEGREES = 6.0

# Minimum study area diagonal (km) below which we treat it as "local"
# and can use the simpler UTM approach even at high latitudes.
LOCAL_AREA_THRESHOLD_KM = 50.0


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class OperationType(Enum):
    """
    The spatial operation being performed.
    Used to select the correct CRS from the strategy.
    """
    DISTANCE  = auto()   # buffer, proximity, nearest-neighbour
    AREA      = auto()   # parcel area, coverage percentage
    ANGLE     = auto()   # bearing, aspect, flow direction
    DISPLAY   = auto()   # frontend rendering only


class ProjectionFamily(Enum):
    """
    Which projection family was selected for the distance CRS.
    Exposed for logging and audit.
    """
    UTM                   = "UTM"
    AZIMUTHAL_EQUIDISTANT = "Azimuthal Equidistant"
    UPS_NORTH             = "Universal Polar Stereographic (North)"
    UPS_SOUTH             = "Universal Polar Stereographic (South)"


# ---------------------------------------------------------------------------
# Study area descriptor
# ---------------------------------------------------------------------------

@dataclass
class StudyAreaDescriptor:
    """
    Geographic summary of the study area derived from its boundary geometry.

    Attributes
    ----------
    centroid_lon, centroid_lat : float
        WGS84 centroid of the study area.
    lon_span, lat_span : float
        Width and height of the bounding box in decimal degrees.
    diagonal_km : float
        Approximate diagonal extent in kilometres (Haversine).
    crosses_antimeridian : bool
        True if the study area straddles the 180° meridian.
    crosses_utm_boundary : bool
        True if the bounding box spans more than one UTM zone.
    """
    centroid_lon: float
    centroid_lat: float
    lon_span: float
    lat_span: float
    diagonal_km: float
    crosses_antimeridian: bool
    crosses_utm_boundary: bool

    @classmethod
    def from_geometry(cls, geom: BaseGeometry) -> StudyAreaDescriptor:
        """
        Derive a StudyAreaDescriptor from a Shapely geometry in WGS84.
        """
        bounds = geom.bounds  # (minx, miny, maxx, maxy) in WGS84 degrees
        minx, miny, maxx, maxy = bounds

        centroid = geom.centroid
        centroid_lon = centroid.x
        centroid_lat = centroid.y

        lon_span = maxx - minx
        lat_span = maxy - miny

        diagonal_km = _haversine_km(miny, minx, maxy, maxx)

        # Antimeridian: bounding box crosses 180° longitude
        crosses_antimeridian = minx < -170 and maxx > 170

        # UTM boundary: study area spans > UTM_MAX_SPAN_DEGREES of longitude
        crosses_utm_boundary = lon_span > UTM_MAX_SPAN_DEGREES

        return cls(
            centroid_lon=centroid_lon,
            centroid_lat=centroid_lat,
            lon_span=lon_span,
            lat_span=lat_span,
            diagonal_km=diagonal_km,
            crosses_antimeridian=crosses_antimeridian,
            crosses_utm_boundary=crosses_utm_boundary,
        )


# ---------------------------------------------------------------------------
# Core projection strategy
# ---------------------------------------------------------------------------

@dataclass
class ProjectionStrategy:
    """
    Runtime CRS strategy for a specific study area.

    Instantiate via ProjectionStrategy.from_geometry() — do not construct
    directly unless you know exactly what CRS objects to pass.

    Attributes
    ----------
    crs_distance : CRS
        Best equidistant CRS for this study area.
        Use for: ST_Distance, ST_Buffer, proximity scoring, catchment radii.

    crs_area : CRS
        Best equal-area CRS for this study area.
        Use for: parcel area calculation, coverage percentage, population grids.

    crs_angle : CRS
        Best conformal CRS for this study area.
        Use for: bearing calculations, aspect/slope direction, flow modelling.

    crs_display : CRS
        Always EPSG:3857. Use only for frontend output — never for analysis.

    descriptor : StudyAreaDescriptor
        The geographic summary that drove the CRS selection.

    projection_family : ProjectionFamily
        Which family was selected for crs_distance (for logging/audit).
    """
    crs_distance: CRS
    crs_area: CRS
    crs_angle: CRS
    crs_display: CRS
    descriptor: StudyAreaDescriptor
    projection_family: ProjectionFamily

    # -----------------------------------------------------------------------
    # Factory method — primary entry point
    # -----------------------------------------------------------------------

    @classmethod
    def from_geometry(cls, study_area_geom: BaseGeometry) -> ProjectionStrategy:
        """
        Build a ProjectionStrategy from a study area geometry in WGS84.

        Parameters
        ----------
        study_area_geom : Shapely geometry in EPSG:4326 (WGS84 lon/lat)
            Can be Point, Polygon, MultiPolygon, or any geometry type.
            If a GeoDataFrame is available, pass .unary_union.

        Returns
        -------
        ProjectionStrategy with all three CRS properties populated.

        Raises
        ------
        ValueError
            If the geometry is empty or not in WGS84 range.
        """
        if study_area_geom is None or study_area_geom.is_empty:
            raise ValueError("study_area_geom must be a non-empty Shapely geometry")

        desc = StudyAreaDescriptor.from_geometry(study_area_geom)

        logger.info(
            "Building ProjectionStrategy | centroid=(%.4f, %.4f) | "
            "span=(%.2f°lon, %.2f°lat) | diagonal=%.1fkm",
            desc.centroid_lon, desc.centroid_lat,
            desc.lon_span, desc.lat_span, desc.diagonal_km
        )

        crs_distance, family = cls._select_distance_crs(desc)
        crs_area             = cls._build_laea_crs(desc)
        crs_angle            = cls._select_conformal_crs(desc)
        crs_display          = CRS.from_epsg(3857)

        strategy = cls(
            crs_distance=crs_distance,
            crs_area=crs_area,
            crs_angle=crs_angle,
            crs_display=crs_display,
            descriptor=desc,
            projection_family=family,
        )

        logger.info(
            "ProjectionStrategy ready | "
            "distance=%s (%s) | area=LAEA(custom) | angle=%s",
            strategy.crs_distance.name,
            family.value,
            strategy.crs_angle.name,
        )

        return strategy

    @classmethod
    def from_epsg(cls, epsg_code: int) -> ProjectionStrategy:
        """
        Convenience factory when the caller already knows the EPSG code
        for the study area and wants a strategy built from it.
        Useful for test fixtures and simple scripts.
        """
        crs = CRS.from_epsg(epsg_code)
        # Extract centroid from CRS geographic bounds if available
        bounds = crs.area_of_use
        if bounds:
            centroid_lon = (bounds.west + bounds.east) / 2
            centroid_lat = (bounds.south + bounds.north) / 2
            geom = box(bounds.west, bounds.south, bounds.east, bounds.north)
        else:
            raise ValueError(
                f"EPSG:{epsg_code} has no area_of_use metadata. "
                "Use from_geometry() instead."
            )
        return cls.from_geometry(geom)

    # -----------------------------------------------------------------------
    # Public convenience methods
    # -----------------------------------------------------------------------

    def reproject(
        self,
        gdf: gpd.GeoDataFrame,
        operation: OperationType
    ) -> gpd.GeoDataFrame:
        """
        Reproject a GeoDataFrame to the correct CRS for the given operation.

        Parameters
        ----------
        gdf : GeoDataFrame — must have a valid .crs set
        operation : OperationType — what spatial operation will be performed

        Returns
        -------
        GeoDataFrame reprojected to the appropriate CRS.

        Example
        -------
            parcels_for_buffering = strategy.reproject(parcels, OperationType.DISTANCE)
            parcels_for_area      = strategy.reproject(parcels, OperationType.AREA)
        """
        target_crs = self._crs_for_operation(operation)

        if gdf.crs is None:
            raise ValueError(
                "GeoDataFrame has no CRS set. "
                "Set it with gdf.set_crs(epsg=4326) before reprojecting."
            )

        if gdf.crs == target_crs:
            return gdf  # already in target CRS, no-op

        return gdf.to_crs(target_crs)

    def reproject_back_to_wgs84(self, gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        """
        Convenience method: reproject any GeoDataFrame back to WGS84.
        Use before persisting results to PostGIS (which stores in EPSG:4326).
        """
        return gdf.to_crs(CRS.from_epsg(4326))

    def describe(self) -> dict:
        """
        Return a human-readable summary of the selected projections.
        Useful for logging, API responses, and planner audit reports.
        """
        d = self.descriptor
        return {
            "study_area": {
                "centroid": {"lon": d.centroid_lon, "lat": d.centroid_lat},
                "span_degrees": {"lon": d.lon_span, "lat": d.lat_span},
                "diagonal_km": round(d.diagonal_km, 1),
                "crosses_utm_boundary": d.crosses_utm_boundary,
                "crosses_antimeridian": d.crosses_antimeridian,
            },
            "selected_projections": {
                "distance": {
                    "name": self.crs_distance.name,
                    "family": self.projection_family.value,
                    "epsg": self._safe_epsg(self.crs_distance),
                },
                "area": {
                    "name": self.crs_area.name,
                    "family": "Lambert Azimuthal Equal-Area (custom centre)",
                    "epsg": None,   # custom CRS has no EPSG code
                },
                "angle": {
                    "name": self.crs_angle.name,
                    "family": "Conformal",
                    "epsg": self._safe_epsg(self.crs_angle),
                },
                "display": {
                    "name": "Web Mercator",
                    "epsg": 3857,
                },
            },
        }

    # -----------------------------------------------------------------------
    # Private CRS selection logic
    # -----------------------------------------------------------------------

    @classmethod
    def _select_distance_crs(
        cls,
        desc: StudyAreaDescriptor
    ) -> tuple[CRS, ProjectionFamily]:
        """
        Select the best equidistant CRS for the study area.

        Decision tree:
        1. Polar regions (|lat| > UTM_MAX_LATITUDE) → UPS (polar stereographic)
        2. Large / cross-zone study areas → Azimuthal Equidistant centred on centroid
        3. Standard case → UTM zone computed from centroid longitude
        """
        lat = desc.centroid_lat
        lon = desc.centroid_lon

        # --- Case 1: Polar regions ---
        if abs(lat) > UTM_MAX_LATITUDE:
            if lat > 0:
                logger.info("Polar region detected (lat=%.2f) → UPS North", lat)
                return CRS.from_epsg(5041), ProjectionFamily.UPS_NORTH
            else:
                logger.info("Polar region detected (lat=%.2f) → UPS South", lat)
                return CRS.from_epsg(5042), ProjectionFamily.UPS_SOUTH

        # --- Case 2: Large study area spanning multiple UTM zones ---
        if desc.crosses_utm_boundary or desc.diagonal_km > 500:
            logger.info(
                "Large study area (%.1fkm diagonal, %.2f° lon span) → "
                "Azimuthal Equidistant centred on (%.4f, %.4f)",
                desc.diagonal_km, desc.lon_span, lon, lat
            )
            crs = cls._build_aeqd_crs(desc)
            return crs, ProjectionFamily.AZIMUTHAL_EQUIDISTANT

        # --- Case 3: Standard — UTM zone from centroid longitude ---
        utm_zone = int((lon + 180) / 6) + 1
        # Clamp to valid range 1-60 (handles edge case at exactly 180°)
        utm_zone = max(1, min(60, utm_zone))

        # Special UTM zone exceptions per international convention:
        # Zone 32 is widened to cover Svalbard at 72-84°N
        # Zones 31-37 have irregular boundaries at 72-84°N (Norway/Svalbard)
        utm_zone = cls._apply_utm_exceptions(utm_zone, lat, lon)

        # EPSG codes: 32601–32660 (north), 32701–32760 (south)
        if lat >= 0:
            epsg = 32600 + utm_zone
        else:
            epsg = 32700 + utm_zone

        logger.info(
            "Standard case → UTM Zone %d%s (EPSG:%d)",
            utm_zone, "N" if lat >= 0 else "S", epsg
        )
        return CRS.from_epsg(epsg), ProjectionFamily.UTM

    @staticmethod
    def _apply_utm_exceptions(zone: int, lat: float, lon: float) -> int:
        """
        Apply the standard UTM zone exceptions for Norway and Svalbard.

        These are geopolitical exceptions built into the UTM standard:
        - Zone 32 is extended westward to cover SW Norway (56-64°N, 3-12°E)
        - Svalbard (72-84°N) uses zones 33 and 35 (wider), skipping 32, 34, 36
        """
        # Norway exception: 56°N–64°N, 3°E–12°E → zone 32 instead of 31
        if 56 <= lat < 64 and 3 <= lon < 12:
            return 32

        # Svalbard exceptions (72°N–84°N)
        if 72 <= lat < 84:
            if 0 <= lon < 9:
                return 31
            elif 9 <= lon < 21:
                return 33
            elif 21 <= lon < 33:
                return 33
            elif 33 <= lon < 42:
                return 35

        return zone

    @staticmethod
    def _build_laea_crs(desc: StudyAreaDescriptor) -> CRS:
        """
        Build a custom Lambert Azimuthal Equal-Area CRS centred on the
        study area centroid using a proj4 string.

        LAEA preserves area everywhere — not just along a standard parallel
        — which makes it ideal for parcel area calculations in any location.
        The projection is centred on the city centroid so distortion is
        minimised within the study area.

        No fixed EPSG code exists for a custom-centred LAEA — we build
        it from the proj4 definition directly.
        """
        proj4 = (
            f"+proj=laea "
            f"+lat_0={desc.centroid_lat:.6f} "
            f"+lon_0={desc.centroid_lon:.6f} "
            f"+x_0=0 +y_0=0 "
            f"+datum=WGS84 +units=m +no_defs"
        )
        crs = CRS.from_proj4(proj4)
        logger.debug("Built custom LAEA CRS: %s", proj4)
        return crs

    @staticmethod
    def _build_aeqd_crs(desc: StudyAreaDescriptor) -> CRS:
        """
        Build a custom Azimuthal Equidistant CRS centred on the study area
        centroid. Used for large study areas that span multiple UTM zones.

        AEQD preserves distances FROM the central point in all directions,
        making it the best single equidistant choice when UTM zone boundaries
        would cause distortion across the study area.
        """
        proj4 = (
            f"+proj=aeqd "
            f"+lat_0={desc.centroid_lat:.6f} "
            f"+lon_0={desc.centroid_lon:.6f} "
            f"+x_0=0 +y_0=0 "
            f"+datum=WGS84 +units=m +no_defs"
        )
        crs = CRS.from_proj4(proj4)
        logger.debug("Built custom AEQD CRS: %s", proj4)
        return crs

    @staticmethod
    def _select_conformal_crs(desc: StudyAreaDescriptor) -> CRS:
        """
        Select the best conformal (angle-preserving) CRS for the study area.

        Conformal projections preserve local angles and shapes, making them
        correct for:
            - Bearing / azimuth calculations
            - Terrain aspect and flow direction (slope faces)
            - Wind direction modelling (relevant for waste plant placement)
            - Any operation where angular relationships between features matter

        Selection logic:
            |lat| > 70°         → Polar Stereographic (conformal at poles)
            Large / mid-lat     → Lambert Conformal Conic (standard for
                                  national mapping at mid-latitudes)
            Equatorial (|lat|<15°) → Mercator (conformal at equator)
            Default             → Transverse Mercator (UTM basis) — good
                                  local conformality for city-scale areas
        """
        lat = abs(desc.centroid_lat)
        lon = desc.centroid_lon

        if lat > 70:
            # Polar stereographic
            epsg = 3995 if desc.centroid_lat > 0 else 3031
            logger.info("Conformal CRS: Polar Stereographic (EPSG:%d)", epsg)
            return CRS.from_epsg(epsg)

        if lat < 15:
            # Near equator: Mercator
            logger.info("Conformal CRS: Mercator (equatorial region)")
            proj4 = (
                f"+proj=merc "
                f"+lon_0={lon:.6f} "
                f"+k=1 +x_0=0 +y_0=0 "
                f"+datum=WGS84 +units=m +no_defs"
            )
            return CRS.from_proj4(proj4)

        if desc.diagonal_km > 300 or desc.lon_span > 5:
            # Large mid-latitude area: Lambert Conformal Conic
            # Standard parallels placed at 1/6 and 5/6 of the latitude range
            # (Snyder's recommendation for minimising scale error)
            lat_min = desc.centroid_lat - desc.lat_span / 2
            lat_max = desc.centroid_lat + desc.lat_span / 2
            sp1 = lat_min + (lat_max - lat_min) / 6
            sp2 = lat_max - (lat_max - lat_min) / 6
            proj4 = (
                f"+proj=lcc "
                f"+lat_1={sp1:.4f} +lat_2={sp2:.4f} "
                f"+lat_0={desc.centroid_lat:.6f} "
                f"+lon_0={desc.centroid_lon:.6f} "
                f"+x_0=0 +y_0=0 "
                f"+datum=WGS84 +units=m +no_defs"
            )
            logger.info(
                "Conformal CRS: Lambert Conformal Conic "
                "(sp1=%.2f, sp2=%.2f)", sp1, sp2
            )
            return CRS.from_proj4(proj4)

        # Default: Transverse Mercator centred on study area
        # (same mathematical basis as UTM but centred on our specific meridian)
        proj4 = (
            f"+proj=tmerc "
            f"+lat_0={desc.centroid_lat:.6f} "
            f"+lon_0={desc.centroid_lon:.6f} "
            f"+k=0.9996 +x_0=0 +y_0=0 "
            f"+datum=WGS84 +units=m +no_defs"
        )
        logger.info(
            "Conformal CRS: Transverse Mercator centred on (%.4f, %.4f)",
            desc.centroid_lon, desc.centroid_lat
        )
        return CRS.from_proj4(proj4)

    def _crs_for_operation(self, operation: OperationType) -> CRS:
        mapping = {
            OperationType.DISTANCE: self.crs_distance,
            OperationType.AREA:     self.crs_area,
            OperationType.ANGLE:    self.crs_angle,
            OperationType.DISPLAY:  self.crs_display,
        }
        return mapping[operation]

    @staticmethod
    def _safe_epsg(crs: CRS) -> Optional[int]:
        """Return EPSG code if the CRS has one, None otherwise (custom proj4 CRS)."""
        try:
            return crs.to_epsg()
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Compute the great-circle distance in kilometres between two WGS84 points.
    Used to estimate study area diagonal extent.
    """
    R = 6371.0  # Earth radius in km
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2)
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))
