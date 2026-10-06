"""Cartographer CLI — service auto-discovery scanner + drift detection.

Usage:
    python -m src.cartographer                    # Full scan + registry + drift
    python -m src.cartographer --scan-only        # Scan without saving or drift
    python -m src.cartographer --drift-only       # Drift check against last registry
    python -m src.cartographer --host worker      # Single host
    python -m src.cartographer --summary          # Print summary only
    python -m src.cartographer --alert            # Send drift alerts via Odin comms
    python -m src.cartographer --persist          # Save scan to Postgres
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

REGISTRY_PATH = Path.home() / ".odin" / ".cartographer" / "registry.json"


def main():
    parser = argparse.ArgumentParser(
        description="Cartographer — service auto-discovery + drift"
    )
    parser.add_argument(
        "--scan-only", action="store_true", help="Scan but don't save registry or check drift"
    )
    parser.add_argument(
        "--drift-only", action="store_true", help="Skip scan, check drift against saved registry"
    )
    parser.add_argument("--host", type=str, help="Scan single host")
    parser.add_argument("--summary", action="store_true", help="Print summary only")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    parser.add_argument("--alert", action="store_true", help="Send drift alerts via Odin comms")
    parser.add_argument("--no-drift", action="store_true", help="Skip drift detection")
    parser.add_argument(
        "--persist", action="store_true", help="Save scan to ops.cartographer_scans in Postgres"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    start = time.monotonic()

    if args.drift_only:
        registry = _load_saved_registry()
        if not registry:
            print("No saved registry found. Run a full scan first.", file=sys.stderr)
            sys.exit(1)
    else:
        registry = _run_scan(args)

    elapsed = time.monotonic() - start
    summary = registry["summary"]

    if args.json and args.drift_only:
        from .drift import detect_drift, load_drift_state

        prev_state = load_drift_state()
        drift_result = detect_drift(registry, prev_state)
        print(json.dumps(drift_result["report"], indent=2))
        return

    if args.json:
        print(json.dumps(registry, indent=2))
        return

    if args.summary:
        _print_summary(summary, elapsed)
        return

    _print_summary(summary, elapsed)
    _print_issues(registry)

    if not args.scan_only:
        from .registry import save_registry

        path = save_registry(registry)
        print(f"  Registry saved: {path}")

        drift_counts = None
        if not args.no_drift:
            drift_counts = _run_drift(registry, args)

        if args.persist:
            from .persist import save_to_db

            elapsed_ms = int(elapsed * 1000)
            if save_to_db(registry, drift_counts=drift_counts, duration_ms=elapsed_ms):
                print("  DB: Scan persisted to ops.cartographer_scans")
            else:
                print("  DB: Skipped (no ODIN_DB_PASSWORD or DB unreachable)")
    else:
        print("  (scan-only mode)")


def _run_scan(args) -> dict:
    from .scanner import scan_all, scan_device, load_devices
    from .registry import build_registry, load_known_services

    if args.host:
        devices = load_devices()
        device = next((d for d in devices if d.name == args.host), None)
        if not device:
            print(f"Unknown host: {args.host}", file=sys.stderr)
            print(f"Available: {', '.join(d.name for d in devices)}", file=sys.stderr)
            sys.exit(1)
        print(f"Scanning {device.name}...")
        scan_results = [scan_device(device)]
    else:
        print("Scanning all devices...")
        scan_results = scan_all()

    known = load_known_services()
    return build_registry(scan_results, known)


def _load_saved_registry() -> dict | None:
    if REGISTRY_PATH.exists():
        try:
            return json.loads(REGISTRY_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            return None
    return None


def _run_drift(registry: dict, args) -> dict | None:
    from .drift import detect_drift, load_drift_state, save_drift_state, has_actionable_drift

    prev_state = load_drift_state()
    drift_result = detect_drift(registry, prev_state)
    report = drift_result["report"]
    new_state = drift_result["new_state"]

    save_drift_state(new_state)

    if report.get("is_first_run"):
        print("\n  Drift: First run — baseline established")
        print(f"  Tracking {len(new_state['services'])} services for future drift detection")
    elif has_actionable_drift(report):
        counts = report["counts"]
        print("\n  Drift Detected:")
        if counts["new_undocumented"]:
            print(f"    New undocumented: {counts['new_undocumented']}")
            for svc in report["new_undocumented"]:
                port = f" :{svc['port']}" if svc.get("port") else ""
                print(f"      {svc['host']} {svc['name']}{port}")
        if counts["newly_unhealthy"]:
            print(f"    Newly unhealthy:  {counts['newly_unhealthy']}")
            for svc in report["newly_unhealthy"]:
                print(f"      {svc['host']} {svc['name']} — {svc['state']}")
        if counts["recovered"]:
            print(f"    Recovered:        {counts['recovered']}")
            for svc in report["recovered"]:
                print(f"      {svc['host']} {svc['name']}")
        if counts["disappeared"]:
            print(f"    Disappeared:      {counts['disappeared']}")
            for svc in report["disappeared"]:
                print(f"      {svc['host']} {svc['name']}")
    else:
        print("\n  Drift: No changes since last scan")

    if args.alert:
        _send_alerts(report, registry)

    return report.get("counts")


def _send_alerts(report: dict, registry: dict):
    from src import comms
    from src.config import load_settings
    from .alerts import send_drift_alert

    comms.init_transports(load_settings())

    if not comms.available():
        print("  Alert: No transports available")
        return

    sent = send_drift_alert(report, registry, comms)
    if sent:
        print("  Alert: Sent successfully")
    else:
        print("  Alert: Nothing to send (no actionable drift)")


def _print_issues(registry: dict):
    print()
    undocumented = [s for s in registry["services"] if not s["documented"]]
    if undocumented:
        print(f"  UNDOCUMENTED ({len(undocumented)}):")
        for svc in undocumented:
            extra = f" (port {svc['port']})" if svc.get("port") else ""
            proc = f" [{svc['process']}]" if svc.get("process") else ""
            print(f"    {svc['host']:8s} {svc['type']:8s} {svc['name']}{extra}{proc}")
        print()

    unhealthy = [s for s in registry["services"] if s.get("healthy") is False]
    if unhealthy:
        print(f"  UNHEALTHY ({len(unhealthy)}):")
        for svc in unhealthy:
            expected = svc.get("expected_state", "running")
            print(
                f"    {svc['host']:8s} {svc['name']:40s} state={svc['state']} expected={expected}"
            )
        print()

    not_found = [s for s in registry["services"] if s["state"] == "not_found"]
    if not_found:
        print(f"  NOT FOUND ({len(not_found)}):")
        for svc in not_found:
            print(f"    {svc['host']:8s} {svc['name']}")
        print()


def _print_summary(summary: dict, elapsed: float):
    print(f"\n  Cartographer Scan Complete ({elapsed:.1f}s)")
    print(f"  {'─' * 40}")
    print(
        f"  Total: {summary['total']}  |  Documented: {summary['documented']}  |  Undocumented: {summary['undocumented']}"
    )
    print(
        f"  Healthy: {summary['healthy']}  |  Unhealthy: {summary['unhealthy']}  |  Not found: {summary['not_found']}"
    )

    print("\n  By host:")
    for host, stats in sorted(summary["by_host"].items()):
        print(
            f"    {host:8s}  total={stats['total']:3d}  healthy={stats['healthy']:3d}  unhealthy={stats['unhealthy']:3d}  undoc={stats['undocumented']:3d}"
        )

    print("\n  By category:")
    for cat, count in sorted(summary["by_category"].items(), key=lambda x: -x[1]):
        print(f"    {cat:20s} {count:3d}")


if __name__ == "__main__":
    main()
