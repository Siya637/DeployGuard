from ast_parser import ast_parser, build_call_graph

from typing import TypedDict,Annoted,List,Union,Dict
from langgraph.graph import StateGraph,START,END
from langchain_core.messages import BaseMessage,HumanMessage,AIMessage,SystemMessage
from langgraph.graph.message import add_messages
from groq import Groq

import os
import ast
import json
import yaml

import git
from git import Repo

os.environ["GROQ_API_KEY"] = "your_api_key_heregsk_1eWUzFrNyrw6jvSRw7aPWGdyb3FYV48vYcQJkQXmoK2JAbBhaYBM"

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

def repo_analyzer(state: ReliabilityState):
    repo = state["repo_path"]

    stack = {}

    files = os.listdir(repo)

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

        "detected_stack":stack
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

def simulation_engine(state: ReliabilityState) -> ReliabilityState:

def risk_assessor(state: ReliabilityState) -> ReliabilityState:

def mitigation_agent(state: ReliabilityState) -> ReliabilityState:

def rollback_advisor(state: ReliabilityState) -> ReliabilityState:

def report_generator(state: ReliabilityState) -> ReliabilityState:

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

builder.add_edge(
    "repo_analyzer",
    "architecture_extractor"
)

builder.add_edge(
    "architecture_extractor",
    "deployment_questions"
)

builder.add_edge(
    "deployment_questions",
    "simulation_engine"
)

builder.add_edge(
    "simulation_engine",
    "risk_assessor"
)

builder.add_edge(
    "risk_assessor",
    "mitigation_agent"
)

builder.add_edge(
    "mitigation_agent",
    "rollback_advisor"
)

builder.add_edge(
    "rollback_advisor",
    "report_generator"
)

builder.add_edge(
    "report_generator",
    END
)

graph = builder.compile()

    
    
    