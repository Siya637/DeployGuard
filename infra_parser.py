# infra_parser.py

import yaml
import re
import json
import os


def parse_dockerfile(content: str, filename: str = "Dockerfile") -> dict:
    signals = {
        "filename": filename,
        "type": "dockerfile",
        "risks": [],
        "metadata": {}
    }

    lines = content.splitlines()

    # Check base image pinning
    for line in lines:
        stripped = line.strip()
        if stripped.upper().startswith("FROM"):
            image = stripped.split()[1] if len(stripped.split()) > 1 else ""
            signals["metadata"]["base_image"] = image
            if image.endswith(":latest") or (":" not in image):
                signals["risks"].append({
                    "type": "unpinned_base_image",
                    "severity": "HIGH",
                    "detail": f"Base image '{image}' is not pinned to a digest or specific version tag. Builds are non-deterministic.",
                })

    # Check for HEALTHCHECK
    has_healthcheck = any(l.strip().upper().startswith("HEALTHCHECK") for l in lines)
    signals["metadata"]["has_healthcheck"] = has_healthcheck
    if not has_healthcheck:
        signals["risks"].append({
            "type": "no_healthcheck",
            "severity": "MEDIUM",
            "detail": "No HEALTHCHECK defined. Container orchestrators cannot detect unhealthy state.",
        })

    # Check if running as root (no USER directive)
    has_user = any(l.strip().upper().startswith("USER") for l in lines)
    signals["metadata"]["has_user_directive"] = has_user
    if not has_user:
        signals["risks"].append({
            "type": "runs_as_root",
            "severity": "MEDIUM",
            "detail": "No USER directive found. Container runs as root by default.",
        })

    return signals


def parse_compose(content: str, filename: str = "docker-compose.yml") -> dict:
    signals = {
        "filename": filename,
        "type": "docker_compose",
        "risks": [],
        "services": {}
    }

    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as e:
        signals["risks"].append({"type": "parse_error", "severity": "LOW", "detail": str(e)})
        return signals

    services = data.get("services", {}) if data else {}

    for svc_name, svc_config in services.items():
        if not svc_config:
            continue

        svc_signals = []

        # Restart policy
        restart = svc_config.get("restart", None)
        if restart in (None, "no"):
            svc_signals.append({
                "type": "no_restart_policy",
                "severity": "HIGH",
                "detail": f"Service '{svc_name}' has no restart policy. A crash results in permanent downtime.",
            })

        # Resource limits
        deploy = svc_config.get("deploy", {}) or {}
        resources = deploy.get("resources", {}) or {}
        limits = resources.get("limits", None)
        mem_limit = svc_config.get("mem_limit", None)

        if not limits and not mem_limit:
            svc_signals.append({
                "type": "no_resource_limits",
                "severity": "HIGH",
                "detail": f"Service '{svc_name}' has no memory/CPU limits. A single runaway instance can starve the host.",
            })

        # Healthcheck
        if "healthcheck" not in svc_config:
            svc_signals.append({
                "type": "no_healthcheck",
                "severity": "MEDIUM",
                "detail": f"Service '{svc_name}' has no healthcheck. Compose will route traffic to unhealthy containers.",
            })

        # depends_on without service_healthy condition
        depends_on = svc_config.get("depends_on", {})
        if isinstance(depends_on, list):
            # shortform — no condition at all
            for dep in depends_on:
                svc_signals.append({
                    "type": "weak_dependency",
                    "severity": "MEDIUM",
                    "detail": f"Service '{svc_name}' depends on '{dep}' without condition: service_healthy. Start-order race condition possible.",
                })
        elif isinstance(depends_on, dict):
            for dep, dep_cfg in depends_on.items():
                if (dep_cfg or {}).get("condition") != "service_healthy":
                    svc_signals.append({
                        "type": "weak_dependency",
                        "severity": "MEDIUM",
                        "detail": f"Service '{svc_name}' depends on '{dep}' without service_healthy condition.",
                    })

        signals["services"][svc_name] = svc_signals
        signals["risks"].extend(svc_signals)

    return signals


def parse_k8s_manifest(content: str, filename: str) -> dict:
    signals = {
        "filename": filename,
        "type": "kubernetes",
        "kind": None,
        "name": None,
        "risks": [],
    }

    try:
        docs = list(yaml.safe_load_all(content))
    except yaml.YAMLError as e:
        signals["risks"].append({"type": "parse_error", "severity": "LOW", "detail": str(e)})
        return signals

    for doc in docs:
        if not doc:
            continue

        kind = doc.get("kind", "")
        name = (doc.get("metadata") or {}).get("name", "unknown")
        signals["kind"] = kind
        signals["name"] = name

        if kind == "Deployment":
            spec = doc.get("spec", {}) or {}

            # Single replica = SPOF
            replicas = spec.get("replicas", 1)
            if replicas < 2:
                signals["risks"].append({
                    "type": "single_replica",
                    "severity": "CRITICAL",
                    "detail": f"Deployment '{name}' runs {replicas} replica(s). Any crash or rolling update causes downtime.",
                })

            # Rolling update strategy
            strategy = spec.get("strategy", {}) or {}
            if strategy.get("type") != "RollingUpdate":
                signals["risks"].append({
                    "type": "no_rolling_update",
                    "severity": "HIGH",
                    "detail": f"Deployment '{name}' does not use RollingUpdate strategy. Deploys cause full downtime.",
                })

            # Check containers
            containers = (spec.get("template", {}) or {}).get("spec", {}).get("containers", []) or []
            for container in containers:
                cname = container.get("name", "unknown")

                # Resource limits
                resources = container.get("resources", {}) or {}
                if not resources.get("limits"):
                    signals["risks"].append({
                        "type": "no_resource_limits",
                        "severity": "HIGH",
                        "detail": f"Container '{cname}' in Deployment '{name}' has no resource limits defined.",
                    })

                # Liveness probe
                if not container.get("livenessProbe"):
                    signals["risks"].append({
                        "type": "no_liveness_probe",
                        "severity": "HIGH",
                        "detail": f"Container '{cname}' has no livenessProbe. Stuck processes won't be restarted.",
                    })

                # Readiness probe
                if not container.get("readinessProbe"):
                    signals["risks"].append({
                        "type": "no_readiness_probe",
                        "severity": "HIGH",
                        "detail": f"Container '{cname}' has no readinessProbe. Unready pods will receive traffic.",
                    })

                # Image pinning
                image = container.get("image", "")
                if image.endswith(":latest") or (":" not in image):
                    signals["risks"].append({
                        "type": "unpinned_image",
                        "severity": "HIGH",
                        "detail": f"Container '{cname}' uses unpinned image '{image}'. Deployments are non-deterministic.",
                    })

    return signals


def parse_requirements(content: str, filename: str = "requirements.txt") -> dict:
    signals = {
        "filename": filename,
        "type": "requirements",
        "risks": [],
        "unpinned": []
    }

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Pinned = has == (or ===). Ranges like >= are still loose.
        if "==" not in line:
            pkg = re.split(r"[><=!;@]", line)[0].strip()
            signals["unpinned"].append(pkg)
            signals["risks"].append({
                "type": "unpinned_dependency",
                "severity": "MEDIUM",
                "detail": f"Package '{pkg}' has no pinned version. Installs may differ between environments.",
            })

    return signals