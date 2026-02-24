"""
EtherCAT Master — Web Server
==============================

Web interface for EtherCAT bus management:

- Adapter selection
- Bus discovery (scan slaves, view identities and PDO layouts)
- PDO configuration (select which PDOs to assign per slave)
- Go OP / Stop with real-time state feedback (IDLE → PRE-OP → OP)

Usage::

    python -m ethercat_master.webserver
    python -m ethercat_master.webserver --port 8080
    python -m ethercat_master.webserver --pdo-config ethercat_config.json

Then open http://localhost:8080 in your browser.
"""

import argparse
import json
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

try:
    from .bus import EtherCATBus
    from .pdo import load_pdo_config, get_slave_pdo
    from .network_test import NetworkLatencyTest
except ImportError:
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from ethercat_master.bus import EtherCATBus
    from ethercat_master.pdo import load_pdo_config, get_slave_pdo
    from ethercat_master.network_test import NetworkLatencyTest

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def _load_net_config(pdo_config_path: str | None) -> dict:
    defaults = {"adapter": None, "cycle_ms": 1.0}
    try:
        if pdo_config_path and Path(pdo_config_path).exists():
            raw = json.loads(Path(pdo_config_path).read_text(encoding="utf-8"))
            net = raw.get("network", {})
            if "adapter" in net:
                defaults["adapter"] = net["adapter"]
            if "cycle_ms" in net:
                defaults["cycle_ms"] = float(net["cycle_ms"])
    except Exception:
        pass
    return defaults


def _save_net_config(pdo_config_path: str | None, adapter: str | None, cycle_ms: float):
    if not pdo_config_path:
        return
    try:
        p = Path(pdo_config_path)
        existing = {}
        if p.exists():
            existing = json.loads(p.read_text(encoding="utf-8"))
        existing["network"] = {"adapter": adapter, "cycle_ms": cycle_ms}
        p.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass


class BusState:
    """Tracks bus connection state for the web server."""

    def __init__(self, pdo_config_path=None):
        self.pdo_config_path = pdo_config_path
        self.bus = None
        self.adapter_name = None
        self.cycle_time_ms = 1.0
        self.last_error = ""
        self._lock = threading.Lock()

    @property
    def state(self):
        """Return the current EtherCAT bus state as a string."""
        if not self.bus or not self.bus.master:
            return "IDLE"
        if self.bus.master.in_op:
            return "OP"
        return "PRE-OP"

    @property
    def slave_count(self):
        if self.bus and self.bus.master:
            try:
                return len(self.bus.master.slaves)
            except Exception:
                pass
        return 0

    def connect(self, adapter_name, cycle_time_ms):
        with self._lock:
            if self.bus and self.bus.master and self.bus.master.in_op:
                raise RuntimeError("Already in OP. Stop first.")
            self.last_error = ""
            self.adapter_name = adapter_name
            self.cycle_time_ms = cycle_time_ms
            self.bus = EtherCATBus(
                adapter=adapter_name,
                cycle_time_ms=cycle_time_ms,
                pdo_config_path=self.pdo_config_path,
            )
            self.bus.open()
            _save_net_config(self.pdo_config_path, adapter_name, cycle_time_ms)

    def disconnect(self):
        with self._lock:
            if self.bus:
                try:
                    self.bus.close()
                except Exception:
                    pass
                self.bus = None
            self.last_error = ""


_SKIP_ADAPTERS = (
    "wi-fi", "wifi", "bluetooth", "loopback",
    "wi-fi direct", "wan miniport", "hyper-v",
)

_SKIP_LINUX_IFACES = ("lo", "wlan", "wlp", "docker", "br-", "veth", "virbr")

bus_state: BusState = None  # set in main()

_WEBGUI_DIR = Path(__file__).parent / "webgui"


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self._send_file(_WEBGUI_DIR / "index.html", "text/html; charset=utf-8")
            return

        if path == "/style.css":
            self._send_file(_WEBGUI_DIR / "style.css", "text/css; charset=utf-8")
            return

        if path == "/api/adapters":
            try:
                adapters = EtherCATBus.list_adapters()
                result = []
                for a in adapters:
                    name = a.name.decode("utf-8", errors="replace") if isinstance(a.name, bytes) else str(a.name)
                    desc = a.desc.decode("utf-8", errors="replace") if isinstance(a.desc, bytes) else str(a.desc)
                    if any(s in desc.lower() for s in _SKIP_ADAPTERS):
                        continue
                    if any(name.startswith(p) for p in _SKIP_LINUX_IFACES):
                        continue
                    result.append({"name": name, "desc": desc})
                self._send_json({
                    "adapters": result,
                    "current": bus_state.adapter_name,
                    "state": bus_state.state,
                })
            except Exception as exc:
                self._send_json({"error": str(exc)})
            return

        if path == "/api/connect":
            if bus_state.state == "OP":
                self._send_json({"error": "Already in OP. Stop first."})
                return
            try:
                adapter = query.get("adapter", [bus_state.adapter_name or ""])[0]
                cycle = float(query.get("cycle", [str(bus_state.cycle_time_ms)])[0])
                bus_state.connect(adapter, cycle)
                self._send_json({
                    "ok": True,
                    "state": bus_state.state,
                    "slaves": bus_state.slave_count,
                })
            except Exception as exc:
                bus_state.last_error = str(exc)
                bus_state.disconnect()
                self._send_json({"error": str(exc)})
            return

        if path == "/api/disconnect":
            bus_state.disconnect()
            self._send_json({"ok": True, "state": "IDLE"})
            return

        if path == "/api/status":
            self._send_json({
                "state": bus_state.state,
                "adapter": bus_state.adapter_name,
                "cycle": bus_state.cycle_time_ms,
                "slaves": bus_state.slave_count,
                "error": bus_state.last_error,
            })
            return

        if path == "/api/connection":
            self._send_json({
                "state": bus_state.state,
                "adapter": bus_state.adapter_name,
                "cycle": bus_state.cycle_time_ms,
                "slaves": bus_state.slave_count,
                "error": bus_state.last_error,
            })
            return

        if path == "/api/discover":
            adapter = query.get("adapter", [bus_state.adapter_name or ""])[0]
            if bus_state.state == "OP":
                self._send_json({"error": "Stop first — discovery needs exclusive adapter access."})
                return
            try:
                slaves = EtherCATBus.discover(
                    adapter=adapter or None,
                    pdo_config_path=bus_state.pdo_config_path,
                )
                self._send_json({"slaves": slaves, "adapter": adapter})
            except Exception as exc:
                self._send_json({"error": str(exc)})
            return

        if path == "/api/run_network_test":
            if bus_state.state != "OP":
                self._send_json({"error": "Bus must be in OP to run the network test."})
                return
            try:
                slave_idx = int(query.get("slave", ["0"])[0])
                num_samples = int(query.get("samples", ["200"])[0])
                test = NetworkLatencyTest(bus_state.bus.master, slave_index=slave_idx)
                test.NUM_SAMPLES = num_samples
                test.run_measurement()
                metrics = test.analyze()
                if metrics is None:
                    self._send_json({"error": "Insufficient data captured"})
                else:
                    self._send_json(metrics)
            except Exception as exc:
                self._send_json({"error": str(exc)})
            return

        if path == "/api/pdo_config":
            cfg_path = bus_state.pdo_config_path
            if cfg_path and Path(cfg_path).exists():
                try:
                    raw = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
                    self._send_json(raw)
                except Exception as exc:
                    self._send_json({"error": str(exc)})
            else:
                self._send_json({"default": {}, "slaves": {}})
            return

        self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/pdo_config":
            cfg_path = bus_state.pdo_config_path
            if not cfg_path:
                self._send_json({"error": "No PDO config path configured"})
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")
                new_data = json.loads(body)

                existing = {}
                p = Path(cfg_path)
                if p.exists():
                    existing = json.loads(p.read_text(encoding="utf-8"))

                if "slaves" not in existing:
                    existing["slaves"] = {}
                for k, v in new_data.get("slaves", {}).items():
                    existing["slaves"][k] = v

                net = new_data.get("network")
                if net:
                    existing["network"] = net
                    if "adapter" in net:
                        bus_state.adapter_name = net["adapter"]
                    bus_state.cycle_time_ms = float(net.get("cycle_ms", bus_state.cycle_time_ms))

                p.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
                self._send_json({"ok": True})
            except Exception as exc:
                self._send_json({"error": str(exc)})
            return

        self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _send_json(self, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, filepath: Path, content_type: str):
        try:
            data = filepath.read_bytes()
        except FileNotFoundError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        first = str(args[0]) if args else ""
        if "/api/" not in first:
            super().log_message(fmt, *args)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global bus_state

    parser = argparse.ArgumentParser(description="EtherCAT Master Web Server")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--adapter", type=str, default=None,
                        help="Adapter name/UID (e.g. \\Device\\NPF_{...})")
    parser.add_argument("--pdo-config", type=str, default=None,
                        help="Path to ethercat_config.json")
    args = parser.parse_args()

    import os
    if os.name != "nt" and os.geteuid() != 0:
        print("\n  Error: EtherCAT requires raw socket access.")
        print("  Please run with sudo:\n")
        print("    sudo ecmaster-web\n")
        raise SystemExit(1)

    pdo_path = args.pdo_config
    if not pdo_path:
        candidate = Path(__file__).parent / "ethercat_config.json"
        if not candidate.exists():
            candidate.write_text('{"network": {}, "default": {}, "slaves": {}}\n', encoding="utf-8")
        pdo_path = str(candidate)

    net_cfg = _load_net_config(pdo_path)
    adapter = args.adapter if args.adapter is not None else net_cfg["adapter"]

    bus_state = BusState(pdo_config_path=pdo_path)
    bus_state.adapter_name = adapter
    bus_state.cycle_time_ms = float(net_cfg["cycle_ms"])

    server = ThreadingHTTPServer(("0.0.0.0", args.port), _Handler)
    print(f"EtherCAT Master Web Server running on http://localhost:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        bus_state.disconnect()
        server.shutdown()


if __name__ == "__main__":
    main()
