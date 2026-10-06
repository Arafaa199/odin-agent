"""Cartographer Alerts — formats drift reports and sends via Odin comms.

Follows the watchdog pattern: init_transports(), then send/send_all.
"""

import logging
from datetime import datetime

log = logging.getLogger("cartographer.alerts")


def format_drift_alert(report: dict) -> str:
    lines = [f"*Cartographer Drift* — {datetime.now().strftime('%Y-%m-%d %H:%M')}"]
    counts = report.get("counts", {})

    if report.get("newly_unhealthy"):
        lines.append("")
        lines.append(f"🔴 *Newly Unhealthy* ({counts['newly_unhealthy']}):")
        for svc in report["newly_unhealthy"]:
            port = f" :{svc['port']}" if svc.get("port") else ""
            lines.append(f"  `{svc['host']}` {svc['name']}{port} — state: {svc['state']}")

    if report.get("disappeared"):
        lines.append("")
        lines.append(f"⚠️ *Disappeared* ({counts['disappeared']}):")
        for svc in report["disappeared"]:
            lines.append(f"  `{svc['host']}` {svc['name']} — was {svc.get('state', '?')}")

    if report.get("new_undocumented"):
        lines.append("")
        lines.append(f"📋 *New Undocumented* ({counts['new_undocumented']}):")
        for svc in report["new_undocumented"]:
            port = f" :{svc['port']}" if svc.get("port") else ""
            proc = f" [{svc['process']}]" if svc.get("process") else ""
            lines.append(f"  `{svc['host']}` {svc['type']}:{svc['name']}{port}{proc}")
        lines.append("  _Add to known\\_services.yaml to suppress_")

    if report.get("recovered"):
        lines.append("")
        lines.append(f"✅ *Recovered* ({counts['recovered']}):")
        for svc in report["recovered"]:
            lines.append(f"  `{svc['host']}` {svc['name']}")

    if report.get("new_services") and not report.get("is_first_run"):
        new_documented = [
            s
            for s in report["new_services"]
            if s["documented"] and s not in report.get("new_undocumented", [])
        ]
        if new_documented:
            lines.append("")
            lines.append(f"🆕 *New Services* ({len(new_documented)}):")
            for svc in new_documented:
                lines.append(f"  `{svc['host']}` {svc['name']}")

    total = sum(counts.values())
    lines.append(f"\n_{total} drift event(s) detected_")
    return "\n".join(lines)


def format_first_run_summary(registry: dict) -> str:
    summary = registry.get("summary", {})
    lines = [
        f"*Cartographer Baseline* — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "First scan complete. Baseline established.",
        f"  Total: {summary.get('total', 0)}",
        f"  Documented: {summary.get('documented', 0)}",
        f"  Undocumented: {summary.get('undocumented', 0)}",
        f"  Healthy: {summary.get('healthy', 0)}",
        f"  Unhealthy: {summary.get('unhealthy', 0)}",
    ]

    unhealthy = [s for s in registry.get("services", []) if s.get("healthy") is False]
    if unhealthy:
        lines.append("")
        lines.append(f"🔴 *Unhealthy at baseline* ({len(unhealthy)}):")
        for svc in unhealthy:
            lines.append(f"  `{svc['host']}` {svc['name']} — {svc['state']}")

    lines.append("\n_Future scans will report drift from this baseline_")
    return "\n".join(lines)


def send_drift_alert(report: dict, registry: dict, comms_module) -> bool:
    if report.get("is_first_run"):
        msg = format_first_run_summary(registry)
        log.info("First run — sending baseline summary")
        return comms_module.send(msg)

    from .drift import has_actionable_drift

    if not has_actionable_drift(report):
        log.info("No actionable drift — skipping alert")
        return False

    msg = format_drift_alert(report)
    log.info(msg)

    is_critical = (
        len(report.get("newly_unhealthy", [])) >= 3 or len(report.get("disappeared", [])) >= 2
    )

    if is_critical and comms_module.available():
        results = comms_module.send_all(msg)
        log.info("Critical drift fan-out: %s", results)
        return any(results.values())
    elif comms_module.send(msg):
        log.info("Drift alert sent")
        return True
    else:
        log.warning("All transports unavailable — alert logged only")
        return False
