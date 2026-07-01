from ast_parser import ast_parser, build_call_graph
from rules_engine import run_rules

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
    

    # this is basic boilerplate copde

def generate_call_graph(state: ReliabilityState)->ReliabilityState:
    state["raw_function_data"] = []
    repo = state["repo_path"]
    clone_path = os.path.expanduser("~/repo_clone")

    #clone repo
    if not os.path.exists(clone_path):
        git.Repo.clone_from(repo, clone_path)
    else:
        print("Repo already exists locally, skipping clone.")

    #storing only python files
    python_files = []
    for root, dirs, files in os.walk(clone_path):
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
            rel_filename = os.path.relpath(filename, start=clone_path)
            result = ast_parser(source_code, rel_filename)
            state["raw_function_data"].append(result)

        except SyntaxError as e:
            print(f"Skipping {filename} due to syntax error: {e}")
            continue
   
    
    #build call graph.json & store into cal_graph state variable
    state["call_graph"] = build_call_graph("build")

    #build risk_factors.json & store into risk_factor state variable
    
    risk_flags = run_rules(state["call_graph"], state["raw_function_data"])
    os.makedirs("build", exist_ok=True)
    with open("build/risk_factors.json", "w") as f:
        json.dump(risk_flags, f, indent=2)
    state["risk_factor"] = risk_flags

    # stack = {}

    # files = os.listdir(clone_path)

    # if "requirements.txt" in files:
    #     stack["python"]=True

    # if "Dockerfile" in files:
    #     stack["docker"]=True

    # if "docker-compose.yml" in files:
    #     stack["compose"]=True

    # if "package.json" in files:
    #     stack["node"]=True

    # if "k8s" in files:
    #     stack["kubernetes"]=True

    # return {
    #     "stack": stack
    # }
    return state


def extract_json(text: str) -> dict:
    text = text.strip()
    if not text:
        raise ValueError("LLM returned empty content")

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object found in LLM output: {text[:200]!r}")

    raw = match.group(0)

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        try:
            return json.loads(repair_json(raw))
        except Exception as e:
            raise ValueError(
                f"Failed to parse or repair JSON from LLM output: {raw[:200]!r}"
            ) from e


def inter_llm_response(state: ReliabilityState)->ReliabilityState:

    llm = ChatGroq(
        api_key=GROQ_API_KEY,
        model="llama-3.3-70b-versatile",
        temperature=0,
    )
    
    with open("questions_prompt.txt", "r", encoding="utf-8") as f:
        questions_prompt = f.read()

    with open("assessment_prompt.txt", "r", encoding="utf-8") as f:
        assessment_prompt = f.read()


    question_prompt = f"""
        {questions_prompt}
        Call Graph
        {state["call_graph"]}
        Risk Factors
        {state["risk_factor"]}

    """
    question_response = llm.invoke([HumanMessage(content=question_prompt)])
    questions_json = extract_json(question_response.content)
    questions = questions_json["questions"]

    print("\nQuestions:\n")

    for i, q in enumerate(questions, start=1):
        print(f"{i}. {q}")

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

        Deployment Information
        {json.dumps(state["deployment_context"], indent=2)}

    """ 

    response = llm.invoke([HumanMessage(content=assessment_prompt)])
    assessment_json = extract_json(response.content)  # now with repair_json fallback

    if assessment_json is None:
        print("inter_llm_response: failed to parse LLM output")
        print(repr(response.content))
        assessment_json = {}

    state["failure_points"] = assessment_json.get("failure_points", [])   # fixed: underscore
    state["reliability_score"] = assessment_json.get("reliability_score", 0)
    state["risk_level"] = assessment_json.get("risk_level", "UNKNOWN")
    state["assessment"] = assessment_json

    out_path = os.path.join("build", "llm_response.json")
    with open(out_path, "w") as f:
        json.dump(assessment_json, f, indent=2)

    return state

def architecture_extractor(state: ReliabilityState) -> ReliabilityState:
    llm = ChatGroq(
        api_key=GROQ_API_KEY,
        model="llama-3.3-70b-versatile",
        temperature=0,
    )
    
    with open("architecture_prompt.txt", "r", encoding="utf-8") as f:
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
        model="llama-3.3-70b-versatile",
        temperature=0,
        max_retries=6
    )

    with open("closure_prompt.txt", "r", encoding="utf-8") as f:
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

    print(closure_response.content)

    return state

builder = StateGraph(ReliabilityState)

builder.add_node("generate_call_graph", generate_call_graph)

builder.add_node("inter_llm_response",
                 inter_llm_response)

builder.add_node("architecture_extractor",
                 architecture_extractor)

builder.add_node("simulation_engine",
                 simulation_engine)

builder.add_node("report_generator",
                 report_generator)

builder.add_edge(START, "generate_call_graph")

builder.add_edge(
    "generate_call_graph",
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



graph.invoke({"repo_path": "https://github.com/PranavKuppa/Email_Sender.git", "raw_function_data":[]})

    


