"""
engine/projection/usage_example.py

Demonstrates how ProjectionStrategy is used in practice.
This is not production code — it's a reference for the team.
"""

from shapely.geometry import box
from engine.projection.crs_strategy import ProjectionStrategy, OperationType

# -----------------------------------------------------------------------
# Example 1: New York City
# -----------------------------------------------------------------------
nyc_boundary = box(-74.26, 40.49, -73.70, 40.92)   # rough NYC bounding box
nyc_strategy = ProjectionStrategy.from_geometry(nyc_boundary)
print(nyc_strategy.describe())
# → distance: UTM Zone 18N (EPSG:32618)
# → area:     Custom LAEA centred on (-73.98, 40.70)
# → angle:    Transverse Mercator centred on NYC

# -----------------------------------------------------------------------
# Example 2: Chicago
# -----------------------------------------------------------------------
chicago_boundary = box(-87.94, 41.64, -87.52, 42.02)
chicago_strategy = ProjectionStrategy.from_geometry(chicago_boundary)
# → distance: UTM Zone 16N (EPSG:32616)   ← automatically different from NYC
# → area:     Custom LAEA centred on Chicago centroid
# → angle:    Transverse Mercator centred on Chicago

# -----------------------------------------------------------------------
# Example 3: Sydney, Australia
# -----------------------------------------------------------------------
sydney_boundary = box(150.50, -34.17, 151.35, -33.42)
sydney_strategy = ProjectionStrategy.from_geometry(sydney_boundary)
# → distance: UTM Zone 56S (EPSG:32756)   ← southern hemisphere, correct zone
# → area:     Custom LAEA centred on Sydney centroid
# → angle:    Transverse Mercator centred on Sydney

# -----------------------------------------------------------------------
# Example 4: Large country-wide study area (India)
# -----------------------------------------------------------------------
india_boundary = box(68.0, 8.0, 97.5, 37.5)
india_strategy = ProjectionStrategy.from_geometry(india_boundary)
# → lon_span = 29.5° >> UTM_MAX_SPAN_DEGREES (6°)
# → distance: Azimuthal Equidistant centred on India centroid
#             (UTM would distort badly across 5 zone boundaries)
# → area:     Custom LAEA centred on India centroid

# -----------------------------------------------------------------------
# Example 5: Using the strategy in the suitability engine
# -----------------------------------------------------------------------
# from engine.suitability.base import SuitabilityConfig
# from engine.suitability.hospital import HospitalSuitabilityAnalyzer
#
# city_boundary = get_city_boundary_from_postgis(city_id)   # WGS84 geometry
# strategy = ProjectionStrategy.from_geometry(city_boundary)
#
# config = SuitabilityConfig(
#     db_url="postgresql://mep_user:secret@localhost:5432/mep_db",
#     projection=strategy,
#     scenario_id="chicago_north_expansion_2026"
# )
#
# result = HospitalSuitabilityAnalyzer(config).run()
#
# In hospital.py the layers are loaded like this:
#     # Distance op — buffers, proximity
#     self.layers["roads"] = self.load_geodataframe(roads_query, OperationType.DISTANCE)
#
#     # Area op — parcel area validation
#     self.layers["parcels"] = self.load_geodataframe(parcel_query, OperationType.AREA)
#
#     # Angle op — wind direction for waste plant placement
#     self.layers["wind"] = self.load_geodataframe(wind_query, OperationType.ANGLE)
