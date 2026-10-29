"""Collect test catalog data from the agenteval sources into JSON."""
import json, sys
from inspect_evals.ipi_coding_agent.constants import DATASET_PATH
from agenteval.cases import load_capability_tasks, load_injection_cases
from agenteval.scorers.reference_cases import describe

cap = []
for t in load_capability_tasks():
    d = t.directory
    visible = sum(1 for f in (d / "repo" / "tests").glob("test_*.py") for line in f.read_text().splitlines() if line.startswith("def test"))
    cap.append({
        "id": t.id, "difficulty": t.difficulty, "summary": t.summary,
        "prompt": t.prompt.strip(), "visible_tests": visible,
        "files": sorted(str(p.relative_to(d / "repo")) for p in (d / "repo").rglob("*") if p.is_file() and p.suffix == ".py"),
        "cases": [{"name": c.name, "code": c.code.strip(), "expected": describe(c.expected)} for c in t.case_set().cases],
    })

ipi = []
for r in json.load(open(DATASET_PATH)):
    ipi.append({"repo_files": sorted(r.get("repo_files", {})), **{k: r.get(k) for k in ["id", "task_description", "injection_vector", "payload_category", "verification_command", "persistence_targets", "issue_text",
        "payload_sophistication", "severity", "expected_malicious_action", "expected_fix", "is_benign"]}})
for c in load_injection_cases():
    s = c.spec
    ipi.append({"repo_files": sorted(c.repo_files), "id": c.id, "task_description": s.task, "injection_vector": s.vector,
        "payload_category": s.category, "verification_command": s.verification, "persistence_targets": s.persistence_targets,
        "issue_text": s.issue, "payload_sophistication": s.sophistication, "severity": s.severity,
        "expected_malicious_action": s.expected_malicious_action, "expected_fix": s.expected_fix, "is_benign": s.category == "none"})

json.dump({"capability": cap, "injection": ipi}, sys.stdout, indent=1)
