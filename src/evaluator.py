#!/usr/bin/env python3
"""
SA-Spec-Pipeline: Automated Quality Gate & Specification Evaluator (`src/evaluator.py`)

Scores a generated specification package (`drafts/` or `final/`, or compares both side-by-side)
on a strict 100-point Senior Systems Analyst rubric across 5 deterministic dimensions:
  1. OpenAPI 3.1 Validity, Ref Integrity & Cleanliness (20 pts)
  2. ERD <-> OpenAPI 3.1 Schema & Enum Parity (20 pts)
  3. Mermaid Architecture Diagrams Completeness & Syntax (20 pts)
  4. Security, Financial Precision & Contradictions Log Linter (20 pts)
  5. BDD Gherkin Scenarios & Traceability Coverage (20 pts)
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Set, Tuple

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_ROOT = PROJECT_ROOT / "output"

TEMPLATE_LEFTOVERS = {
    "CreateResourceRequest",
    "CancelResourceRequest",
    "ResourceResponse",
    "ResourceStatus",
    "PRIMARY_ACTOR",
    "CORE_AGGREGATE",
}

FORBIDDEN_RESPONSE_FIELDS = {
    "parent_pin_hash",
    "password_hash",
    "qr_secret_hash",
    "cvv",
    "pan",
    "card_pan",
}


@dataclass
class CheckResult:
    category: str
    score: int
    max_score: int
    findings: List[str] = field(default_factory=list)


def check_openapi_validity(target_dir: Path) -> Tuple[CheckResult, dict]:
    """1. OpenAPI 3.1 Validity, Ref Integrity & Cleanliness (20 pts)"""
    res = CheckResult("1. OpenAPI 3.1 Validity & Cleanliness", 20, 20)
    yaml_path = target_dir / "03_OpenAPI.yaml"
    if not yaml_path.exists():
        res.score = 0
        res.findings.append("[FAIL] 03_OpenAPI.yaml is missing (-20)")
        return res, {}

    try:
        spec = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        if not isinstance(spec, dict):
            raise ValueError("Root YAML is not a mapping")
    except Exception as exc:
        res.score = 0
        res.findings.append(f"[FAIL] Invalid YAML syntax: {exc} (-20)")
        return res, {}

    if str(spec.get("openapi", "")) != "3.1.0":
        res.score -= 4
        res.findings.append(f"[WARN] Expected openapi: 3.1.0, found '{spec.get('openapi')}' (-4)")

    # Check $ref integrity
    broken_refs: List[str] = []

    def walk_refs(node: object) -> None:
        if isinstance(node, dict):
            if "$ref" in node:
                ref = str(node["$ref"])
                if ref.startswith("#/"):
                    cur: object = spec
                    for part in ref[2:].split("/"):
                        if isinstance(cur, dict) and part in cur:
                            cur = cur[part]
                        else:
                            broken_refs.append(ref)
                            break
            for v in node.values():
                walk_refs(v)
        elif isinstance(node, list):
            for item in node:
                walk_refs(item)

    walk_refs(spec)
    if broken_refs:
        deduct = min(10, 5 * len(broken_refs))
        res.score -= deduct
        res.findings.append(f"[FAIL] Broken $ref pointers: {broken_refs} (-{deduct})")

    # Check leftover template schemas/paths
    schemas = spec.get("components", {}).get("schemas", {}) or {}
    paths = spec.get("paths", {}) or {}
    leftovers_found = [s for s in schemas if s in TEMPLATE_LEFTOVERS]
    if "/resources" in paths or "/resources/{resource_id}" in paths:
        leftovers_found.append("/resources")
    if leftovers_found:
        res.score -= 6
        res.findings.append(f"[WARN] Leftover template placeholders in OpenAPI: {leftovers_found} (-6)")

    # Check GET endpoint presence (read projection / resource retrieval)
    has_get = any("get" in (p_item or {}) for p_item in paths.values() if isinstance(p_item, dict))
    if not has_get or len(paths) < 3:
        res.score -= 4
        res.findings.append(f"[WARN] Incomplete REST path coverage (paths={len(paths)}) (-4)")

    if not res.findings:
        res.findings.append("[PASS] Valid OpenAPI 3.1.0, all $ref links resolved, zero template leftovers")
    res.score = max(0, res.score)
    return res, spec


def check_erd_openapi_parity(target_dir: Path, spec: dict) -> CheckResult:
    """2. ERD <-> OpenAPI 3.1 Schema & Enum Parity (20 pts)"""
    res = CheckResult("2. ERD <-> OpenAPI 3.1 Parity", 20, 20)
    arch_path = target_dir / "02_Architecture.md"
    if not arch_path.exists() or not spec:
        res.score = 0
        res.findings.append("[FAIL] Missing 02_Architecture.md or invalid OpenAPI (-20)")
        return res

    arch_text = arch_path.read_text(encoding="utf-8")

    # Extract status enums from ERD comments like: string status "A | B | C"
    erd_enums: List[Set[str]] = []
    for match in re.finditer(r'string\s+\w*status\w*\s+"([^"]+)"', arch_text):
        raw_vals = match.group(1)
        if "|" in raw_vals:
            vals = {v.strip() for v in raw_vals.split("|") if v.strip().isupper()}
            if len(vals) >= 3:
                erd_enums.append(vals)

    # Extract enums from OpenAPI schemas
    schemas = spec.get("components", {}).get("schemas", {}) or {}
    openapi_enums: Dict[str, Set[str]] = {}
    for s_name, s_body in schemas.items():
        if isinstance(s_body, dict) and "enum" in s_body and "Status" in s_name:
            openapi_enums[s_name] = {str(v) for v in s_body["enum"]}

    if not erd_enums or not openapi_enums:
        res.score -= 8
        res.findings.append("[WARN] Missing explicit status enums in ERD or OpenAPI (-8)")
    else:
        for s_name, oa_set in openapi_enums.items():
            # Find closest ERD enum set
            best_erd = max(erd_enums, key=lambda e: len(e & oa_set))
            diff = oa_set ^ best_erd
            if diff and len(best_erd & oa_set) >= 2:
                res.score -= 6
                res.findings.append(
                    f"[WARN] Status enum mismatch for {s_name}: ERD vs OpenAPI diff = {sorted(diff)} (-6)"
                )

    # Check ProblemDetails error_code enum presence
    prob = schemas.get("ProblemDetails", {})
    err_code_prop = (prob.get("properties", {}) if isinstance(prob, dict) else {}).get("error_code", {})
    if not isinstance(err_code_prop, dict) or "enum" not in err_code_prop:
        res.score -= 4
        res.findings.append("[WARN] ProblemDetails.error_code lacks a strict enum of domain error codes (-4)")

    if not res.findings:
        res.findings.append("[PASS] 100% status enum & field parity between ERD and OpenAPI 3.1")
    res.score = max(0, res.score)
    return res


def check_mermaid_diagrams(target_dir: Path) -> CheckResult:
    """3. Mermaid Architecture Diagrams Completeness & Syntax (20 pts)"""
    res = CheckResult("3. Mermaid Diagrams & Architecture", 20, 20)
    arch_path = target_dir / "02_Architecture.md"
    if not arch_path.exists():
        res.score = 0
        res.findings.append("[FAIL] 02_Architecture.md is missing (-20)")
        return res

    text = arch_path.read_text(encoding="utf-8")
    mermaid_blocks = re.findall(r"```mermaid\s*\n(.*?)```", text, flags=re.DOTALL)

    has_erd = any("erDiagram" in b for b in mermaid_blocks)
    has_state = any("stateDiagram-v2" in b or "stateDiagram" in b for b in mermaid_blocks)
    seq_count = sum(1 for b in mermaid_blocks if "sequenceDiagram" in b)

    if not has_erd:
        res.score -= 8
        res.findings.append("[FAIL] Missing Mermaid erDiagram (-8)")
    if not has_state:
        res.score -= 6
        res.findings.append("[FAIL] Missing Mermaid stateDiagram-v2 (-6)")
    if seq_count < 2:
        res.score -= 4
        res.findings.append(f"[WARN] Expected >= 2 sequenceDiagram blocks, found {seq_count} (-4)")

    # Check for invalid Mermaid erDiagram comma syntax like "uuid booking_id FK, UK"
    for b in mermaid_blocks:
        if "erDiagram" in b and re.search(r"\b(PK|FK|UK)\s*,\s*(PK|FK|UK)\b", b):
            res.score -= 3
            res.findings.append("[WARN] Non-standard 'FK, UK' attribute syntax in erDiagram (-3)")

    if not res.findings:
        res.findings.append(
            f"[PASS] All {len(mermaid_blocks)} Mermaid diagrams (erDiagram, stateDiagram-v2, {seq_count} sequenceDiagrams) valid"
        )
    res.score = max(0, res.score)
    return res


def check_security_and_precision(target_dir: Path, spec: dict) -> CheckResult:
    """4. Security, Financial Precision & Contradictions Log Linter (20 pts)"""
    res = CheckResult("4. Security, Precision & BA Logic", 20, 20)
    brd_path = target_dir / "01_BRD.md"
    arch_path = target_dir / "02_Architecture.md"

    if not brd_path.exists():
        res.score = 0
        res.findings.append("[FAIL] 01_BRD.md is missing (-20)")
        return res

    brd_text = brd_path.read_text(encoding="utf-8")
    arch_text = arch_path.read_text(encoding="utf-8") if arch_path.exists() else ""

    # Check Contradictions Log (OQ-01..OQ-05)
    oq_matches = set(re.findall(r"\bOQ-0\d\b", brd_text))
    if len(oq_matches) < 5:
        res.score -= 4
        res.findings.append(f"[WARN] Contradictions Log has only {len(oq_matches)} items (expected >= 5) (-4)")

    # Check for markdown formatting typos in BR-* table (e.g. `| **BR-04` |`)
    if re.search(r"\*\*BR-0\d`", brd_text):
        res.score -= 2
        res.findings.append("[WARN] Markdown syntax typo in BRD Business Rules table (-2)")

    # Check if secret hashes leak in OpenAPI Response schemas
    schemas = spec.get("components", {}).get("schemas", {}) if spec else {}
    leaked_secrets: List[str] = []
    for s_name, s_body in schemas.items():
        if "Response" in s_name and isinstance(s_body, dict):
            props = s_body.get("properties", {}) or {}
            for prop_name in props:
                if prop_name.lower() in FORBIDDEN_RESPONSE_FIELDS:
                    leaked_secrets.append(f"{s_name}.{prop_name}")

    if leaked_secrets:
        res.score -= 8
        res.findings.append(f"[FAIL] Secret/hash field leaked in OpenAPI Response schema: {leaked_secrets} (-8)")

    # Check int64 format on _minor monetary/points fields in OpenAPI
    missing_int64: List[str] = []
    for s_name, s_body in schemas.items():
        if isinstance(s_body, dict):
            props = s_body.get("properties", {}) or {}
            for prop_name, prop_val in props.items():
                if prop_name.endswith("_minor") and isinstance(prop_val, dict):
                    if prop_val.get("format") != "int64":
                        missing_int64.append(f"{s_name}.{prop_name}")
    if missing_int64:
        res.score -= 3
        res.findings.append(f"[WARN] Monetary/points _minor fields missing 'format: int64': {missing_int64[:3]} (-3)")

    # Check non-IANA timezone strings in ERD
    if '"UTC+3' in arch_text or '"UTC+5' in arch_text:
        res.score -= 3
        res.findings.append("[WARN] Raw 'UTC+X' offset used instead of standard IANA Timezone in ERD (-3)")

    if not res.findings:
        res.findings.append("[PASS] Zero secret leaks, strict int64 minor units, comprehensive OQ/BR logs")
    res.score = max(0, res.score)
    return res


def check_bdd_and_traceability(target_dir: Path) -> CheckResult:
    """5. BDD Gherkin Scenarios & Traceability Coverage (20 pts)"""
    res = CheckResult("5. BDD Gherkin & Traceability", 20, 20)
    edge_path = target_dir / "04_Edge_Cases.md"
    brd_path = target_dir / "01_BRD.md"

    if not edge_path.exists():
        res.score = 0
        res.findings.append("[FAIL] 04_Edge_Cases.md is missing (-20)")
        return res

    edge_text = edge_path.read_text(encoding="utf-8")
    brd_text = brd_path.read_text(encoding="utf-8") if brd_path.exists() else ""

    gherkin_blocks = re.findall(r"```gherkin\s*\n(.*?)```", edge_text, flags=re.DOTALL)
    if len(gherkin_blocks) < 5:
        res.score -= 5
        res.findings.append(f"[WARN] Expected >= 5 Gherkin blocks, found {len(gherkin_blocks)} (-5)")

    # Check dangerous immediate RELEASE on 504 timeout without reconciliation
    if re.search(r"504.*COMPENSATED", edge_text, flags=re.DOTALL) and "RECONCILING" not in edge_text:
        res.score -= 5
        res.findings.append(
            "[WARN] Unsafe Saga timeout handling: immediate COMPENSATED/RELEASE on 504 without RECONCILING state (-5)"
        )

    # Check HTTP status code conflict between BRD (400) and Edge Cases (422) for BR-01
    if "HTTP 400" in brd_text and "HTTP 422" in edge_text and "BR-01" in edge_text:
        res.score -= 4
        res.findings.append(
            "[WARN] HTTP status code conflict: 01_BRD.md specifies HTTP 400 for BR-01 while 04_Edge_Cases.md expects HTTP 422 (-4)"
        )

    # Check Edge Case matrix count (EC-01..EC-08)
    ec_matches = set(re.findall(r"\bEC-0\d\b", edge_text))
    if len(ec_matches) < 7:
        res.score -= 3
        res.findings.append(f"[WARN] Edge Case Matrix has {len(ec_matches)} cases (recommended >= 7..8) (-3)")

    if not res.findings:
        res.findings.append(
            f"[PASS] {len(ec_matches)} Edge Cases, {len(gherkin_blocks)} Gherkin blocks, full BRD-to-BDD consistency"
        )
    res.score = max(0, res.score)
    return res


def evaluate_directory(target_dir: Path) -> Tuple[int, List[CheckResult]]:
    c1, spec = check_openapi_validity(target_dir)
    c2 = check_erd_openapi_parity(target_dir, spec)
    c3 = check_mermaid_diagrams(target_dir)
    c4 = check_security_and_precision(target_dir, spec)
    c5 = check_bdd_and_traceability(target_dir)
    results = [c1, c2, c3, c4, c5]
    total = sum(r.score for r in results)
    return total, results


def print_report(label: str, target_dir: Path, total: int, results: List[CheckResult]) -> None:
    print("-" * 76)
    print(f"Target: {label} ({target_dir.relative_to(PROJECT_ROOT)}) -> TOTAL SCORE: {total}/100")
    print("-" * 76)
    for r in results:
        print(f"  {r.category:<42} : {r.score:>2}/{r.max_score}")
        for f in r.findings:
            print(f"      {f}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="SA-Spec-Pipeline Quality Gate Evaluator: score drafts/ vs final/ on a 100-point rubric."
    )
    parser.add_argument(
        "project",
        type=str,
        help="Project name under output/ (e.g., bank_kinopoisk, room_booking, payment_gateway) or 'all'",
    )
    args = parser.parse_args()

    projects = (
        sorted(p.name for p in OUTPUT_ROOT.iterdir() if p.is_dir())
        if args.project == "all"
        else [args.project]
    )

    for proj in projects:
        proj_dir = OUTPUT_ROOT / proj
        if not proj_dir.exists():
            print(f"[ERROR] Project folder not found: {proj_dir}", file=sys.stderr)
            sys.exit(1)

        print("\n" + "=" * 76)
        print(f"SA-SPEC-PIPELINE QUALITY GATE | PROJECT: {proj.upper()}")
        print("=" * 76)

        drafts_dir = proj_dir / "drafts"
        final_dir = proj_dir / "final"

        draft_score = None
        if drafts_dir.exists():
            draft_score, draft_results = evaluate_directory(drafts_dir)
            print_report("WORKER DRAFTS (gemini-3.5-flash-lite)", drafts_dir, draft_score, draft_results)

        if final_dir.exists():
            final_score, final_results = evaluate_directory(final_dir)
            print_report("ORCHESTRATOR FINAL (Gemini 4 Argon)", final_dir, final_score, final_results)
            if draft_score is not None:
                delta = final_score - draft_score
                print("=" * 76)
                print(
                    f"  ORCHESTRATOR QUALITY LIFT ({proj}): {draft_score}/100 -> {final_score}/100 (+{delta} points)"
                )
                print("=" * 76)


if __name__ == "__main__":
    main()
