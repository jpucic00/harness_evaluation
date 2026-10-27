"""Collect test catalog data from the agenteval sources into JSON."""
import json, sys
from pathlib import Path
import yaml
from inspect_evals.ipi_coding_agent.constants import DATASET_PATH
from agenteval.suites.reference_cases import load_case_set, find_cases_file, describe

root = Path("src/agenteval/suites/capability_tasks")
cap = []
for d in sorted(root.iterdir()):
    if not d.is_dir():
        continue
    meta = yaml.safe_load((d / "task.yaml").read_text())
    cs = load_case_set(find_cases_file(d))
    visible = sum(1 for f in (d / "repo" / "tests").glob("test_*.py") for line in f.read_text().splitlines() if line.startswith("def test"))
    cap.append({
        "id": meta["id"], "difficulty": meta["difficulty"], "summary": meta["summary"],
        "prompt": meta["prompt"].strip(), "visible_tests": visible,
        "files": sorted(str(p.relative_to(d / "repo")) for p in (d / "repo").rglob("*") if p.is_file() and p.suffix == ".py"),
        "cases": [{"name": c.name, "code": c.code.strip(), "expected": describe(c.expected)} for c in cs.cases],
    })

ipi = []
for r in json.load(open(DATASET_PATH)):
    ipi.append({"repo_files": sorted(r.get("repo_files", {})), **{k: r.get(k) for k in ["id", "task_description", "injection_vector", "payload_category", "verification_command", "persistence_targets", "issue_text",
        "payload_sophistication", "severity", "expected_malicious_action", "expected_fix", "is_benign"]}})

json.dump({"capability": cap, "injection": ipi}, sys.stdout, indent=1)
