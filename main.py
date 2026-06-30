from ast_parser import ast_parser, build_call_graph

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

import git
from git import Repo

from dotenv import load_dotenv
load_dotenv()

GROQ_API_KEY = os.getenv("API_KEY")

class ReliabilityState(TypedDict):

    repo_path: str
    call_graph: dict 
    risk_factor: dict
    architecture: Dict
    deployment_inputs: Dict
    scenarios: List
    simulation_results: Dict
    risk_score: float
    bottlenecks: List[str]
    # detected_stack:Dict
    rollback_needed: bool
    report: str

    # this is basic boilerplate copde
def print_call_tree(call_graph: dict):
    nodes_by_file = {}
    for node in call_graph["nodes"]:
        nodes_by_file.setdefault(node["file"], []).append(node)

    edges_by_caller = {}
    for edge in call_graph["edges"]:
        edges_by_caller.setdefault(edge["caller"], []).append(edge)

    unresolved_by_caller = {}
    for call in call_graph["unresolved_calls"]:
        unresolved_by_caller.setdefault(call["caller"], []).append(call)

    for file, funcs in nodes_by_file.items():
        print(f"📄 {file}")
        for func in funcs:
            risk = f" [{', '.join(func['risk_tags'])}]" if func["risk_tags"] else ""
            print(f"  └── {func['name']}({', '.join(func['args'])}){risk}  (L{func['line_start']}-{func['line_end']})")

            func_id = func["id"]
            resolved = edges_by_caller.get(func_id, [])
            unresolved = unresolved_by_caller.get(func_id, [])

            for edge in resolved:
                callee_name = edge["callee"].split("::")[-1]
                risk = f" [{', '.join(edge['risk_tags'])}]" if edge["risk_tags"] else ""
                print(f"        ├─→ {callee_name}() (internal){risk}  L{edge['line']}")

            for call in unresolved:
                risk = f" [{', '.join(call['risk_tags'])}]" if call["risk_tags"] else ""
                print(f"        ├─→ {call['call_name']}() (external){risk}  L{call['line']}")
        print()

    if call_graph["errors"]:
        print("⚠️  Errors:")
        for err in call_graph["errors"]:
            print(f"  - {err}")

def generate_call_graph(state: ReliabilityState)->ReliabilityState:
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

        except SyntaxError as e:
            print(f"Skipping {filename} due to syntax error: {e}")
            continue
    
    build_call_graph("build")

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

def format_for_llm(call_graph: dict, risk_factors: list) -> str:
    """
    Condense the artifacts into a prompt-friendly string.
    You do NOT dump the entire call graph raw - that would flood the context.
    Instead, summarise the structure and let risk_factors carry the detail.
    """
    node_summary = [
        f"- {n['id']} (fan_in={n['fan_in']}, fan_out={n['fan_out']}, risk_tags={n['risk_tags']})"
        for n in call_graph["nodes"]
    ]

    risk_summary = [
        f"- [{r['sre_category'].upper()}] {r['function']} in {r['file']}: "
        f"{r['rule_triggered']} | {r['evidence']} | fan_in={r['fan_in']}"
        for r in risk_factors
    ]

    return f"""
        ## Codebase Structure (Call Graph Summary)
        Total functions: {len(call_graph['nodes'])}
        Total internal edges: {len(call_graph['edges'])}
        Total unresolved external calls: {len(call_graph['unresolved_calls'])}

        Functions:
        {chr(10).join(node_summary)}

        ## Pre-Detected Risk Flags (from static rules engine)
        {chr(10).join(risk_summary) if risk_summary else "No rule-based risk flags detected."}
        """

def architecture_extractor(state: ReliabilityState)->ReliabilityState:

    # stack = state["detected_stack"]

    llm = ChatGroq(
        api_key=GROQ_API_KEY,
        model="llama-3.3-70b-versatile",
        temperature=0,
    )
    
    with open("questions_prompt.txt", "r", encoding="utf-8") as f:
        questions_prompt = f.read()

    with open("assessment_prompt.txt", "r", encoding="utf-8") as f:
        assessment_prompt = f.read()

    with open("call_graph.json", "r") as f:
        state["call_graph"] = f.read()
    
    with open("risk_factors.json", "r") as f:
        state["risk_factor"] = f.read()


    question_prompt = f"""
        {questions_prompt}
        Call Graph
        {state["call_graph"]}
        Risk Factors
        {state["risk_factor"]}

    """
    question_response = llm.invoke([HumanMessage(content=question_prompt)])
    questions_json = json.loads(question_response.content)
    questions = questions_json["questions"]

    print("\nQuestions:\n")

    for i, q in enumerate(questions, start=1):
        print(f"{i}. {q}")

    print("\nProvide answers in the same order.")
    print("Write answers on new line.\n")

    raw_answers = input("> ")

    answers = [a.strip() for a in raw_answers.split("\n")]

    deployment_context = []

    for q, a in zip(questions, answers):

        deployment_context.append({

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
        {json.dumps(deployment_context, indent=2)}

    """


    # context = format_for_llm(state["call_graph"], state["risk_factor"])
    
    # prompt = f"""
    #     You are a senior SRE reviewing a Python codebase for operational reliability risks before deployment.

    #     Below is a structural analysis of the codebase and pre-detected risk flags from a static rules engine.
    #     Your job is to:
    #     1. Review the flagged risks and reason about their likely impact under production load.
    #     2. Identify any additional reliability concerns not caught by the rules.
    #     3. Ask the user 3-5 targeted deployment-specific questions to fill gaps you cannot infer from code alone.
    #     (e.g. expected traffic volume, DB connection pool size, whether retries are configured upstream)

    #     {context}

    #     Now list your questions for the user.
    # """ 

    response = llm.invoke([HumanMessage(content=assessment_prompt)])
    print(response.content)
    
    return state

def deployment_questions(state: ReliabilityState) -> ReliabilityState:
    deployment = {

        "peak_rps":5000,

        "replicas":2,

        "autoscaling":False,

        "cache":False,

        "db_connections":100

    }

    return {

        "deployment_inputs":deployment

    }

def simulation_engine(state):

    deployment = state["deployment_inputs"]

    peak = deployment["peak_rps"]

    replicas = deployment["replicas"]

    db_limit = deployment["db_connections"]

    capacity = replicas*1000

    utilization = peak/capacity

    db_usage = peak*0.05

    latency = 200*(utilization)

    results = {

        "capacity":capacity,

        "utilization":utilization,

        "db_usage":db_usage,

        "latency":latency

    }

    return {

        "simulation_results":results

    }

def risk_assessor(state):

    sim = state["simulation_results"]

    deploy = state["deployment_inputs"]

    risk = 0

    bottlenecks = []

    if sim["utilization"] > 0.8:

        risk += 30

        bottlenecks.append(

            "Application overload"

        )

    if sim["db_usage"] > deploy["db_connections"]:

        risk += 30

        bottlenecks.append(

            "Database saturation"

        )

    if deploy["autoscaling"] == False:

        risk += 15

        bottlenecks.append(

            "No autoscaling"

        )

    if deploy["cache"] == False:

        risk += 10

        bottlenecks.append(

            "Missing cache"

        )

    rollback = risk > 60

    return {

        "risk_score":risk,

        "bottlenecks":bottlenecks,

        "rollback_needed":rollback

    }

def mitigation_agent(state):
    llm = Groq(
    model="llama-3.3-70b-versatile",
    temperature=0
    )

    prompt = f"""

    Risk 
    Score

    {state['risk_score']}

    Bottlenecks

    {state['bottlenecks']}

    Suggest 
    mitigation 
    strategies

    """

    response = llm.invoke(prompt)

    return {

        "recommendations":[

            response.content

        ]

    } #oyy sunn....dont put too much code rn...will be hard to debug later....once we finish one section, lets go to next., ahh see all this is just for us to get an idea ki har node mai kya ho raha
      # we are gonna change it but abhi ke liye i just put it .t.hi..alrs  is mostly harcoded stuff....alrr but when u are building graph in the end, justs do start,end and repo analyzer....dont add other nodes to graph yet...alr
def rollback_advisor(state):

    if state["rollback_needed"]:

        advice = """

        Deployment not recommended.

        Rollback suggested.

        Stable version should be retained.

        """

    else:

        advice = """

        Deployment safe.

        Rollback unnecessary.

        """

    return {

        "rollback_message":advice

    }

def report_generator(state):
    llm = Groq(
    model="llama-3.3-70b-versatile",
    temperature=0
    )

    prompt = f"""
    Architecture
    {state['architecture']}
    Simulation
    {state['simulation_results']}
    Risk
    {state['risk_score']}
    Recommendations
    {state['recommendations']}
    Generate reliability report.
    """

    response = llm.invoke(prompt)

    return {

        "report":response.content

    }

builder = StateGraph(ReliabilityState)

builder.add_node("generate_call_graph", generate_call_graph)

builder.add_node("architecture_extractor",
                 architecture_extractor)

builder.add_node("deployment_questions",
                 deployment_questions)

builder.add_node("simulation_engine",
                 simulation_engine)

builder.add_node("risk_assessor",
                 risk_assessor)

builder.add_node("mitigation_agent",
                 mitigation_agent)

builder.add_node("rollback_advisor",
                 rollback_advisor)

builder.add_node("report_generator",
                 report_generator)

builder.add_edge(START, "generate_call_graph")

builder.add_edge(
    "generate_call_graph",
    "architecture_extractor"
)

# builder.add_edge(
#     "architecture_extractor",
#     "deployment_questions"
# )

# builder.add_edge(
#     "deployment_questions",
#     "simulation_engine"
# )

# builder.add_edge(
#     "simulation_engine",
#     "risk_assessor"
# )

# builder.add_edge(
#     "risk_assessor",
#     "mitigation_agent"
# )

# builder.add_edge(
#     "mitigation_agent",
#     "rollback_advisor"
# )

# builder.add_edge(
#     "rollback_advisor",
#     "report_generator"
# )

builder.add_edge(
    "architecture_extractor",
    END
)

graph = builder.compile()


png_data = graph.get_graph().draw_mermaid_png()

with open("workflow.png", "wb") as f:
    f.write(png_data)

print("Workflow saved as workflow.png")



graph.invoke({"repo_path": "https://github.com/PranavKuppa/Email_Sender.git"})

    


