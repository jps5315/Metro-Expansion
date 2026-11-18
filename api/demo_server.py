"""Loopback-only HTTP server for the synthetic hospital suitability demo."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from api.demo_model import CRITERION_LABELS, CRITERIA, REFERENCE_WEIGHTS, HospitalDemoModel


ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = Path(__file__).resolve().parent / "static"
POLICY_FILE = ROOT / "data" / "processed" / "hospital_demo_policy.json"
MODEL = HospitalDemoModel()


def _normalized_weights(raw: Any) -> dict[str, float]:
    if not isinstance(raw, dict) or set(raw) != set(CRITERIA):
        raise ValueError("Provide one nonnegative weight for every listed criterion.")
    parsed: dict[str, float] = {}
    for criterion in CRITERIA:
        value = raw[criterion]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"Weight for {CRITERION_LABELS[criterion]} must be a number.")
        value = float(value)
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"Weight for {CRITERION_LABELS[criterion]} must be finite and nonnegative.")
        parsed[criterion] = value
    total = sum(parsed.values())
    if not math.isfinite(total) or total <= 0:
        raise ValueError("At least one criterion weight must be greater than zero.")
    return {criterion: value / total for criterion, value in parsed.items()}


def _load_profile() -> dict[str, Any]:
    if not POLICY_FILE.exists():
        return {"weights": dict(REFERENCE_WEIGHTS), "is_override": False, "revision": 0}
    try:
        data = json.loads(POLICY_FILE.read_text(encoding="utf-8"))
        weights = _normalized_weights(data.get("weights"))
        return {
            "weights": weights,
            "is_override": bool(data.get("is_override", True)),
            "revision": int(data.get("revision", 1)),
            "updated_at": data.get("updated_at"),
        }
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {"weights": dict(REFERENCE_WEIGHTS), "is_override": False, "revision": 0}


def _save_profile(weights: dict[str, float], *, override: bool) -> dict[str, Any]:
    current = _load_profile()
    profile = {
        "weights": _normalized_weights(weights),
        "is_override": override,
        "revision": current["revision"] + 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    POLICY_FILE.parent.mkdir(parents=True, exist_ok=True)
    POLICY_FILE.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    return profile


class DemoRequestHandler(BaseHTTPRequestHandler):
    server_version = "HospitalSuitabilityDemo/0.1"

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 64_000:
            raise ValueError("Request body is empty or too large.")
        payload = json.loads(self.rfile.read(length))
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object.")
        return payload

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            return self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        if path == "/api/demo":
            return self._send_json(200, MODEL.map_payload())
        if path == "/api/config":
            profile = _load_profile()
            return self._send_json(200, {
                "criteria": [
                    {"key": key, "label": CRITERION_LABELS[key]}
                    for key in CRITERIA
                ],
                "reference_weights": REFERENCE_WEIGHTS,
                "active_weights": profile["weights"],
                "is_override": profile["is_override"],
                "revision": profile["revision"],
                "updated_at": profile.get("updated_at"),
            })
        self.send_error(404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
            if path == "/api/config":
                if payload.get("use_defaults") is True:
                    profile = _save_profile(REFERENCE_WEIGHTS, override=False)
                else:
                    profile = _save_profile(payload.get("weights"), override=True)
                return self._send_json(200, {
                    "active_weights": profile["weights"],
                    "is_override": profile["is_override"],
                    "revision": profile["revision"],
                    "updated_at": profile["updated_at"],
                })
            if path == "/api/score":
                profile = _load_profile()
                result = MODEL.score(
                    float(payload.get("x")),
                    float(payload.get("y")),
                    profile["weights"],
                    policy_is_override=profile["is_override"],
                )
                return self._send_json(200, result)
            self.send_error(404)
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            self._send_json(400, {"error": str(error)})
        except OSError as error:
            self._send_json(500, {"error": f"Could not save the local demo policy: {error}"})

    def log_message(self, format: str, *args: object) -> None:
        print(f"[demo] {self.address_string()} - {format % args}")


def main() -> None:
    address = ("127.0.0.1", 8765)
    server = ThreadingHTTPServer(address, DemoRequestHandler)
    print("Hospital suitability illustrative demo")
    print(f"Open http://{address[0]}:{address[1]}/")
    print("Synthetic data only; server is bound to this computer.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping demo server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
