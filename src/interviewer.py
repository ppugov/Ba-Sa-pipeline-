#!/usr/bin/env python3
"""
SA-Spec-Pipeline: Step 0 — Pre-Flight Stakeholder Interviewer (Gap Discovery)

Analyzes a raw business request or meeting notes across the 6 BA/SA Blind Spots
using Gemini Structured Outputs (`response_schema` with Pydantic) to generate:
1. Detected blind spots & ambiguities before writing specs.
2. Concrete clarifying questions for stakeholders with recommended Senior BA/SA defaults.
3. Optional auto-enrichment of the inbox file with approved architectural defaults.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import warnings
from pathlib import Path
from typing import List

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", message=".*NotOpenSSLWarning.*")
warnings.filterwarnings("ignore", module="urllib3")

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

try:
    from pydantic import BaseModel, Field
    from google import genai
    from google.genai import types
except ImportError:
    print(
        "[ERROR] Required packages 'google-genai' and 'pydantic' are not installed.\n"
        "Run: .venv/bin/pip install -r requirements.txt",
        file=sys.stderr,
    )
    sys.exit(1)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = "gemini-3.5-flash-lite"


class ClarifyingQuestion(BaseModel):
    question_id: str = Field(description="Short ID, e.g. Q-01")
    category: str = Field(
        description="One of the 6 BA/SA blind spots: Money & Limits | Rollback & Cancellation | RBAC & Roles | Security & Compliance | Peak Load & Time | Single Source of Truth"
    )
    blind_spot_detected: str = Field(
        description="What is missing, vague, or contradictory in the business request"
    )
    question_for_business: str = Field(
        description="Clear, concise question to ask the Product Owner or Stakeholder in Russian"
    )
    options: List[str] = Field(
        description="2 to 3 concrete options for the stakeholder to choose from in Russian"
    )
    sa_recommended_default: str = Field(
        description="Senior BA/SA recommended architectural decision if stakeholder has no preference"
    )


class DiscoveryReport(BaseModel):
    domain_summary: str = Field(description="Brief 1-2 sentence summary of the business goal in Russian")
    readiness_score_pct: int = Field(
        description="Initial requirement completeness score from 0 to 100 before clarification"
    )
    questions: List[ClarifyingQuestion] = Field(
        description="List of 4 to 6 high-impact clarifying questions covering the 6 BA/SA blind spots"
    )


def load_env_file(env_path: Path) -> None:
    if load_dotenv is not None and env_path.exists():
        load_dotenv(dotenv_path=env_path, override=False)
        return
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() and k.strip() not in os.environ:
            os.environ[k.strip()] = v.strip().strip("'").strip('"')


def run_discovery(input_path: Path, model_name: str, apply_defaults: bool = False) -> DiscoveryReport:
    load_env_file(PROJECT_ROOT / ".env")
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("[ERROR] GEMINI_API_KEY is not set in .env", file=sys.stderr)
        sys.exit(1)

    if not input_path.exists():
        print(f"[ERROR] Input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    raw_text = input_path.read_text(encoding="utf-8")
    client = genai.Client(api_key=api_key)

    system_instruction = (
        "You are a Principal Business & Systems Analyst conducting Step 0 (Pre-Flight Gap Discovery). "
        "Analyze the raw stakeholder notes across the 6 mandatory BA/SA blind spots:\n"
        "1. Money, Quotas & Limits\n"
        "2. Rollback, Refunds & Cancellations (Unhappy Path)\n"
        "3. RBAC, Roles & Anti-Fraud\n"
        "4. Security, PII & Legal Compliance (152-FZ, PCI DSS, Banking Secrecy)\n"
        "5. Peak Load, Timeouts & Timezones\n"
        "6. Single Source of Truth & External System Integration\n"
        "Return a structured JSON report matching the DiscoveryReport schema in Russian."
    )

    response = client.models.generate_content(
        model=model_name,
        contents=f"RAW STAKEHOLDER INPUT:\n{raw_text}",
        config=types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=0.2,
            response_mime_type="application/json",
            response_schema=DiscoveryReport,
        ),
    )

    parts = []
    if response.candidates and response.candidates[0].content and response.candidates[0].content.parts:
        for part in response.candidates[0].content.parts:
            if getattr(part, "text", None):
                parts.append(part.text)
    json_text = "".join(parts) if parts else (response.text or "{}")
    report = DiscoveryReport.model_validate_json(json_text)

    print("=" * 76)
    print(f"SA-Spec-Pipeline | Step 0: Pre-Flight Stakeholder Discovery (Structured Output)")
    print(f"  Source File     : {input_path.name}")
    print(f"  Readiness Score : {report.readiness_score_pct}/100")
    print(f"  Summary         : {report.domain_summary}")
    print("=" * 76)

    for q in report.questions:
        print(f"\n[{q.question_id}] ({q.category})")
        print(f"  Слепая зона : {q.blind_spot_detected}")
        print(f"  Вопрос      : {q.question_for_business}")
        for idx, opt in enumerate(q.options, start=1):
            print(f"    {idx}) {opt}")
        print(f"  Рекомендация SA (Default): {q.sa_recommended_default}")

    if apply_defaults:
        appendix_lines = [
            "\n\n---",
            "[ДОПОЛНЕНИЕ АНАЛИТИКА (Pre-Flight Discovery Defaults)]:",
        ]
        for q in report.questions:
            appendix_lines.append(f"- {q.question_id} ({q.category}): {q.sa_recommended_default}")
        input_path.write_text(raw_text.rstrip() + "\n" + "\n".join(appendix_lines) + "\n", encoding="utf-8")
        print(f"\n[OK] Appended SA recommended defaults to {input_path}")

    print("\n" + "=" * 76)
    return report


def main() -> None:
    load_env_file(PROJECT_ROOT / ".env")
    parser = argparse.ArgumentParser(
        description="Step 0: Analyze raw notes and generate structured clarifying questions for stakeholders."
    )
    parser.add_argument("input_file", type=Path, help="Path to raw notes in inbox/")
    parser.add_argument("--model", type=str, default=os.environ.get("GEMINI_MODEL", DEFAULT_MODEL))
    parser.add_argument(
        "--apply-defaults",
        action="store_true",
        help="Automatically append SA recommended defaults to the input file",
    )
    args = parser.parse_args()
    run_discovery(args.input_file.resolve(), args.model, args.apply_defaults)


if __name__ == "__main__":
    main()
