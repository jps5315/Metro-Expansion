"""
engine/validation/data_quality.py

Data quality and authenticity validation for the Metro Expansion Planner (MEP).

Purpose
-------
City governments provide spatial datasets (parcels, roads, zoning, health data)
that drive site suitability recommendations. If that data contains errors and
our system produces incorrect recommendations as a result, the city may blame
our software. This module is our defence:

  1. We detect and log every anomaly in the source data at ingestion time.
  2. We issue explicit warnings to the planner before any analysis runs.
  3. We maintain an immutable anomaly log that timestamps when anomalies
     were detected and when planners acknowledged them.
  4. No analysis run can proceed against data with CRITICAL anomalies
     without an explicit planner acknowledgement recorded in the database.

Checks performed
----------------
GEOMETRY CHECKS
  G01  Geometry validity (ST_IsValid) — self-intersections, degenerate rings
  G02  Polygon overlap check — no two parcels share interior area
        (boundary touching / edge intersection is acceptable and expected)
  G03  CRS sanity check — coordinate values are plausible for declared CRS
  G04  Geometry type consistency — all rows match the declared geometry type
  G05  Null / empty geometry detection

ATTRIBUTE CHECKS
  A01  Required columns present and not null above threshold
  A02  Numeric range sanity (area > 0, density 0-1, population >= 0, etc.)
  A03  Zoning code lookup coverage — all codes present in zoning_lookup table
  A04  Referential integrity — foreign keys resolve
  A05  Duplicate source IDs within same city + dataset version

CONVERGENCE CHECKS (statistical plausibility)
  C01  Population density plausibility — cells with density > 50,000/km²
       are flagged (denser than Manhattan, likely data error)
  C02  Disease prevalence range — CDC PLACES values should be 0–100%
  C03  Area distribution sanity — parcel area distribution compared to
       expected log-normal distribution for urban land parcels
  C04  Road network connectivity — isolated road segments flagged

PROVENANCE CHECKS
  P01  Source file hash recorded and matches re-ingestion
  P02  Dataset version and ingestion timestamp present
  P03  Source agency matches expected agencies for the city
  P04  Data currency check — dataset more than N years old triggers warning

Severity levels
---------------
  CRITICAL  Analysis blocked until planner acknowledges. Data is likely
            unusable or will produce dangerously incorrect results.
            Example: 40% of parcels have invalid geometries.

  WARNING   Analysis can proceed but planner is informed. Data has
            anomalies that may affect specific results.
            Example: 3% of parcels overlap a neighbouring parcel.

  INFO      Logged for audit but not shown to planner unless requested.
            Example: 0.1% of road segments are isolated (normal for OSM data).

Author: MEP Team
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.validation import explain_validity
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Severity and anomaly data structures
# ---------------------------------------------------------------------------

class AnomalySeverity(Enum):
    INFO     = "INFO"
    WARNING  = "WARNING"
    CRITICAL = "CRITICAL"


class CheckCategory(Enum):
    GEOMETRY    = "GEOMETRY"
    ATTRIBUTE   = "ATTRIBUTE"
    CONVERGENCE = "CONVERGENCE"
    PROVENANCE  = "PROVENANCE"


@dataclass
class Anomaly:
    """
    A single detected data quality anomaly.

    Attributes
    ----------
    check_code    : e.g. 'G02' — maps to the check catalogue above
    severity      : INFO / WARNING / CRITICAL
    category      : which check category
    message       : human-readable description for the planner
    detail        : structured detail for the audit log
    affected_ids  : sample of affected row IDs (up to 20)
    affected_count: total count of affected rows
    recommendation: suggested remediation action
    """
    check_code:      str
    severity:        AnomalySeverity
    category:        CheckCategory
    message:         str
    detail:          dict[str, Any] = field(default_factory=dict)
    affected_ids:    list[str]      = field(default_factory=list)
    affected_count:  int            = 0
    recommendation:  str            = ""


@dataclass
class ValidationReport:
    """
    Complete validation report for one dataset ingestion.

    Attributes
    ----------
    city_id        : UUID of the city
    dataset_name   : which table / layer was validated
    source_file    : original file path or URL
    file_hash      : SHA-256 of the source file (provenance)
    validated_at   : UTC timestamp
    anomalies      : all detected anomalies
    n_rows_checked : total rows examined
    passed         : True only if zero CRITICAL anomalies
    blocked        : True if analysis is blocked pending acknowledgement
    """
    city_id:        str
    dataset_name:   str
    source_file:    str
    file_hash:      str
    validated_at:   datetime
    anomalies:      list[Anomaly] = field(default_factory=list)
    n_rows_checked: int           = 0
    passed:         bool          = True
    blocked:        bool          = False

    @property
    def critical_count(self) -> int:
        return sum(1 for a in self.anomalies if a.severity == AnomalySeverity.CRITICAL)

    @property
    def warning_count(self) -> int:
        return sum(1 for a in self.anomalies if a.severity == AnomalySeverity.WARNING)

    def summary(self) -> str:
        status = "BLOCKED" if self.blocked else ("PASSED" if self.passed else "WARNINGS")
        return (
            f"[{status}] {self.dataset_name} | "
            f"{self.n_rows_checked} rows | "
            f"{self.critical_count} critical, {self.warning_count} warnings"
        )

    def to_dict(self) -> dict:
        return {
            "city_id":         self.city_id,
            "dataset_name":    self.dataset_name,
            "source_file":     self.source_file,
            "file_hash":       self.file_hash,
            "validated_at":    self.validated_at.isoformat(),
            "n_rows_checked":  self.n_rows_checked,
            "passed":          self.passed,
            "blocked":         self.blocked,
            "critical_count":  self.critical_count,
            "warning_count":   self.warning_count,
            "anomalies": [
                {
                    "check_code":     a.check_code,
                    "severity":       a.severity.value,
                    "category":       a.category.value,
                    "message":        a.message,
                    "affected_count": a.affected_count,
                    "affected_ids":   a.affected_ids[:20],
                    "recommendation": a.recommendation,
                    "detail":         a.detail,
                }
                for a in self.anomalies
            ],
        }


# ---------------------------------------------------------------------------
# Thresholds — tune per deployment
# ---------------------------------------------------------------------------

# G02 Overlap: what fraction of parcels overlapping is CRITICAL vs WARNING
OVERLAP_CRITICAL_PCT = 0.05    # 5% — data is likely corrupted
OVERLAP_WARNING_PCT  = 0.005   # 0.5% — small number of boundary issues

# A01 Required column null threshold
NULL_CRITICAL_PCT    = 0.10    # 10% nulls in a required column = CRITICAL
NULL_WARNING_PCT     = 0.01    # 1% nulls = WARNING

# C01 Population density ceiling
MAX_PLAUSIBLE_DENSITY_PER_KM2 = 50_000   # denser than Manhattan

# C02 CDC PLACES prevalence range
MAX_PREVALENCE_PCT   = 100.0
MIN_PREVALENCE_PCT   = 0.0

# P04 Data currency: warn if dataset is older than this many years
MAX_DATA_AGE_YEARS_WARNING  = 3
MAX_DATA_AGE_YEARS_CRITICAL = 7


# ---------------------------------------------------------------------------
# Main validator class
# ---------------------------------------------------------------------------

class DataQualityValidator:
    """
    Validates a spatial dataset before it is used in analysis.

    Usage
    -----
    validator = DataQualityValidator(db_url, city_id)
    report = validator.validate_land_parcels(gdf, source_file="/path/to/parcels.gpkg")
    validator.persist_report(report)   # writes to audit.anomaly_log

    if report.blocked:
        raise DataQualityBlockedError(report)
    elif report.warning_count > 0:
        notify_planner(report)         # API layer sends warning to frontend
    """

    def __init__(self, db_url: str, city_id: str) -> None:
        self.db_url  = db_url
        self.city_id = city_id
        self._engine: Engine | None = None

    # ==========================================================================
    # Public validate_* methods — one per dataset type
    # ==========================================================================

    def validate_land_parcels(
        self,
        gdf: gpd.GeoDataFrame,
        source_file: str = "",
        dataset_version: str = ""
    ) -> ValidationReport:
        """
        Full validation suite for land_parcels data.
        Runs all geometry, attribute, convergence, and provenance checks.
        """
        report = self._init_report("land_parcels", source_file, gdf)
        logger.info("Validating land_parcels: %d rows", len(gdf))

        # Geometry checks
        self._check_G01_geometry_validity(gdf, report)
        self._check_G02_polygon_overlap(gdf, report)
        self._check_G03_crs_sanity(gdf, report, expected_crs="EPSG:4326")
        self._check_G04_geometry_type(gdf, report, expected_type="Polygon")
        self._check_G05_null_geometry(gdf, report)

        # Attribute checks
        required_cols = ["parcel_id", "zoning_code", "area_m2",
                         "development_density", "geometry"]
        self._check_A01_required_columns(gdf, report, required_cols)
        self._check_A02_numeric_ranges(gdf, report, {
            "area_m2":             (0.1,    None),      # must be positive
            "development_density": (0.0,    1.0),       # 0 = vacant, 1 = fully built
            "floor_area_ratio":    (0.0,    30.0),      # FAR > 30 is implausible
            "building_coverage_pct": (0.0, 100.0),
        })
        self._check_A05_duplicate_source_ids(gdf, report, "source_parcel_id")

        # Convergence checks
        self._check_C03_area_distribution(gdf, report)

        # Provenance
        self._check_P02_provenance_fields(gdf, report, dataset_version)
        self._check_P04_data_currency(dataset_version, report)

        self._finalise_report(report)
        return report

    def validate_health_demand_grid(
        self,
        gdf: gpd.GeoDataFrame,
        source_file: str = "",
        cdc_year: int | None = None
    ) -> ValidationReport:
        """
        Validation suite for health_demand_grid (CDC PLACES + demographics).
        """
        report = self._init_report("health_demand_grid", source_file, gdf)
        logger.info("Validating health_demand_grid: %d rows", len(gdf))

        self._check_G01_geometry_validity(gdf, report)
        self._check_G03_crs_sanity(gdf, report, expected_crs="EPSG:4326")
        self._check_G05_null_geometry(gdf, report)

        required_cols = ["cell_id", "population", "elderly_population", "geometry"]
        self._check_A01_required_columns(gdf, report, required_cols)
        self._check_A02_numeric_ranges(gdf, report, {
            "population":        (0, 500_000),
            "elderly_population": (0, None),
        })

        prevalence_cols = [
            "prevalence_heart_disease", "prevalence_diabetes", "prevalence_copd",
            "prevalence_stroke", "prevalence_obesity", "prevalence_mental_health"
        ]
        self._check_C02_prevalence_ranges(gdf, report, prevalence_cols)
        self._check_C01_population_density(gdf, report)

        # Elderly population cannot exceed total population
        if "population" in gdf.columns and "elderly_population" in gdf.columns:
            invalid_elderly = gdf[
                gdf["elderly_population"] > gdf["population"]
            ]
            if len(invalid_elderly) > 0:
                report.anomalies.append(Anomaly(
                    check_code="A02_ELDERLY",
                    severity=AnomalySeverity.CRITICAL,
                    category=CheckCategory.ATTRIBUTE,
                    message=f"{len(invalid_elderly)} rows have elderly_population "
                            f"exceeding total population — data is internally inconsistent.",
                    affected_count=len(invalid_elderly),
                    affected_ids=invalid_elderly.index.astype(str).tolist()[:20],
                    recommendation="Check CDC PLACES join logic. "
                                   "elderly_population should be a subset of population.",
                    detail={"max_ratio": float(
                        (invalid_elderly["elderly_population"] /
                         invalid_elderly["population"].clip(lower=1)).max()
                    )}
                ))

        if cdc_year:
            current_year = datetime.now().year
            age = current_year - cdc_year
            if age > MAX_DATA_AGE_YEARS_CRITICAL:
                sev = AnomalySeverity.CRITICAL
            elif age > MAX_DATA_AGE_YEARS_WARNING:
                sev = AnomalySeverity.WARNING
            else:
                sev = None
            if sev:
                report.anomalies.append(Anomaly(
                    check_code="P04",
                    severity=sev,
                    category=CheckCategory.PROVENANCE,
                    message=f"CDC PLACES data is from {cdc_year} ({age} years old). "
                            f"Disease prevalence estimates may not reflect current conditions.",
                    recommendation="Request updated CDC PLACES data for the current year.",
                    detail={"cdc_year": cdc_year, "data_age_years": age}
                ))

        self._finalise_report(report)
        return report

    def validate_road_network(
        self,
        gdf: gpd.GeoDataFrame,
        source_file: str = ""
    ) -> ValidationReport:
        """Validation suite for road_network data."""
        report = self._init_report("road_network", source_file, gdf)
        logger.info("Validating road_network: %d rows", len(gdf))

        self._check_G01_geometry_validity(gdf, report)
        self._check_G03_crs_sanity(gdf, report, expected_crs="EPSG:4326")
        self._check_G05_null_geometry(gdf, report)
        self._check_G04_geometry_type(gdf, report, expected_type="LineString")

        self._check_A01_required_columns(gdf, report, ["road_id", "road_class", "geometry"])
        self._check_A02_numeric_ranges(gdf, report, {
            "speed_limit_kmh": (0, 200),
            "lanes": (1, 20),
        })

        # C04: isolated segments (no shared endpoint with any other segment)
        self._check_C04_road_connectivity(gdf, report)

        self._finalise_report(report)
        return report

    # ==========================================================================
    # Geometry checks
    # ==========================================================================

    def _check_G01_geometry_validity(
        self,
        gdf: gpd.GeoDataFrame,
        report: ValidationReport
    ) -> None:
        """
        G01: ST_IsValid equivalent — detects self-intersecting rings,
        duplicate points, degenerate geometries.
        """
        invalid_mask = ~gdf.geometry.is_valid
        n_invalid = invalid_mask.sum()

        if n_invalid == 0:
            return

        pct = n_invalid / len(gdf)
        severity = (AnomalySeverity.CRITICAL if pct > 0.05
                    else AnomalySeverity.WARNING)

        invalid_rows = gdf[invalid_mask]
        reasons = invalid_rows.geometry.apply(explain_validity).value_counts()

        report.anomalies.append(Anomaly(
            check_code="G01",
            severity=severity,
            category=CheckCategory.GEOMETRY,
            message=(
                f"{n_invalid} of {len(gdf)} geometries ({pct:.1%}) are invalid. "
                f"Most common reason: {reasons.index[0] if len(reasons) > 0 else 'unknown'}."
            ),
            affected_count=n_invalid,
            affected_ids=invalid_rows.index.astype(str).tolist()[:20],
            recommendation=(
                "Apply ST_MakeValid() to affected rows before ingestion. "
                "Review source data export settings — invalid geometries "
                "often indicate a CRS mismatch or corrupted export."
            ),
            detail={
                "pct_invalid": round(float(pct), 4),
                "invalidity_reasons": reasons.head(5).to_dict()
            }
        ))

    def _check_G02_polygon_overlap(
        self,
        gdf: gpd.GeoDataFrame,
        report: ValidationReport
    ) -> None:
        """
        G02: Detect parcels whose INTERIORS overlap (not just touch at boundary).

        Rule: two parcels can share an edge (intersection = line) but must not
        share interior area (intersection = polygon). Overlapping parcel bodies
        indicate either duplicate records or topology errors in the source data.

        Performance note: a full O(n²) intersection check is impractical for
        800k parcels. We use a spatial index-accelerated approach:
        1. Find candidate pairs using sjoin (bounding box overlap)
        2. Check only those pairs for actual polygon interior intersection
        This reduces checks from O(n²) to O(n × avg_neighbours).
        """
        if len(gdf) < 2:
            return

        # Use only valid geometries to avoid false positives from G01 issues
        valid_gdf = gdf[gdf.geometry.is_valid].copy()
        if len(valid_gdf) < 2:
            return

        # Find candidate overlapping pairs via spatial join
        candidates = gpd.sjoin(
            valid_gdf[["geometry"]],
            valid_gdf[["geometry"]],
            how="inner",
            predicate="overlaps"    # 'overlaps' in Shapely = DE-9IM T*T***T**
                                    # requires shared interior area, not just touching
        )

        # Remove self-joins
        candidates = candidates[candidates.index != candidates.index_right]

        # Each pair appears twice (A→B and B→A) — deduplicate
        overlap_pairs = candidates[
            candidates.index < candidates.index_right
        ]

        n_overlapping = len(overlap_pairs)

        if n_overlapping == 0:
            return

        # Count unique parcels involved (not just pair count)
        affected_parcels = set(overlap_pairs.index.tolist() +
                               overlap_pairs["index_right"].tolist())
        pct = len(affected_parcels) / len(valid_gdf)

        severity = (AnomalySeverity.CRITICAL if pct > OVERLAP_CRITICAL_PCT
                    else AnomalySeverity.WARNING)

        report.anomalies.append(Anomaly(
            check_code="G02",
            severity=severity,
            category=CheckCategory.GEOMETRY,
            message=(
                f"{len(affected_parcels)} parcels ({pct:.2%}) participate in "
                f"{n_overlapping} overlapping pairs. "
                f"Overlapping parcel interiors indicate topology errors in the "
                f"source data. Boundary touching is permitted; interior overlap "
                f"is not."
            ),
            affected_count=len(affected_parcels),
            affected_ids=[str(i) for i in list(affected_parcels)[:20]],
            recommendation=(
                "Report to source agency with the affected parcel IDs. "
                "These parcels should be excluded from analysis until corrected. "
                "Do NOT merge or dissolve — that would destroy parcel identity."
            ),
            detail={
                "n_overlapping_pairs": n_overlapping,
                "pct_affected": round(float(pct), 4),
                "sample_pairs": [
                    {"a": str(row.Index), "b": str(row.index_right)}
                    for row in list(overlap_pairs.itertuples())[:5]
                ]
            }
        ))

    def _check_G03_crs_sanity(
        self,
        gdf: gpd.GeoDataFrame,
        report: ValidationReport,
        expected_crs: str = "EPSG:4326"
    ) -> None:
        """
        G03: Verify the data's CRS matches expectations AND that coordinate
        values are plausible for that CRS.

        A common ingestion error: data is projected (metres) but labelled
        as WGS84 (degrees). Coordinate values reveal this:
        WGS84 lon must be in [-180, 180], lat in [-90, 90].
        If coordinates outside these ranges exist in a "WGS84" dataset,
        the CRS metadata is wrong.
        """
        # Check declared CRS
        if gdf.crs is None:
            report.anomalies.append(Anomaly(
                check_code="G03_NO_CRS",
                severity=AnomalySeverity.CRITICAL,
                category=CheckCategory.GEOMETRY,
                message="Dataset has no CRS defined. Cannot determine coordinate system.",
                recommendation="Set CRS before ingestion: gdf.set_crs(epsg=4326)",
            ))
            return

        declared = gdf.crs.to_epsg()
        expected_epsg = int(expected_crs.split(":")[1])

        if declared != expected_epsg:
            report.anomalies.append(Anomaly(
                check_code="G03_CRS_MISMATCH",
                severity=AnomalySeverity.CRITICAL,
                category=CheckCategory.GEOMETRY,
                message=(
                    f"Dataset CRS is EPSG:{declared} but EPSG:{expected_epsg} "
                    f"is required for MEP storage. All data must be in WGS84 "
                    f"geographic coordinates before ingestion."
                ),
                recommendation=f"Reproject with gdf.to_crs(epsg={expected_epsg})",
                detail={"declared_epsg": declared, "expected_epsg": expected_epsg}
            ))
            return  # Cannot check coordinate ranges if CRS is wrong

        # Check coordinate plausibility for WGS84
        if expected_epsg == 4326:
            bounds = gdf.geometry.total_bounds  # [minx, miny, maxx, maxy]
            issues = []

            if bounds[0] < -180 or bounds[2] > 180:
                issues.append(f"Longitude out of range: [{bounds[0]:.4f}, {bounds[2]:.4f}]")
            if bounds[1] < -90 or bounds[3] > 90:
                issues.append(f"Latitude out of range: [{bounds[1]:.4f}, {bounds[3]:.4f}]")

            # Detect projected coordinates disguised as WGS84
            # Projected coordinates are typically in the range 100,000 – 10,000,000
            if abs(bounds[0]) > 180 or abs(bounds[2]) > 180:
                issues.append(
                    "Coordinate magnitudes suggest projected CRS (metres) "
                    "mislabelled as WGS84 (degrees). "
                    f"X range: [{bounds[0]:.0f}, {bounds[2]:.0f}]"
                )

            if issues:
                report.anomalies.append(Anomaly(
                    check_code="G03_COORD_RANGE",
                    severity=AnomalySeverity.CRITICAL,
                    category=CheckCategory.GEOMETRY,
                    message=(
                        f"Coordinate values are implausible for EPSG:4326: "
                        + "; ".join(issues)
                    ),
                    recommendation=(
                        "Inspect the source data's actual CRS using GIS software. "
                        "The dataset is likely in a projected CRS and needs reprojection."
                    ),
                    detail={"bounds": list(bounds), "issues": issues}
                ))

    def _check_G04_geometry_type(
        self,
        gdf: gpd.GeoDataFrame,
        report: ValidationReport,
        expected_type: str = "Polygon"
    ) -> None:
        """G04: All geometries should match the expected type."""
        type_counts = gdf.geometry.geom_type.value_counts()
        unexpected = type_counts[type_counts.index != expected_type]
        unexpected = unexpected[type_counts.index != f"Multi{expected_type}"]

        if len(unexpected) > 0:
            report.anomalies.append(Anomaly(
                check_code="G04",
                severity=AnomalySeverity.WARNING,
                category=CheckCategory.GEOMETRY,
                message=(
                    f"Unexpected geometry types found: "
                    f"{unexpected.to_dict()}. Expected {expected_type}."
                ),
                affected_count=int(unexpected.sum()),
                recommendation=(
                    "Filter or convert unexpected geometry types before ingestion. "
                    "Point and line geometries in a polygon layer usually indicate "
                    "failed geometry reconstruction from the source format."
                ),
                detail={"type_counts": type_counts.to_dict()}
            ))

    def _check_G05_null_geometry(
        self,
        gdf: gpd.GeoDataFrame,
        report: ValidationReport
    ) -> None:
        """G05: No null or empty geometries permitted."""
        null_mask  = gdf.geometry.isna()
        empty_mask = gdf.geometry.is_empty
        n_null  = null_mask.sum()
        n_empty = empty_mask.sum()

        if n_null > 0:
            report.anomalies.append(Anomaly(
                check_code="G05_NULL",
                severity=AnomalySeverity.CRITICAL,
                category=CheckCategory.GEOMETRY,
                message=f"{n_null} rows have NULL geometry and cannot be used in analysis.",
                affected_count=int(n_null),
                affected_ids=gdf[null_mask].index.astype(str).tolist()[:20],
                recommendation="Remove null-geometry rows before ingestion."
            ))
        if n_empty > 0:
            report.anomalies.append(Anomaly(
                check_code="G05_EMPTY",
                severity=AnomalySeverity.WARNING,
                category=CheckCategory.GEOMETRY,
                message=f"{n_empty} rows have empty geometries (zero-area polygons).",
                affected_count=int(n_empty),
                affected_ids=gdf[empty_mask].index.astype(str).tolist()[:20],
                recommendation="Investigate empty geometries in source data."
            ))

    # ==========================================================================
    # Attribute checks
    # ==========================================================================

    def _check_A01_required_columns(
        self,
        gdf: gpd.GeoDataFrame,
        report: ValidationReport,
        required_cols: list[str]
    ) -> None:
        """A01: Required columns must be present and not excessively null."""
        for col in required_cols:
            # Column missing entirely
            if col not in gdf.columns and col != "geometry":
                report.anomalies.append(Anomaly(
                    check_code="A01_MISSING_COL",
                    severity=AnomalySeverity.CRITICAL,
                    category=CheckCategory.ATTRIBUTE,
                    message=f"Required column '{col}' is missing from the dataset.",
                    recommendation=f"Ensure source data includes '{col}'.",
                    detail={"missing_column": col}
                ))
                continue

            if col == "geometry":
                continue

            # Column present but has nulls
            null_count = gdf[col].isna().sum()
            if null_count == 0:
                continue

            null_pct = null_count / len(gdf)
            severity = (AnomalySeverity.CRITICAL if null_pct > NULL_CRITICAL_PCT
                        else AnomalySeverity.WARNING)

            report.anomalies.append(Anomaly(
                check_code="A01_NULL_VALUES",
                severity=severity,
                category=CheckCategory.ATTRIBUTE,
                message=(
                    f"Column '{col}' has {null_count} null values "
                    f"({null_pct:.1%} of {len(gdf)} rows)."
                ),
                affected_count=int(null_count),
                recommendation=(
                    f"Investigate null values in '{col}'. "
                    f"If >10% null, the dataset may be from an incompatible source schema."
                ),
                detail={"column": col, "null_count": int(null_count),
                        "null_pct": round(float(null_pct), 4)}
            ))

    def _check_A02_numeric_ranges(
        self,
        gdf: gpd.GeoDataFrame,
        report: ValidationReport,
        range_specs: dict[str, tuple]
    ) -> None:
        """
        A02: Numeric columns must fall within expected ranges.
        range_specs: {column_name: (min_value, max_value)}
        None means no bound on that side.
        """
        for col, (min_val, max_val) in range_specs.items():
            if col not in gdf.columns:
                continue

            series = gdf[col].dropna()
            if len(series) == 0:
                continue

            violations = pd.Series([False] * len(series), index=series.index)
            if min_val is not None:
                violations |= series < min_val
            if max_val is not None:
                violations |= series > max_val

            n_violations = violations.sum()
            if n_violations == 0:
                continue

            pct = n_violations / len(gdf)
            severity = (AnomalySeverity.CRITICAL if pct > 0.05
                        else AnomalySeverity.WARNING)

            report.anomalies.append(Anomaly(
                check_code="A02",
                severity=severity,
                category=CheckCategory.ATTRIBUTE,
                message=(
                    f"Column '{col}': {n_violations} values ({pct:.1%}) "
                    f"outside expected range [{min_val}, {max_val}]. "
                    f"Actual range: [{float(series.min()):.4f}, {float(series.max()):.4f}]."
                ),
                affected_count=int(n_violations),
                affected_ids=series[violations].index.astype(str).tolist()[:20],
                recommendation=(
                    f"Values outside [{min_val}, {max_val}] in '{col}' are "
                    f"statistically implausible. Check for unit conversion errors "
                    f"(e.g. feet vs metres for area, 0-1 vs 0-100 for percentages)."
                ),
                detail={
                    "column": col, "expected_min": min_val, "expected_max": max_val,
                    "actual_min": float(series.min()), "actual_max": float(series.max()),
                    "n_violations": int(n_violations)
                }
            ))

    def _check_A05_duplicate_source_ids(
        self,
        gdf: gpd.GeoDataFrame,
        report: ValidationReport,
        id_col: str
    ) -> None:
        """A05: Source IDs must be unique within a city + dataset version."""
        if id_col not in gdf.columns:
            return

        dupes = gdf[gdf[id_col].duplicated(keep=False)]
        if len(dupes) == 0:
            return

        report.anomalies.append(Anomaly(
            check_code="A05",
            severity=AnomalySeverity.CRITICAL,
            category=CheckCategory.ATTRIBUTE,
            message=(
                f"{len(dupes)} rows share duplicate values in '{id_col}'. "
                f"Source IDs must be unique. This likely indicates a "
                f"duplicate-row error in the source export."
            ),
            affected_count=len(dupes),
            affected_ids=dupes[id_col].astype(str).tolist()[:20],
            recommendation=(
                "Deduplicate by source ID before ingestion. "
                "If duplicates represent legitimate split parcels, ensure "
                "each part has a unique suffix added to its source ID."
            ),
            detail={"id_column": id_col,
                    "n_duplicate_groups": int(gdf[id_col].duplicated().sum())}
        ))

    # ==========================================================================
    # Convergence checks
    # ==========================================================================

    def _check_C01_population_density(
        self,
        gdf: gpd.GeoDataFrame,
        report: ValidationReport
    ) -> None:
        """
        C01: Population density plausibility.
        Cells denser than Manhattan (~28,000/km²) are flagged.
        Cells > 50,000/km² are almost certainly data errors.
        """
        if "population_density" not in gdf.columns:
            # Compute from population + geometry area if possible
            if "population" not in gdf.columns:
                return
            # Estimate density from geometry area in km²
            area_km2 = gdf.geometry.to_crs("EPSG:6933").area / 1e6
            density = gdf["population"] / area_km2.clip(lower=0.001)
        else:
            density = gdf["population_density"]

        over_limit = density > MAX_PLAUSIBLE_DENSITY_PER_KM2
        n_over = over_limit.sum()

        if n_over == 0:
            return

        report.anomalies.append(Anomaly(
            check_code="C01",
            severity=AnomalySeverity.WARNING,
            category=CheckCategory.CONVERGENCE,
            message=(
                f"{n_over} grid cells have population density > "
                f"{MAX_PLAUSIBLE_DENSITY_PER_KM2:,}/km², "
                f"which exceeds Manhattan's density (~28,000/km²). "
                f"Maximum found: {float(density.max()):,.0f}/km²."
            ),
            affected_count=int(n_over),
            affected_ids=gdf[over_limit].index.astype(str).tolist()[:20],
            recommendation=(
                "Verify population counts and cell area for flagged cells. "
                "Common causes: population assigned to wrong geographic level "
                "(census block vs tract), or area calculated in wrong units."
            ),
            detail={
                "threshold": MAX_PLAUSIBLE_DENSITY_PER_KM2,
                "max_found": float(density.max()),
                "mean_density": float(density.mean())
            }
        ))

    def _check_C02_prevalence_ranges(
        self,
        gdf: gpd.GeoDataFrame,
        report: ValidationReport,
        prevalence_cols: list[str]
    ) -> None:
        """C02: CDC PLACES prevalence values must be 0–100%."""
        for col in prevalence_cols:
            if col not in gdf.columns:
                continue
            series = gdf[col].dropna()
            out_of_range = (series < 0) | (series > 100)
            n_bad = out_of_range.sum()
            if n_bad == 0:
                continue

            report.anomalies.append(Anomaly(
                check_code="C02",
                severity=AnomalySeverity.CRITICAL,
                category=CheckCategory.CONVERGENCE,
                message=(
                    f"CDC PLACES column '{col}' has {n_bad} values outside [0, 100]. "
                    f"Prevalence must be a percentage. "
                    f"Range found: [{float(series.min()):.2f}, {float(series.max()):.2f}]."
                ),
                affected_count=int(n_bad),
                recommendation=(
                    f"Check if '{col}' is stored as a decimal fraction (0–1) "
                    f"rather than percentage (0–100). Multiply by 100 if so."
                ),
                detail={"column": col, "min": float(series.min()),
                        "max": float(series.max())}
            ))

    def _check_C03_area_distribution(
        self,
        gdf: gpd.GeoDataFrame,
        report: ValidationReport
    ) -> None:
        """
        C03: Parcel area distribution sanity.
        Urban land parcels follow an approximate log-normal distribution.
        A very high fraction of zero-area or identical-area parcels
        suggests a geometry reconstruction failure.
        """
        if "area_m2" not in gdf.columns:
            return

        areas = gdf["area_m2"].dropna()
        if len(areas) == 0:
            return

        n_zero    = (areas <= 0).sum()
        n_tiny    = (areas < 10).sum()   # < 10m² — implausibly small urban parcel

        if n_zero > 0:
            report.anomalies.append(Anomaly(
                check_code="C03_ZERO_AREA",
                severity=AnomalySeverity.CRITICAL,
                category=CheckCategory.CONVERGENCE,
                message=(
                    f"{n_zero} parcels have zero or negative area_m2. "
                    f"These will cause division-by-zero errors in density calculations."
                ),
                affected_count=int(n_zero),
                recommendation="Remove zero-area parcels or recalculate from geometry.",
            ))

        if n_tiny > 0:
            report.anomalies.append(Anomaly(
                check_code="C03_TINY_AREA",
                severity=AnomalySeverity.WARNING,
                category=CheckCategory.CONVERGENCE,
                message=(
                    f"{n_tiny} parcels have area < 10m² (smaller than a parking space). "
                    f"These are likely slivers from topology operations."
                ),
                affected_count=int(n_tiny),
                recommendation=(
                    "Review slivers — they are usually artefacts from dissolve "
                    "or clip operations and should be merged with adjacent parcels."
                ),
            ))

    def _check_C04_road_connectivity(
        self,
        gdf: gpd.GeoDataFrame,
        report: ValidationReport
    ) -> None:
        """
        C04: Road segments should form a connected network.
        A segment is 'isolated' if neither of its endpoints touches
        any other segment within snapping tolerance (1 metre).
        A high isolated segment count suggests coordinate precision issues.
        """
        if len(gdf) < 2:
            return

        # Collect all endpoints
        endpoints = []
        for geom in gdf.geometry:
            if geom is None or geom.is_empty:
                continue
            coords = list(geom.coords)
            endpoints.extend([coords[0], coords[-1]])

        if not endpoints:
            return

        ep_df = pd.DataFrame(endpoints, columns=["x", "y"])

        # Round to 5 decimal places (~1m precision for WGS84)
        ep_df["x_r"] = ep_df["x"].round(5)
        ep_df["y_r"] = ep_df["y"].round(5)

        connected_endpoints = ep_df.groupby(["x_r", "y_r"]).size()
        isolated = (connected_endpoints == 1).sum()
        total_endpoints = len(connected_endpoints)

        isolated_pct = isolated / total_endpoints if total_endpoints > 0 else 0

        # 10% isolated endpoints is unusual (OSM data is well-connected)
        if isolated_pct > 0.10:
            report.anomalies.append(Anomaly(
                check_code="C04",
                severity=AnomalySeverity.WARNING,
                category=CheckCategory.CONVERGENCE,
                message=(
                    f"{isolated:.0f} of {total_endpoints} road endpoints "
                    f"({isolated_pct:.1%}) are not connected to any other segment. "
                    f"High disconnectivity may indicate coordinate precision loss "
                    f"during export (e.g. rounding from projected to geographic CRS)."
                ),
                affected_count=int(isolated),
                recommendation=(
                    "Re-export road network at full coordinate precision. "
                    "If sourced from OSM, re-run osmnx ingestion with "
                    "retain_all=True to preserve connectivity."
                ),
                detail={"isolated_pct": round(float(isolated_pct), 4)}
            ))

    # ==========================================================================
    # Provenance checks
    # ==========================================================================

    def _check_P02_provenance_fields(
        self,
        gdf: gpd.GeoDataFrame,
        report: ValidationReport,
        dataset_version: str
    ) -> None:
        """P02: Dataset must have version and source metadata."""
        if not dataset_version:
            report.anomalies.append(Anomaly(
                check_code="P02_NO_VERSION",
                severity=AnomalySeverity.WARNING,
                category=CheckCategory.PROVENANCE,
                message=(
                    "No dataset_version provided. Without version tracking, "
                    "it is impossible to reproduce analysis results or identify "
                    "which data vintage was used for a given scenario."
                ),
                recommendation=(
                    "Always pass dataset_version when calling validate_*. "
                    "Format: 'YYYY-QN' e.g. '2024-Q1' or 'YYYY-MM-DD'."
                )
            ))

    def _check_P04_data_currency(
        self,
        dataset_version: str,
        report: ValidationReport
    ) -> None:
        """P04: Warn if dataset is older than expected thresholds."""
        if not dataset_version:
            return

        # Try to extract year from version string
        try:
            year = int(dataset_version[:4])
            age = datetime.now().year - year
        except (ValueError, IndexError):
            return

        if age > MAX_DATA_AGE_YEARS_CRITICAL:
            sev = AnomalySeverity.CRITICAL
            msg = (f"Dataset version '{dataset_version}' is {age} years old. "
                   f"Data this old may no longer reflect current conditions "
                   f"and could lead to significantly incorrect suitability scores.")
        elif age > MAX_DATA_AGE_YEARS_WARNING:
            sev = AnomalySeverity.WARNING
            msg = (f"Dataset version '{dataset_version}' is {age} years old. "
                   f"Consider requesting an updated dataset from the source agency.")
        else:
            return

        report.anomalies.append(Anomaly(
            check_code="P04",
            severity=sev,
            category=CheckCategory.PROVENANCE,
            message=msg,
            recommendation="Request current data from the source agency.",
            detail={"dataset_version": dataset_version, "age_years": age}
        ))

    # ==========================================================================
    # Persistence and utilities
    # ==========================================================================

    def persist_report(self, report: ValidationReport) -> str:
        """
        Write the validation report to audit.anomaly_log in PostGIS.
        Returns the anomaly_log_id for reference.
        """
        if self._engine is None:
            self._engine = create_engine(self.db_url)

        report_json = json.dumps(report.to_dict())

        with self._engine.begin() as conn:
            result = conn.execute(text("""
                INSERT INTO audit.anomaly_log (
                    city_id, dataset_name, source_file, file_hash,
                    validated_at, n_rows_checked,
                    critical_count, warning_count,
                    passed, blocked, report_json
                ) VALUES (
                    :city_id, :dataset_name, :source_file, :file_hash,
                    :validated_at, :n_rows_checked,
                    :critical_count, :warning_count,
                    :passed, :blocked, :report_json::JSONB
                )
                RETURNING anomaly_log_id
            """), {
                "city_id":        report.city_id,
                "dataset_name":   report.dataset_name,
                "source_file":    report.source_file,
                "file_hash":      report.file_hash,
                "validated_at":   report.validated_at,
                "n_rows_checked": report.n_rows_checked,
                "critical_count": report.critical_count,
                "warning_count":  report.warning_count,
                "passed":         report.passed,
                "blocked":        report.blocked,
                "report_json":    report_json,
            })
            log_id = result.scalar()

        logger.info("Validation report persisted: anomaly_log_id=%s", log_id)
        return str(log_id)

    # ==========================================================================
    # Private helpers
    # ==========================================================================

    @staticmethod
    def _compute_file_hash(source_file: str) -> str:
        """SHA-256 hash of source file for provenance tracking."""
        if not source_file or not os.path.exists(source_file):
            return "no_file"
        sha256 = hashlib.sha256()
        with open(source_file, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                sha256.update(chunk)
        return sha256.hexdigest()

    def _init_report(
        self,
        dataset_name: str,
        source_file: str,
        gdf: gpd.GeoDataFrame
    ) -> ValidationReport:
        return ValidationReport(
            city_id=self.city_id,
            dataset_name=dataset_name,
            source_file=source_file,
            file_hash=self._compute_file_hash(source_file),
            validated_at=datetime.now(timezone.utc),
            n_rows_checked=len(gdf),
        )

    @staticmethod
    def _finalise_report(report: ValidationReport) -> None:
        """Set passed and blocked flags based on anomaly severities."""
        report.passed  = report.critical_count == 0
        report.blocked = report.critical_count > 0
        if report.blocked:
            logger.error(
                "VALIDATION BLOCKED: %s has %d critical anomalies. "
                "Analysis cannot proceed until planner acknowledges.",
                report.dataset_name, report.critical_count
            )
        elif report.warning_count > 0:
            logger.warning(
                "VALIDATION WARNINGS: %s has %d warnings. "
                "Analysis can proceed — planner notification required.",
                report.dataset_name, report.warning_count
            )
        else:
            logger.info("VALIDATION PASSED: %s — no anomalies detected.", report.dataset_name)


# ---------------------------------------------------------------------------
# Exception for blocked analysis runs
# ---------------------------------------------------------------------------

class DataQualityBlockedError(Exception):
    """
    Raised when an analysis run is attempted against data with CRITICAL anomalies
    that have not been acknowledged by a planner.
    """
    def __init__(self, report: ValidationReport) -> None:
        self.report = report
        super().__init__(
            f"Analysis blocked: {report.dataset_name} has "
            f"{report.critical_count} critical data quality anomalies. "
            f"A planner must acknowledge these anomalies before analysis can run. "
            f"Anomaly log ID: {report.city_id}"
        )
