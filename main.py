from ast_parser import ast_parser, build_call_graph
from rules_engine import run_rules
from infra_parser import parse_dockerfile, parse_compose, parse_k8s_manifest, parse_requirements


from typing import TypedDict,Annotated,List,Union,Dict
from langgraph.graph import StateGraph,START,END
from langchain_core.messages import BaseMessage,HumanMessage,AIMessage,SystemMessage
from langgraph.graph.message import add_messages
from langchain_groq import ChatGroq
from langchain_core.messages import HumanMessage
from IPython.display import display,Image

import os
import ast
import json
import yaml
import glob
import re
from json_repair import repair_json

import git
from git import Repo

from dotenv import load_dotenv
load_dotenv()

GROQ_API_KEY = os.getenv("API_KEY")
GIT_URL = os.getenv("REP_URL")
_SKIP_DIRS  = {".git", "venv", "__pycache__", "node_modules"}
_BUILD_JSONS = {"call_graph.json", "risk_factors.json",
                "llm_response.json", "infra_signals.json"}

class ReliabilityState(TypedDict):

    repo_path: str
    call_graph: dict 
    raw_function_data: List[Dict]
    risk_factor: dict
    assessment: dict
    failure_points :dict
    reliability_score: int
    risk_level: str
    deployment_context: List
    architecture: Dict
    simulation_results: Dict
    rollback_recommended: bool
    risk_score: float
    report: str
    infra_signals:  List[Dict]
    clone_path: str
    repo_status: str
    
def get_repo(state: ReliabilityState) -> ReliabilityState:
    repo = state["repo_path"]
    repo_name = os.path.splitext(os.path.basename(repo))[0]
    build_dir = repo_name + "_clone"
    clone_path = os.path.expanduser(build_dir)
    state["clone_path"] = clone_path

    gitignore_path = ".gitignore"

    # Create .gitignore if it doesn't exist
    if not os.path.exists(gitignore_path):
        open(gitignore_path, "w").close()

    # Read existing entries
    with open(gitignore_path, "r") as f:
        entries = [line.strip() for line in f]

    # Add only if not already present
    if build_dir not in entries:
        with open(gitignore_path, "a") as f:
            f.write(f"\n{build_dir}/\n")

    # Clone the repository if it doesn't exist locally
    if not os.path.exists(clone_path):
        Repo.clone_from(repo, clone_path)
        state["repo_status"] = "CLONED"
    else:
        print("Repo already exists locally, skipping clone.")
        state["repo_status"] = "NOT CLONED"
    
    return state

def route_repo(state: ReliabilityState) -> str:
    return state["repo_status"]
 
def _load_existing_call_graph(build_dir: str = "build") -> dict:
    path = os.path.join(build_dir, "call_graph.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {"nodes": [], "edges": [], "unresolved_calls": [], "errors": []}

def _load_existing_infra_signals(build_dir: str = "build") -> list:
    path = os.path.join(build_dir, "infra_signals.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return []

def _rebuild_fan_metrics(graph: dict) -> None:
    """Recompute fan_in / fan_out in-place from current edges."""
    fan_in:  dict = {}
    fan_out: dict = {}
    for edge in graph["edges"]:
        fan_out[edge["caller"]] = fan_out.get(edge["caller"], 0) + 1
        fan_in[edge["callee"]]  = fan_in.get(edge["callee"],  0) + 1
    for node in graph["nodes"]:
        node["fan_in"]  = fan_in.get(node["id"],  0)
        node["fan_out"] = fan_out.get(node["id"], 0)
 
def _load_raw_function_data_from_build(build_dir: str = "build") -> List[Dict]:
    """Reconstruct raw_function_data by loading every per-file JSON
    that build_call_graph() wrote — needed as input to run_rules()."""
    result = []
    for path in glob.glob(os.path.join(build_dir, "*.json")):
        if os.path.basename(path) in _BUILD_JSONS:
            continue
        try:
            with open(path) as f:
                result.append(json.load(f))
        except (json.JSONDecodeError, OSError):
            pass
    return result       

def pull_new_files(state: ReliabilityState) -> ReliabilityState:
    """
    Incremental update path (repo was already cloned).
 
    Steps
    ─────
    1.  git pull; diff HEAD before/after to get changed file paths.
    2.  Re-parse changed .py files → overwrite their per-file build JSONs.
    3.  Re-parse changed infra files.
    4.  Splice new data into call_graph.json (remove stale, add fresh,
        rebuild fan metrics).  Save.
    5.  Splice new data into infra_signals.json.  Save.
    6.  Re-run run_rules() → new risk_factors.json.
    7.  Populate state so the pipeline can continue from inter_llm_response.
    """
    clone_path = state["clone_path"]
    build_dir  = "build"
    os.makedirs(build_dir, exist_ok=True)
 
    # ── Step 1: pull and collect changed paths ────────────────
    repo       = Repo(clone_path)
    old_commit = repo.head.commit
    repo.remotes.origin.pull()
    new_commit = repo.head.commit
 
    changed_paths: set[str] = set()
    if old_commit != new_commit:
        for item in old_commit.diff(new_commit):
            # a_path = before-pull name, b_path = after-pull name
            # normalise to forward-slash relative paths
            for p in (item.a_path, item.b_path):
                if p:
                    changed_paths.add(p.replace("\\", "/"))
        print(f"pull_new_files: {len(changed_paths)} changed path(s): {changed_paths}")
    else:
        print("pull_new_files: already up-to-date, no changes detected.")
        # Still wire up state from disk so the rest of the pipeline runs.
        state["call_graph"]        = _load_existing_call_graph(build_dir)
        state["infra_signals"]     = _load_existing_infra_signals(build_dir)
        state["raw_function_data"] = _load_raw_function_data_from_build(build_dir)
        rf_path = os.path.join(build_dir, "risk_factors.json")
        with open(rf_path) as f:
            state["risk_factor"] = json.load(f)
        return state
 
    changed_py    = {p for p in changed_paths if p.endswith(".py")}
    changed_infra = {p for p in changed_paths if not p.endswith(".py")}
 
    # ── Step 2: re-parse changed Python files ─────────────────
    new_file_data: List[Dict] = []
    deleted_py:    set[str]   = set()
 
    for rel_path in changed_py:
        abs_path = os.path.join(clone_path, rel_path)
        if not os.path.exists(abs_path):
            # file was deleted — mark for removal; clean up build JSON
            deleted_py.add(rel_path)
            safe_name = rel_path.replace("/", "__").replace("\\", "__")
            stale_json = os.path.join(build_dir, f"{safe_name}.json")
            if os.path.exists(stale_json):
                os.remove(stale_json)
                print(f"  removed stale build JSON: {stale_json}")
            continue
 
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                source = f.read()
            # ast_parser also overwrites the per-file JSON in build/
            parsed = ast_parser(source, rel_path, build_dir=build_dir)
            new_file_data.append(parsed)
            print(f"  re-parsed: {rel_path}")
        except SyntaxError as e:
            print(f"  skipping {rel_path} (syntax error): {e}")
 
    # ── Step 3: re-parse changed infra files ──────────────────
    new_infra_signals: List[Dict] = []
 
    for rel_path in changed_infra:
        abs_path = os.path.join(clone_path, rel_path)
        if not os.path.exists(abs_path):
            continue                    # deleted — will be pruned below
        base = os.path.basename(rel_path)
        try:
            with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except OSError:
            continue
 
        if base.startswith("Dockerfile"):
            new_infra_signals.append(parse_dockerfile(content, rel_path))
        elif base.startswith("docker-compose"):
            new_infra_signals.append(parse_compose(content, rel_path))
        elif rel_path.endswith((".yml", ".yaml")) and "kind:" in content:
            new_infra_signals.append(parse_k8s_manifest(content, rel_path))
        elif base == "requirements.txt":
            new_infra_signals.append(parse_requirements(content, rel_path))
 
    # ── Step 4: splice new Python data into call_graph ────────
    graph = _load_existing_call_graph(build_dir)
 
    # files whose entries need to be wiped (changed OR deleted)
    stale_files = (changed_py | deleted_py)
 
    graph["nodes"] = [
        n for n in graph["nodes"]
        if n["file"].replace("\\", "/") not in stale_files
    ]
    graph["edges"] = [
        e for e in graph["edges"]
        if not any([
            e["caller"].split("::")[0].replace("\\", "/") in stale_files,
            e["callee"].split("::")[0].replace("\\", "/") in stale_files,
        ])
    ]

    graph["unresolved_calls"] = [
        u for u in graph["unresolved_calls"]
        if u["caller"].split("::")[0].replace("\\", "/") not in stale_files
    ]
 
    # Build a name→[id] lookup from surviving nodes, then extend with new ones
    name_lookup: dict[str, list[str]] = {}
    for node in graph["nodes"]:
        name_lookup.setdefault(node["name"], []).append(node["id"])
 
    for file_data in new_file_data:
        for func in file_data.get("functions", []):
            func_id = f"{file_data['filename']}::{func['name']}"
            graph["nodes"].append({
                "id":         func_id,
                "file":       file_data["filename"],
                "name":       func["name"],
                "args":       func["args"],
                "line_start": func["line_start"],
                "line_end":   func["line_end"],
                "risk_tags":  func["risk_tags"],
                "fan_in":     0,
                "fan_out":    0,
            })
            name_lookup.setdefault(func["name"], []).append(func_id)
 
    # Resolve calls for the newly-parsed functions
    for file_data in new_file_data:
        for func in file_data.get("functions", []):
            caller_id = f"{file_data['filename']}::{func['name']}"
            for call in func.get("calls", []):
                call_name = call.get("name")
                if not call_name:
                    continue
                candidates = name_lookup.get(call_name)
                if not candidates:
                    candidates = name_lookup.get(call_name.split(".")[-1])
                if candidates:
                    for callee_id in candidates:
                        graph["edges"].append({
                            "caller":    caller_id,
                            "callee":    callee_id,
                            "line":      call["line"],
                            "risk_tags": call["risk_tags"],
                        })
                else:
                    graph["unresolved_calls"].append({
                        "caller":    caller_id,
                        "call_name": call_name,
                        "line":      call["line"],
                        "risk_tags": call["risk_tags"],
                    })
 
    _rebuild_fan_metrics(graph)
 
    with open(os.path.join(build_dir, "call_graph.json"), "w") as f:
        json.dump(graph, f, indent=4)
    print(f"  call_graph.json updated ({len(graph['nodes'])} nodes, {len(graph['edges'])} edges)")
 
    state["call_graph"] = graph
 
    # ── Step 5: splice new infra signals ──────────────────────
    existing_infra = _load_existing_infra_signals(build_dir)
 
    # prune stale entries (changed or deleted files)
    stale_rel = {p.replace("\\", "/") for p in changed_infra}
    existing_infra = [
        s for s in existing_infra
        if s.get("filename", "").replace("\\", "/") not in stale_rel
    ]
    existing_infra.extend(new_infra_signals)
 
    with open(os.path.join(build_dir, "infra_signals.json"), "w") as f:
        json.dump(existing_infra, f, indent=2)
 
    state["infra_signals"] = existing_infra
 
    # ── Step 6: re-run rules ──────────────────────────────────
    raw_function_data = _load_raw_function_data_from_build(build_dir)
    state["raw_function_data"] = raw_function_data
 
    risk_flags = run_rules(graph, raw_function_data, existing_infra, build_dir=build_dir)
    state["risk_factor"] = risk_flags
 
    return state

def generate_call_graph(state: ReliabilityState)->ReliabilityState:
    state["raw_function_data"] = []
    # repo = state["repo_path"]
    # clone_path = os.path.expanduser("~/repo_clone")

    # #clone repo
    # if not os.path.exists(clone_path):
    #     git.Repo.clone_from(repo, clone_path)
    # else:
    #     print("Repo already exists locally, skipping clone.")

    #storing only python files
    python_files = []
    for root, dirs, files in os.walk(state["clone_path"]):
        # skip common noise folders
        dirs[:] = [d for d in dirs if d not in (".git", "venv", "__pycache__", "node_modules")]
        for file in files:
            if file.endswith(".py"):
                python_files.append(os.path.join(root, file))

    #for every file, we extract the source code as a form of string and call ast_parser
    for filename in python_files:
        with open(filename, "r", encoding="utf-8") as f:
            source_code = f.read()

        try:
            rel_filename = os.path.relpath(filename, start=state["clone_path"])
            result = ast_parser(source_code, rel_filename)
            state["raw_function_data"].append(result)

        except SyntaxError as e:
            print(f"Skipping {filename} due to syntax error: {e}")
            continue
   
    
    #build call graph.json & store into cal_graph state variable
    state["call_graph"] = build_call_graph("build")
    
    #
    infra_signals = []

    for root, dirs, files in os.walk(state["clone_path"]):
        dirs[:] = [d for d in dirs if d not in (".git", "venv", "__pycache__", "node_modules")]

        for fname in files:
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, start=state["clone_path"])

            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
            except OSError:
                continue

            # Dockerfile (any variant: Dockerfile, Dockerfile.prod, etc.)
            if fname.startswith("Dockerfile"):
                infra_signals.append(parse_dockerfile(content, rel))

            # docker-compose
            elif fname in ("docker-compose.yml", "docker-compose.yaml") or fname.startswith("docker-compose"):
                infra_signals.append(parse_compose(content, rel))

            # Kubernetes — detect by presence of 'kind:' key
            elif fname.endswith((".yml", ".yaml")) and "kind:" in content:
                infra_signals.append(parse_k8s_manifest(content, rel))

            # requirements.txt
            elif fname == "requirements.txt":
                infra_signals.append(parse_requirements(content, rel))

    state["infra_signals"] = infra_signals

    # Save for inspection
    with open("build/infra_signals.json", "w") as f:
        json.dump(infra_signals, f, indent=2)

    #build risk_factors.json & store into risk_factor state variable
    
    risk_flags = run_rules(state["call_graph"], state["raw_function_data"], state["infra_signals"])
    os.makedirs("build", exist_ok=True)
    with open("build/risk_factors.json", "w") as f:
        json.dump(risk_flags, f, indent=2)
    state["risk_factor"] = risk_flags

    return state

def extract_json(text: str):
    text = text.strip()
    if not text:
        raise ValueError("LLM returned empty content")

    # Find ALL top-level {...} blocks (non-greedy won't work; use findall)
    matches = re.findall(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)* \}', text, re.DOTALL)
    # Simpler: split on ```json fences first
    fence_blocks = re.findall(r'```json\s*(.*?)```', text, re.DOTALL)
    
    if fence_blocks:
        results = []
        for block in fence_blocks:
            try:
                results.append(json.loads(block.strip()))
            except json.JSONDecodeError:
                results.append(json.loads(repair_json(block.strip())))
        return results if len(results) > 1 else results[0]

    # Fallback: grab first {...}
    match = re.search(r'\{.*?\}', text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON found in LLM output: {text[:200]!r}")
    raw = match.group(0)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return json.loads(repair_json(raw))


def pick_block(parsed, key: str):
    """From a parsed result (dict or list of dicts), return the one containing key."""
    if isinstance(parsed, dict):
        return parsed
    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, dict) and key in item:
                return item
    raise ValueError(f"No JSON block with key '{key}' found. Got: {parsed}")

def inter_llm_response(state: ReliabilityState)->ReliabilityState:

    llm = ChatGroq(
        api_key=GROQ_API_KEY,
        model="meta-llama/llama-4-scout-17b-16e-instruct",
        temperature=0,
        max_retries=6
    )
    
    with open("prompts/questions_prompt.txt", "r", encoding="utf-8") as f:
        questions_prompt = f.read()

    with open("prompts/assessment_prompt.txt", "r", encoding="utf-8") as f:
        assessment_prompt = f.read()


    question_prompt = f"""
        {questions_prompt}
        Call Graph
        {state["call_graph"]}
        Risk Factors
        {state["risk_factor"]}

    """
    # question_response = llm.invoke([HumanMessage(content=question_prompt)])
    # questions_json = extract_json(question_response.content)
    # questions = questions_json["questions"]

    question_response = llm.invoke([HumanMessage(content=question_prompt)])
    parsed = extract_json(question_response.content)
    questions_block = pick_block(parsed, "questions")
    questions = questions_block["questions"]

    print("\nQuestions:\n")
    for i, q in enumerate(questions, start=1):
        print(f"{i}. {q}")   # q is now a plain string, not a dict

    print("\nProvide answers in the same order.")
    print("Write answers by separating them with a comma.\n")

    raw_answers = input("> ")

    answers = [a.strip() for a in raw_answers.split(",")]

    state["deployment_context"] = []

    for q, a in zip(questions, answers):

        state["deployment_context"].append({

            "question": q,

            "answer": a

        })

    assessment_prompt = f"""
        {assessment_prompt}
        Call Graph
        {state["call_graph"]}
        Risk Factors
        {state["risk_factor"]}

        INFRA_SIGNALS: {json.dumps(state["infra_signals"], indent=2)}
        Deployment Information
        {json.dumps(state["deployment_context"], indent=2)}

    """ 

    response = llm.invoke([HumanMessage(content=assessment_prompt)])
    parsed = extract_json(response.content)
    assessment_json = pick_block(parsed, "reliability_score")  # grabs the right block

    state["failure_points"]     = assessment_json.get("failure_points", [])
    state["reliability_score"]  = assessment_json.get("reliability_score", 0)
    state["risk_level"]         = assessment_json.get("risk_level", "UNKNOWN")
    state["assessment"]         = assessment_json

    out_path = os.path.join("build", "llm_response.json")
    with open(out_path, "w") as f:
        json.dump(assessment_json, f, indent=2)

    return state

def architecture_extractor(state: ReliabilityState) -> ReliabilityState:
    llm = ChatGroq(
        api_key=GROQ_API_KEY,
        model="meta-llama/llama-4-scout-17b-16e-instruct",
        temperature=0,
        max_retries=6
    )
    
    with open("prompts/architecture_prompt.txt", "r", encoding="utf-8") as f:
        architecture_prompt = f.read()


    architect_prompt=f"""

        schema: {architecture_prompt}

        USER:
        CALL_GRAPH:
        {state["call_graph"]}

        RISK_FACTORS:
        {state["risk_factor"]}

        FAILURE_POINTS:
        {state["failure_points"]}

        INFRA_SIGNALS: {json.dumps(state["infra_signals"], indent=2)}
        
        DEPLOYMENT_CONTEXT (if collected):
        {state["deployment_context"]}
    """
    
    architecture_response = llm.invoke([HumanMessage(content=architect_prompt)])
    state["architecture"] = extract_json(architecture_response.content)
   
    return state

def simulation_engine(state: ReliabilityState) -> ReliabilityState:

    failure_points    = state["failure_points"]
    reliability_score = state["reliability_score"]

    # ------------------------------------------------------------------
    # How much reliability degrades when a failure point is active.
    # These are base impact points subtracted from reliability_score.
    # ------------------------------------------------------------------
    SEVERITY_IMPACT = {
        "LOW":      3,
        "MEDIUM":   8,
        "HIGH":     15,
        "CRITICAL": 25
    }

    # ------------------------------------------------------------------
    # Probability that a failure manifests once the component is
    # under sufficient load.
    # ------------------------------------------------------------------
    PROBABILITY_WEIGHT = {
        "LOW":    0.2,
        "MEDIUM": 0.5,
        "HIGH":   0.85
    }

    # ------------------------------------------------------------------
    # Minimum load multiplier (relative to baseline traffic) at which
    # each severity level activates.
    #
    # CRITICAL issues exist even at nominal load (1x).
    # LOW severity issues only surface under heavy saturation (8x).
    # ------------------------------------------------------------------
    ACTIVATION_LOAD = {
        "CRITICAL": 1.0,
        "HIGH":     1.5,
        "MEDIUM":   3.0,
        "LOW":      8.0
    }

    # ------------------------------------------------------------------
    # The three workload scenarios we simulate.
    # Each is defined by a load multiplier vs. baseline.
    # ------------------------------------------------------------------
    WORKLOAD_SCENARIOS = {
        "best_case":    1.0,   # nominal / quiet period traffic
        "average_case": 3.0,   # moderate load spike
        "worst_case":   8.0    # peak load / saturation event
    }

    # ------------------------------------------------------------------
    # Run the simulation for each scenario.
    # ------------------------------------------------------------------
    scenario_analysis = {}

    for scenario_name, load_multiplier in WORKLOAD_SCENARIOS.items():

        active_failures = []
        total_impact    = 0.0

        for fp in failure_points:

            severity    = fp.get("severity",    "LOW")
            probability = fp.get("probability", "LOW")
            component   = fp.get("component",   "unknown")
            reason      = fp.get("reason",      fp.get("issue", ""))

            # Skip if this load level doesn't yet trigger this failure point
            if load_multiplier < ACTIVATION_LOAD[severity]:
                continue

            # Load amplification: the further load exceeds the activation
            # threshold, the harder the failure hits — capped at 3x.
            load_amplification = min(
                load_multiplier / ACTIVATION_LOAD[severity],
                3.0
            )

            impact = (
                SEVERITY_IMPACT[severity]
                * PROBABILITY_WEIGHT[probability]
                * load_amplification
            )

            total_impact += impact

            active_failures.append({
                "component":                component,
                "reason":                   reason,
                "severity":                 severity,
                "probability":              probability,
                "impact_score":             round(impact, 2),
                "activates_at_load":        f"{ACTIVATION_LOAD[severity]}x"
            })

        # Sort by impact descending — highest risk components surface first
        active_failures.sort(key=lambda x: x["impact_score"], reverse=True)

        predicted_reliability = max(
            round(reliability_score - total_impact, 1),
            0
        )

        if predicted_reliability >= 85:
            expected_status = "Stable"
        elif predicted_reliability >= 70:
            expected_status = "Degraded Performance"
        elif predicted_reliability >= 50:
            expected_status = "Partial Outage Risk"
        else:
            expected_status = "High Outage Risk"

        scenario_analysis[scenario_name] = {
            "load_multiplier":       f"{load_multiplier}x baseline",
            "active_failure_count":  len(active_failures),
            "active_failures":       active_failures,
            "total_impact":          round(total_impact, 2),
            "predicted_reliability": predicted_reliability,
            "expected_status":       expected_status
        }

    # ------------------------------------------------------------------
    # Rollback is driven by worst-case predicted reliability,
    # defaulting to True (fail-safe) if the key is missing.
    # ------------------------------------------------------------------
    worst_reliability = scenario_analysis["worst_case"]["predicted_reliability"]
    rollback = worst_reliability < 60

    simulation_results = {
        "baseline_reliability": reliability_score,
        "scenario_analysis":    scenario_analysis
    }

    state["simulation_results"]    = simulation_results
    state["rollback_recommended"]  = rollback

    # print(json.dumps(simulation_results, indent=2))
    # print("Rollback recommended:", rollback)

    return state

def report_generator(state: ReliabilityState) -> ReliabilityState:
    llm = ChatGroq(
        api_key=GROQ_API_KEY,
        model="meta-llama/llama-4-scout-17b-16e-instruct",
        temperature=0,
        max_retries=6
    )

    with open("prompts/closure_prompt.txt", "r", encoding="utf-8") as f:
        closure_prompt = f.read()

    close_prompt = f"""
    
        schema: {closure_prompt}

        input:
        assessment:
        {state["assessment"]}

        RISK_FACTORS:
        {state["risk_factor"]}

        FAILURE_POINTS:
        {state["failure_points"]}

        ROLLBACK:
        {state["rollback_recommended"]}

        RELIABILITY SCORE:
        {state["reliability_score"]}

        RISK FACTORS:
        {state["risk_factor"]}

        Simulation Results:
        {state["simulation_results"]}


    """

    closure_response = llm.invoke([HumanMessage(content=close_prompt)])
    with open("reliability_report.md", "w", encoding="utf-8") as f:
        f.write(closure_response.content)

    print(closure_response.content)

    return state

builder = StateGraph(ReliabilityState)

builder.add_node("get_repo", 
                 get_repo)

builder.add_node("pull_new_files", pull_new_files)

builder.add_node("generate_call_graph", 
                 generate_call_graph)

builder.add_node("inter_llm_response",
                 inter_llm_response)

builder.add_node("architecture_extractor",
                 architecture_extractor)

builder.add_node("simulation_engine",
                 simulation_engine)

builder.add_node("report_generator",
                 report_generator)

builder.add_edge(START, "get_repo")

builder.add_conditional_edges(
    "get_repo",
    route_repo,
    {
        "CLONED": "generate_call_graph",
        "NOT CLONED": "pull_new_files",
    }
)

builder.add_edge(
    "generate_call_graph",
    "inter_llm_response"
)

builder.add_edge(
    "pull_new_files",      
    "inter_llm_response"
) 

builder.add_edge(
    "inter_llm_response",
    "architecture_extractor"
)

builder.add_edge(
    "architecture_extractor",
    "simulation_engine"
)

builder.add_edge(
    "simulation_engine",
    "report_generator"
)

builder.add_edge(
    "report_generator",
    END
)

graph = builder.compile()

png_data = graph.get_graph().draw_mermaid_png()

with open("workflow.png", "wb") as f:
    f.write(png_data)

print("Workflow saved as workflow.png")
graph.invoke({"repo_path": GIT_URL, "raw_function_data":[]})
