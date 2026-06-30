"""
rules_engine.py

Takes the merged call graph (output of build_call_graph) and runs a set
of independent SRE-based rules over it, producing a flat list of risk
flags. Each rule is a small, self-contained function so new rules can
be added without touching existing ones.
"""

import json

FAN_IN_THRESHOLD = 5  # tune this once you have a real repo to test against


def rule_unbounded_loop_with_risky_call(node, func_lookup):
    """Saturation/latency risk: a risk-tagged call (network/db/file) made
    inside a loop, with no sign of pagination/limiting at the AST level."""
    if not node.get("has_loop"):
        return None
    risky_calls_in_loop = [
        c for c in func_lookup.get(node["id"], {}).get("calls", [])
        if c["in_loop"] and c["risk_tags"]
    ]
    if not risky_calls_in_loop:
        return None
    return {
        "function": node["name"],
        "file": node["file"],
        "rule_triggered": "unbounded_loop_with_risky_call",
        "sre_category": "saturation",
        "evidence": f"{len(risky_calls_in_loop)} risky call(s) inside a loop: "
                    f"{[c['name'] for c in risky_calls_in_loop]}",
        "fan_in": node.get("fan_in", 0),
    }


def rule_unprotected_risky_call(node, func_lookup):
    """Error-propagation risk: a network/db/file call with no try/except
    around it. If it fails, the failure propagates uncaught."""
    unprotected = [
        c for c in func_lookup.get(node["id"], {}).get("calls", [])
        if c["risk_tags"] and not c["in_try_except"]
    ]
    if not unprotected:
        return None
    return {
        "function": node["name"],
        "file": node["file"],
        "rule_triggered": "unprotected_risky_call",
        "sre_category": "error_rate",
        "evidence": f"Unprotected risky call(s): {[c['name'] for c in unprotected]}",
        "fan_in": node.get("fan_in", 0),
    }


def rule_high_fan_in_risky_function(node, func_lookup):
    """Single-point-of-failure risk: many functions depend on this one,
    and it also touches a risky operation - a failure here has a wide
    blast radius."""
    if node.get("fan_in", 0) < FAN_IN_THRESHOLD:
        return None
    if not node.get("risk_tags"):
        return None
    return {
        "function": node["name"],
        "file": node["file"],
        "rule_triggered": "high_fan_in_risky_function",
        "sre_category": "single_point_of_failure",
        "evidence": f"fan_in={node['fan_in']}, risk_tags={node['risk_tags']}",
        "fan_in": node["fan_in"],
    }


def rule_unresolved_external_risky_call(unresolved_entry):
    """External/library calls (network/db/file) that aren't functions
    defined in the repo - these are exactly where real-world failures
    (timeouts, API errors, disk issues) tend to happen."""
    if not unresolved_entry["risk_tags"]:
        return None
    return {
        "function": unresolved_entry["caller"],
        "file": unresolved_entry["caller"].split("::")[0],
        "rule_triggered": "unresolved_external_risky_call",
        "sre_category": "latency_or_error_rate",
        "evidence": f"Calls external '{unresolved_entry['call_name']}' "
                    f"(tags: {unresolved_entry['risk_tags']}) at line {unresolved_entry['line']}",
        "fan_in": None,
    }


NODE_RULES = [
    rule_unbounded_loop_with_risky_call,
    rule_unprotected_risky_call,
    rule_high_fan_in_risky_function,
]


def run_rules(call_graph: dict, raw_function_data: list, build_dir: str = "build") -> list:
    """
    call_graph: output of build_call_graph()
    raw_function_data: list of per-file ast_parser() outputs
    """

    # build a lookup: node_id -> full function record
    func_lookup = {}
    for file_data in raw_function_data:
        for func in file_data.get("functions", []):
            func_id = f"{file_data['filename']}::{func['name']}"
            func_lookup[func_id] = func

    flags = []

    for node in call_graph["nodes"]:
        for rule in NODE_RULES:
            result = rule(node, func_lookup)
            if result:
                flags.append(result)

    for entry in call_graph.get("unresolved_calls", []):
        result = rule_unresolved_external_risky_call(entry)
        if result:
            flags.append(result)

    # Save the detected risk factors
    os.makedirs(build_dir, exist_ok=True)
    out_path = os.path.join(build_dir, "risk_factors.json")
    with open(out_path, "w") as f:
        json.dump(flags, f, indent=2)

    return flags


if __name__ == "__main__":
    # quick manual test - assumes build_call_graph() output and the
    # raw per-file parsed data are both available
    import os
    import glob

    build_dir = "build"
    raw_function_data = []
    for path in glob.glob(os.path.join(build_dir, "*.json")):
        with open(path) as f:
            raw_function_data.append(json.load(f))

    from ast_parser import build_call_graph
    call_graph = build_call_graph(build_dir=build_dir)

    flags = run_rules(call_graph, raw_function_data)

    out_path = os.path.join(build_dir, "risk_factors.json")
    with open(out_path, "w") as f:
        json.dump(flags, f, indent=2)

    print(f"Wrote {len(flags)} risk flags to {out_path}")