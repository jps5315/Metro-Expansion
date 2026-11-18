# Hospital suitability illustrative demo

This workspace now includes a local demonstration app for comparing
hypothetical hospital locations. It uses synthetic roads, demand points,
hospitals, and planning-layer scores. The existing analyzer's reference
weights drive the demo, and a city policy form can save a normalized override
that immediately affects planner scores.

## Run locally

Use the Python environment already configured for the analysis engine, with its
GeoPandas, Shapely, NumPy, pandas, SQLAlchemy, and projection dependencies
available. From the workspace root, run:

```powershell
python -m api.demo_server
```

Then open <http://127.0.0.1:8765/>. The server binds to loopback and is intended
for a local demo. Stop it with Ctrl+C.

## Demo behavior

- **Planner view:** click the synthetic map to place a hypothetical site. The
  result shows the overall weighted score, each criterion's score and weighted
  contribution, the current policy in plain language, and a sample network
  travel-time value.
- **City policy:** enter relative weights for all nine criteria and save. The
  server validates nonnegative values, normalizes them to a 100% total, and
  uses them in subsequent scores. Restore reference defaults at any time.
- **Reference defaults:** imported from
  `HospitalSuitabilityAnalyzer.DEFAULT_WEIGHTS`, avoiding a separate demo copy
  of the developer's starting weights.
- **Local persistence:** the active policy is stored in
  `data/processed/hospital_demo_policy.json`.

The app is a single prototype with separate planner and policy views, not two
duplicated applications. It has no user accounts or role-based authorization;
the local policy form is a workflow illustration, not a secure multi-user city
configuration system.

## Network gravity calculation

The analysis engine and demo share `NetworkTravelTimeIndex` and
`network_gravity_marginal_gain`. The index routes over OSM node topology,
respects one-way links, uses posted speeds when available and road-class
defaults otherwise, and connects point locations to the nearest network node.
The node connector assumes 10 km/h and rejects points more than 1,000 m from a
node. The gravity score estimates the marginal increase in need-weighted
adequate access after adding a reference 200-bed hospital, using a reference
adequacy level of 2 beds per 1,000 people. Those values are starting assumptions
to validate locally, not universal service standards.

The engine scores road access using the full vehicle road layer and emergency
access using major roads. It normalizes the gravity gain by total weighted
demand, so a site's score does not depend on which other candidates happen to
be included in the same run.

## Limits before operational use

The demo is not a live map or a production GIS deployment. Its synthetic
weather/access-resilience value is a neutral 0.60 placeholder. The road-time
estimate does not use live or historical traffic, recurring closures, weather,
EMS CAD/AVL records, road works, or an ambulance-specific routing profile. The
demo also does not run parcel hard-constraint screening. Connect validated city
datasets, local service thresholds, authentication, audit/version controls, and
operational review before treating outputs as a planning recommendation.
