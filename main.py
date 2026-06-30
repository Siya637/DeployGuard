from ast_parser import ast_parser, build_call_graph

from typing import TypedDict,Annotated,List,Union,Dict
from langgraph.graph import StateGraph,START,END
from langchain_core.messages import BaseMessage,HumanMessage,AIMessage,SystemMessage
from langgraph.graph.message import add_messages
from groq import Groq
from IPython.display import display,Image

import os
import ast
import json
import yaml

import git
from git import Repo

class ReliabilityState(TypedDict):

    repo_path: str
    call_graph: str 
    architecture: Dict
    deployment_inputs: Dict
    scenarios: List
    simulation_results: Dict
    risk_score: float
    bottlenecks: List[str]
    detected_stack:Dict
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

def repo_analyzer(state: ReliabilityState):
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
    
    state["call_graph"] = build_call_graph("build")
    print_call_tree(state["call_graph"])

    stack = {}

    files = os.listdir(clone_path)

    if "requirements.txt" in files:
        stack["python"]=True

    if "Dockerfile" in files:
        stack["docker"]=True

    if "docker-compose.yml" in files:
        stack["compose"]=True

    if "package.json" in files:
        stack["node"]=True

    if "k8s" in files:
        stack["kubernetes"]=True

    return {
        "stack": stack
    }

def architecture_extractor(state):

    stack = state["detected_stack"]

    llm = Groq(
    model="llama-3.3-70b-versatile",
    temperature=0
    )
    prompt = f"""
        Detected technologies
        {stack}
        Infer architecture.
        Identify
        backend
        database
        cache
        message queue
        deployment
        """

    response = llm.invoke(prompt) 

    return {

        "architecture":response.content

    }

def deployment_questions(state: ReliabilityState) -> ReliabilityState:
    #will have to figure this out as iss mai we will have to call llm na
    #so hardcoding initially
    #u saw what i wrote above?? where?
    #ohh accha ok ha that u said is correct but let just write some basic cheeze then we will modify it later as per our req....haa but where exactly are you planning to call the ast_pwarser rn?
    #in the repo analyser
    # alrr....seee...use a for loop...for everyfile in repo, call ast_parser and apss file as parameter.(will give syntax)....in the end we will call build_graph to get call graph and pass it to llm. alr thik
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

    prompt = f"""

Risk Score

{state['risk_score']}

Bottlenecks

{state['bottlenecks']}

Suggest mitigation strategies.

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

builder.add_node("repo_analyzer", repo_analyzer)

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

builder.add_edge(START, "repo_analyzer")

# builder.add_edge(
#     "repo_analyzer",
#     "architecture_extractor"
# )

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
    "repo_analyzer",
    END
)

graph = builder.compile()


png_data = graph.get_graph().draw_mermaid_png()

with open("workflow.png", "wb") as f:
    f.write(png_data)

print("Workflow saved as workflow.png")



graph.invoke({"repo_path": "https://github.com/PranavKuppa/Email_Sender.git"})

    
    