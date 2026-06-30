"""
ast_extractor.py

Basic AST extraction for a single Python source file (given as a string).
Output: a JSON-serializable dict describing functions, calls, imports,
and risk-tagged operations (network/file/db) found in the source.

"""

import ast
import json
import os
import glob


# crude keyword lists to flag "risky" operations by matching against
# the called function/attribute name. Will get smarter later.
RISK_KEYWORDS = {
    "network": ["requests", "urlopen", "socket", "http", "get", "post", "put", "delete", "connect"],
    "file": ["open", "read", "write", "remove", "unlink", "rmdir", "mkdir"],
    "db": ["execute", "cursor", "commit", "session", "query", "insert", "select", "update_one", "find"],
}


def _classify_risk(call_name: str):
    """Given a dotted/short call name, return a list of risk tags it matches."""
    if not call_name:
        return []
    lowered = call_name.lower()
    tags = []
    for tag, keywords in RISK_KEYWORDS.items():
        if any(kw in lowered for kw in keywords):
            tags.append(tag)
    return tags


def _get_call_name(node: ast.Call):
    """Extract a readable name from a Call node, e.g. 'requests.get' or 'open'."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        parts = []
        cur = func
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
        return ".".join(reversed(parts))
    return None


class CodeVisitor(ast.NodeVisitor):
    def __init__(self):
        self.functions = []   # list of function records
        self.imports = []     # list of import records
        self._current_function = None  # stack-ish, basic version handles top-level funcs only

    def visit_Import(self, node):
        for alias in node.names:
            self.imports.append({
                "module": alias.name,
                "alias": alias.asname,
                "line": node.lineno,
            })
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        for alias in node.names:
            self.imports.append({
                "module": f"{node.module}.{alias.name}" if node.module else alias.name,
                "alias": alias.asname,
                "line": node.lineno,
            })
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node):
        self._visit_function(node)

    def _visit_function(self, node):
        func_record = {
            "name": node.name,
            "line_start": node.lineno,
            "line_end": getattr(node, "end_lineno", None),
            "args": [a.arg for a in node.args.args],
            "calls": [],
            "risk_tags": set(),
        }

        # walk only within this function's body for calls
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                call_name = _get_call_name(child)
                tags = _classify_risk(call_name)
                func_record["calls"].append({
                    "name": call_name,
                    "line": child.lineno,
                    "risk_tags": tags,
                })
                func_record["risk_tags"].update(tags)

        func_record["risk_tags"] = sorted(func_record["risk_tags"])
        self.functions.append(func_record)

        # don't generic_visit into nested functions twice -
        # ast.walk above already captured calls within this function,
        # so we skip descending further here.


def ast_parser(source_code: str, filename: str = "<string>") -> dict:
    """
    Parse a Python source string and return a JSON-serializable dict:
    {
        "filename": ...,
        "imports": [...],
        "functions": [...],
        "errors": [...]   # populated if source fails to parse
    }
    """
    result = {
        "filename": filename,
        "imports": [],
        "functions": [],
        "errors": [],
    }

    try:
        tree = ast.parse(source_code, filename=filename)
    except SyntaxError as e:
        result["errors"].append(f"SyntaxError: {e}")
        return result

    visitor = CodeVisitor()
    visitor.visit(tree)

    result["imports"] = visitor.imports
    result["functions"] = visitor.functions

    save_ast_json(result, build_dir=os.path.join(os.path.dirname(os.path.abspath(__file__)), "build"))

    return result


def save_ast_json(parsed: dict, build_dir: str = "build") -> str:
    """
    Save a single file's parsed AST dict (output of ast_parser) as a JSON
    file inside build_dir. One JSON file per source file.

    Returns the path the file was written to.
    """
    os.makedirs(build_dir, exist_ok=True)

    # turn the original filename into a safe, flat filename for the json,
    # e.g. "src/utils/io.py" -> "src__utils__io.py.json"
    safe_name = parsed["filename"].replace("/", "__").replace("\\", "__")
    out_path = os.path.join(build_dir, f"{safe_name}.json")

    with open(out_path, "w") as f:
        json.dump(parsed, f, indent=2)

    return out_path


def build_call_graph(build_dir: str = "build") -> dict:
    """
    Read every .json file in build_dir (each produced by save_ast_json),
    and merge them into a single repo-wide call graph.

    Nodes  = every function found, across all files, identified by
             "filename::function_name"
    Edges  = (caller_id -> callee_id) for calls that could be resolved
             to a known function. Calls that can't be matched to any
             known function (e.g. external library calls) are kept
             separately as "unresolved_calls" rather than dropped,
             since those are often exactly the risky ones (network/file/db).

    Resolution here is basic: it matches a call name against known
    function names by exact match or by the last dotted segment
    (e.g. "self.save" -> "save"). Proper cross-file resolution using
    imports is a later improvement.

    Returns a dict:
    {
        "nodes": [...],
        "edges": [...],
        "unresolved_calls": [...],
        "errors": [...]
    }
    """
    graph = {
        "nodes": [],
        "edges": [],
        "unresolved_calls": [],
        "errors": [],
    }

    json_paths = glob.glob(os.path.join(build_dir, "*.json"))

    # first pass: collect every function as a node, and build a lookup
    # from simple function name -> list of qualified ids (a name can
    # exist in more than one file)
    name_lookup = {}
    all_file_data = []

    for path in json_paths:
        try:
            with open(path, "r") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            graph["errors"].append(f"Failed to read {path}: {e}")
            continue

        all_file_data.append(data)

        if data.get("errors"):
            graph["errors"].extend(f"{data['filename']}: {err}" for err in data["errors"])

        for func in data.get("functions", []):
            func_id = f"{data['filename']}::{func['name']}"
            graph["nodes"].append({
                "id": func_id,
                "file": data["filename"],
                "name": func["name"],
                "args": func["args"],
                "line_start": func["line_start"],
                "line_end": func["line_end"],
                "risk_tags": func["risk_tags"],
            })
            name_lookup.setdefault(func["name"], []).append(func_id)

    # second pass: walk every function's calls and try to resolve them
    # against the name_lookup to build edges
    for data in all_file_data:
        for func in data.get("functions", []):
            caller_id = f"{data['filename']}::{func['name']}"

            for call in func.get("calls", []):
                call_name = call.get("name")
                if not call_name:
                    continue

                # try exact match first, then last dotted segment
                # (handles things like "self.save" -> "save")
                candidates = name_lookup.get(call_name)
                if not candidates:
                    short_name = call_name.split(".")[-1]
                    candidates = name_lookup.get(short_name)

                if candidates:
                    for callee_id in candidates:
                        graph["edges"].append({
                            "caller": caller_id,
                            "callee": callee_id,
                            "line": call["line"],
                            "risk_tags": call["risk_tags"],
                        })
                else:
                    # not a function we found in the repo - likely an
                    # external/library call (often where the risk is)
                    graph["unresolved_calls"].append({
                        "caller": caller_id,
                        "call_name": call_name,
                        "line": call["line"],
                        "risk_tags": call["risk_tags"],
                    })

    return graph


if __name__ == "__main__":
    # quick manual test across two "files" to exercise the multi-file flow
    file_a = """
import requests

def fetch_data(url):
    resp = requests.get(url)
    return resp.json()
"""

    file_b = """
from utils import fetch_data

def save_to_disk(data, path):
    with open(path, "w") as f:
        f.write(data)

def run_pipeline(url, path):
    data = fetch_data(url)
    save_to_disk(data, path)
"""

    ast_parser(file_a, filename="utils.py")
    ast_parser(file_b, filename="main.py") 

    call_graph = build_call_graph(build_dir="build")
    print(json.dumps(call_graph, indent=2))