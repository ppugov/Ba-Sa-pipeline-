#!/usr/bin/env python3
"""
SA-Spec-Pipeline: Worker Script (Orchestrator-Worker Pattern)

Reads raw stakeholder notes from an input file and sequentially invokes
the lightweight Gemini Worker model (`gemini-3.5-flash-lite` via `google-genai` SDK)
with rate-limit safe pauses to populate all 4 specification templates into:
    output/<project_name>/drafts/
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import warnings
from pathlib import Path
from typing import Dict, List, Tuple

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", message=".*NotOpenSSLWarning.*")
warnings.filterwarnings("ignore", module="urllib3")

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

try:
    from google import genai
    from google.genai import types
    from google.genai.errors import APIError
except ImportError:
    print(
        "[ERROR] Official 'google-genai' SDK is not installed.\n"
        "Run: pip install -r requirements.txt  (or: pip install google-genai python-dotenv)",
        file=sys.stderr,
    )
    sys.exit(1)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = PROJECT_ROOT / "templates"
OUTPUT_ROOT = PROJECT_ROOT / "output"

DEFAULT_MODEL = "gemini-3.5-flash-lite"
DEFAULT_DELAY_SECONDS = 5.0
MAX_RETRIES = 4

PIPELINE_STEPS: List[Tuple[str, str]] = [
    (
        "01_BRD.md",
        (
            "You are a Senior Business & Systems Analyst Worker in a universal Orchestrator-Worker pipeline. "
            "Populate the Business Requirements Document (BRD) template using the raw stakeholder notes from ANY business domain. "
            "CRITICAL: Identify every contradiction, stakeholder conflict, ambiguity, security/compliance risk, and technical flaw "
            "in the raw notes and explicitly document them in Section 7 (Contradictions, Ambiguities & Open Questions Log) "
            "along with a concrete BA/SA architectural resolution. Output ONLY the completed Markdown document."
        ),
    ),
    (
        "02_Architecture.md",
        (
            "You are a Senior Systems Analyst Worker in a universal Orchestrator-Worker pipeline. "
            "Populate the System Architecture & Data Model template based on the raw notes and the generated 01_BRD.md. "
            "Replace the template placeholder entities with the real domain entities of the project. "
            "Ensure valid Mermaid syntax for `erDiagram`, `stateDiagram-v2`, and `sequenceDiagram`. "
            "Enforce domain best practices (proper data types, integer minor units if money is involved, PII/security compliance, "
            "Idempotency-Key handling, concurrency control, and Transactional Outbox for events). Output ONLY the completed Markdown document."
        ),
    ),
    (
        "03_OpenAPI.yaml",
        (
            "You are a Senior Systems Analyst Worker in a universal Orchestrator-Worker pipeline. "
            "Populate the OpenAPI 3.1.0 YAML specification template based on 01_BRD.md and 02_Architecture.md. "
            "Replace placeholder `/resources` endpoints and schemas with the actual domain resources of the project. "
            "CRITICAL: Every schema property name, data type, and status enum in OpenAPI MUST strictly match "
            "the ERD entities and State Machine in 02_Architecture.md. Output ONLY raw valid YAML (do not wrap in markdown code fences)."
        ),
    ),
    (
        "04_Edge_Cases.md",
        (
            "You are a Senior Systems Analyst & QA Architect Worker in a universal Orchestrator-Worker pipeline. "
            "Populate the Edge Cases, Failure Modes & BDD Test Scenarios template based on 01_BRD.md, "
            "02_Architecture.md, and 03_OpenAPI.yaml. Include comprehensive Gherkin scenarios covering "
            "idempotency, domain business rules (BR-*), boundary conditions, invalid state transitions, "
            "concurrency race conditions, and external integration timeouts. Output ONLY the completed Markdown document."
        ),
    ),
]


def load_env_file(env_path: Path) -> None:
    """Load environment variables from .env file using python-dotenv or built-in parser."""
    if load_dotenv is not None and env_path.exists():
        load_dotenv(dotenv_path=env_path, override=False)
        return

    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


def derive_project_name(input_path: Path, explicit_name: str | None = None) -> str:
    """Derive clean project folder name from input file stem."""
    if explicit_name:
        return re.sub(r"[^a-zA-Z0-9_-]+", "_", explicit_name.strip()).strip("_").lower()
    stem = input_path.stem.lower()
    stem = re.sub(r"_raw$", "", stem)
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", stem).strip("_") or "project"


def strip_outer_code_fence(text: str, is_yaml: bool = False) -> str:
    """Remove accidental outer ```yaml / ```markdown code fences returned by the LLM."""
    cleaned = text.strip()
    if is_yaml:
        match = re.match(r"^```(?:yaml|yml)?\s*\n(.*?)\n```\s*$", cleaned, flags=re.DOTALL | re.IGNORECASE)
        if match:
            return match.group(1).strip() + "\n"
    else:
        match = re.match(r"^```(?:markdown|md)\s*\n(.*?)\n```\s*$", cleaned, flags=re.DOTALL | re.IGNORECASE)
        if match:
            return match.group(1).strip() + "\n"
    return cleaned + "\n"


def generate_with_retry(
    client: genai.Client,
    model_name: str,
    system_instruction: str,
    user_prompt: str,
    base_delay: float,
) -> str:
    """Call Gemini API with exponential backoff for rate-limit (429) or transient errors."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=user_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=0.2,
                ),
            )
            parts = []
            if response.candidates and response.candidates[0].content and response.candidates[0].content.parts:
                for part in response.candidates[0].content.parts:
                    if getattr(part, "text", None):
                        parts.append(part.text)
            text_result = "".join(parts) if parts else (response.text or "")
            if not text_result:
                raise RuntimeError("Empty response text received from Gemini API.")
            return text_result
        except APIError as exc:
            status_code = getattr(exc, "code", None)
            is_rate_limit = status_code == 429 or "429" in str(exc) or "RESOURCE_EXHAUSTED" in str(exc)
            if attempt < MAX_RETRIES and (is_rate_limit or (status_code and status_code >= 500)):
                wait_s = base_delay * (2 ** (attempt - 1))
                print(
                    f"  [WARN] Gemini API transient/rate-limit error (attempt {attempt}/{MAX_RETRIES}): {exc}. "
                    f"Retrying in {wait_s:.1f}s..."
                )
                time.sleep(wait_s)
                continue
            raise


def run_pipeline(
    input_file: Path,
    project_name: str,
    model_name: str,
    delay_seconds: float,
) -> Path:
    """Execute the 4-stage Worker draft generation pipeline."""
    load_env_file(PROJECT_ROOT / ".env")
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key or api_key == "your_free_gemini_api_key_here":
        print(
            "[ERROR] GEMINI_API_KEY is missing or still set to placeholder.\n"
            "Please set a valid GEMINI_API_KEY in .env or environment variables.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not input_file.exists():
        print(f"[ERROR] Input file not found: {input_file}", file=sys.stderr)
        sys.exit(1)

    raw_notes = input_file.read_text(encoding="utf-8")
    drafts_dir = OUTPUT_ROOT / project_name / "drafts"
    drafts_dir.mkdir(parents=True, exist_ok=True)

    client = genai.Client(api_key=api_key)

    print("=" * 72)
    print(f"SA-Spec-Pipeline | Worker Stage")
    print(f"  Project Name : {project_name}")
    print(f"  Input File   : {input_file}")
    print(f"  Worker Model : {model_name}")
    print(f"  Output Dir   : {drafts_dir}")
    print("=" * 72)

    generated_docs: Dict[str, str] = {}

    for idx, (template_name, step_instruction) in enumerate(PIPELINE_STEPS, start=1):
        template_path = TEMPLATES_DIR / template_name
        if not template_path.exists():
            print(f"[ERROR] Template missing: {template_path}", file=sys.stderr)
            sys.exit(1)

        template_content = (
            template_path.read_text(encoding="utf-8")
            .replace("{{PROJECT_NAME}}", project_name)
            .replace("{{SOURCE_FILE}}", str(input_file.relative_to(PROJECT_ROOT) if input_file.is_relative_to(PROJECT_ROOT) else input_file))
        )

        prior_context_parts: List[str] = []
        for prev_name, prev_content in generated_docs.items():
            prior_context_parts.append(f"--- BEGIN PREVIOUSLY GENERATED {prev_name} ---\n{prev_content}\n--- END {prev_name} ---")
        prior_context_block = "\n\n".join(prior_context_parts) if prior_context_parts else "None (this is the first document)."

        user_prompt = (
            f"PROJECT NAME: {project_name}\n\n"
            f"=== RAW STAKEHOLDER NOTES ({input_file.name}) ===\n"
            f"{raw_notes}\n\n"
            f"=== PREVIOUSLY GENERATED PIPELINE ARTIFACTS ===\n"
            f"{prior_context_block}\n\n"
            f"=== TARGET TEMPLATE TO POPULATE ({template_name}) ===\n"
            f"{template_content}\n\n"
            f"Populate the template thoroughly in Russian (matching the stakeholder notes language, while keeping "
            f"code/API identifiers, field names, statuses, and Gherkin keywords standard). "
            f"Preserve all structural sections of the template."
        )

        print(f"\n[{idx}/{len(PIPELINE_STEPS)}] Generating draft: {template_name} ...")
        start_ts = time.time()
        raw_output = generate_with_retry(
            client=client,
            model_name=model_name,
            system_instruction=step_instruction,
            user_prompt=user_prompt,
            base_delay=max(delay_seconds, 2.0),
        )
        elapsed = time.time() - start_ts

        cleaned_output = strip_outer_code_fence(
            raw_output,
            is_yaml=template_name.endswith((".yaml", ".yml")),
        )
        generated_docs[template_name] = cleaned_output

        out_file = drafts_dir / template_name
        out_file.write_text(cleaned_output, encoding="utf-8")
        print(f"  [OK] Saved {out_file.relative_to(PROJECT_ROOT)} ({len(cleaned_output)} chars, {elapsed:.1f}s)")

        if idx < len(PIPELINE_STEPS) and delay_seconds > 0:
            print(f"  [RATE-LIMIT GUARD] Sleeping {delay_seconds:.1f}s before next request...")
            time.sleep(delay_seconds)

    print("\n" + "=" * 72)
    print(f"[DONE] All 4 draft specifications saved to: {drafts_dir}")
    print("Next step: Orchestrator (Gemini 4 Argon) reviews drafts per .agy/instructions.md")
    print("=" * 72)
    return drafts_dir


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="SA-Spec-Pipeline Worker: generate draft SA specifications using Gemini Flash-Lite."
    )
    parser.add_argument(
        "input_file",
        type=Path,
        help="Path to raw meeting notes file (e.g., inbox/payment_gateway_raw.txt)",
    )
    parser.add_argument(
        "--project-name",
        type=str,
        default=None,
        help="Optional explicit project name (defaults to input filename stem without _raw)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help=f"Gemini model ID (defaults to GEMINI_MODEL env var or '{DEFAULT_MODEL}')",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=None,
        help=f"Pause in seconds between API calls for rate-limit safety (default: {DEFAULT_DELAY_SECONDS}s)",
    )
    return parser.parse_args()


def main() -> None:
    load_env_file(PROJECT_ROOT / ".env")
    args = parse_args()
    input_path = args.input_file.resolve()
    project_name = derive_project_name(input_path, args.project_name)
    model_name = args.model or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
    delay_seconds = (
        args.delay
        if args.delay is not None
        else float(os.environ.get("WORKER_DELAY_SECONDS", DEFAULT_DELAY_SECONDS))
    )
    run_pipeline(
        input_file=input_path,
        project_name=project_name,
        model_name=model_name,
        delay_seconds=delay_seconds,
    )


if __name__ == "__main__":
    main()
