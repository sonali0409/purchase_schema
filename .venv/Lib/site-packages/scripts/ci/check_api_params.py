#  -----------------------------------------------------------------------------------------
#  (C) Copyright IBM Corp. 2026.
#  https://opensource.org/licenses/BSD-3-Clause
#  -----------------------------------------------------------------------------------------

"""
check_api_params.py
-------------------
Compares Python class method (or __init__) parameter names against the
properties defined in a chosen OpenAPI schema object inside watsonx-ai.json.

Usage — run all checks defined in api_params_checks.yaml (default):
    python check_api_params.py

Usage — single custom check:
    python check_api_params.py \
        --module   ibm_watsonx_ai.gateway.gateway_inference \
        --class    GatewayInference \
        --method   chat \
        --schema   CreateChatsRequest

The spec is always downloaded fresh from IBM Cloud, saved with a unique
timestamp filename, and deleted automatically when the script finishes.
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import os
import sys
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Spec download
# ---------------------------------------------------------------------------

# Download from https://cloud.ibm.com/docs/apis/watsonx-ai
SPEC_DOWNLOAD_URL = "https://cloud.ibm.com/docs/apis/watsonx-ai.json"


def download_spec() -> Path:
    """Download the API spec from IBM Cloud and return the path it was saved to.

    The file is written to a temporary file outside the source tree so that
    concurrent runs never collide and the file cannot be accidentally committed.
    """
    fd, dest_str = tempfile.mkstemp(prefix="watsonx-ai-", suffix=".json")
    dest = Path(dest_str)

    print(f"  ⬇️  Downloading spec from {SPEC_DOWNLOAD_URL} …")
    try:
        with urllib.request.urlopen(SPEC_DOWNLOAD_URL, timeout=30) as resp:  # noqa: S310
            os.write(fd, resp.read())
    except Exception as exc:
        print(
            f"  ❌  Download failed: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)
    finally:
        os.close(fd)

    print(f"  ✅  Saved to '{dest}'.\n")
    return dest


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_PATH = Path(__file__).parent / "api_params_checks.yaml"

# Type alias: (module, class, method, schema, sdk_only, spec_only_allowed)
CheckEntry = tuple[str, str, str, str, set[str], set[str]]


def load_checks(config_path: Path | str = DEFAULT_CONFIG_PATH) -> list[CheckEntry]:
    """Load check definitions from *config_path* (YAML).

    Returns a flat list of ``(module, class, method, schema, sdk_only,
    spec_only_allowed)`` tuples.

    The YAML format supports:
    * A class-level ``sdk_only`` list inherited by all its methods.
    * A method-level ``sdk_only`` list merged with the class list.
    * ``method`` as a single name or a list of names sharing the same config.
    * Standard YAML anchors/aliases for shared sets.
    """
    config_path = Path(config_path)

    if yaml is None:
        print(
            "  ⚠️  PyYAML is not installed.\n"
            "      Install it with:  pip install pyyaml",
            file=sys.stderr,
        )
        return []

    if not config_path.exists():
        print(
            f"  ⚠️  Config file not found: '{config_path}'.\n"
            f"      Create it or pass --config <path>.",
            file=sys.stderr,
        )
        return []

    with config_path.open(encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)

    entries: list[CheckEntry] = []
    for cls_block in doc.get("checks", []):
        module = cls_block["module"]
        class_name = cls_block["class"]
        class_sdk_only: set[str] = set(cls_block.get("sdk_only") or [])

        for meth_block in cls_block.get("methods", []):
            raw_method = meth_block["method"]
            method_names: list[str] = (
                raw_method if isinstance(raw_method, list) else [raw_method]
            )
            schema = meth_block["schema"]
            sdk_only = class_sdk_only | set(meth_block.get("sdk_only") or [])
            spec_only_allowed: set[str] = set(meth_block.get("spec_only_allowed") or [])
            for method in method_names:
                entries.append(
                    (module, class_name, method, schema, sdk_only, spec_only_allowed)
                )

    return entries


# ---------------------------------------------------------------------------
# Core data structures
# ---------------------------------------------------------------------------

@dataclass
class CompareResult:
    """Holds the outcome of one method-vs-schema comparison."""

    module_name: str
    class_name: str
    method_name: str
    schema_name: str

    python_only: list[str] = field(default_factory=list)   # in Python, not in spec
    spec_only: list[str] = field(default_factory=list)     # in spec, not in Python
    matched: list[str] = field(default_factory=list)       # in both

    @property
    def ok(self) -> bool:
        return not self.python_only and not self.spec_only

    def print_report(self, *, verbose: bool = False) -> None:
        label = f"{self.class_name}.{self.method_name}  vs  {self.schema_name}"
        width = max(60, len(label) + 4)
        print("=" * width)
        print(f"  {label}")
        print("=" * width)

        if self.ok:
            print("  ✅  All parameters match — no gaps found.")
        else:
            if self.python_only:
                print(f"\n  ⚠️  In Python signature but NOT in spec ({len(self.python_only)}):")
                for p in sorted(self.python_only):
                    print(f"       - {p}")
            if self.spec_only:
                print(f"\n  ❌  In spec but MISSING from Python signature ({len(self.spec_only)}):")
                for p in sorted(self.spec_only):
                    print(f"       - {p}")

        if verbose and self.matched:
            print(f"\n  ✔   Matched ({len(self.matched)}):")
            for p in sorted(self.matched):
                print(f"       = {p}")

        print()


# ---------------------------------------------------------------------------
# Spec loader & schema resolver
# ---------------------------------------------------------------------------

class OpenAPISpec:
    """Minimal OpenAPI 3.x reader that resolves $ref chains in schemas."""

    def __init__(self, path: Path | str) -> None:
        with Path(path).open(encoding="utf-8") as fh:
            self._doc: dict[str, Any] = json.load(fh)

    def schema_properties(self, schema_name: str) -> set[str]:
        """Return the flat set of top-level property names for *schema_name*.

        Resolves allOf / anyOf / oneOf wrappers recursively so that composed
        schemas are handled correctly.
        """
        schemas: dict[str, Any] = self._doc.get("components", {}).get("schemas", {})
        if schema_name not in schemas:
            raise KeyError(
                f"Schema '{schema_name}' not found. Available: "
                + ", ".join(sorted(schemas.keys()))
            )
        return self._collect_properties(schemas[schema_name], schemas)

    def _collect_properties(
        self,
        schema: dict[str, Any],
        all_schemas: dict[str, Any],
    ) -> set[str]:
        props: set[str] = set()

        if "$ref" in schema:
            ref_name = schema["$ref"].split("/")[-1]
            if ref_name in all_schemas:
                return self._collect_properties(all_schemas[ref_name], all_schemas)
            return props

        for key in schema.get("properties", {}):
            props.add(key)

        for keyword in ("allOf", "anyOf", "oneOf"):
            for sub in schema.get(keyword, []):
                props |= self._collect_properties(sub, all_schemas)

        return props


# ---------------------------------------------------------------------------
# Python introspection
# ---------------------------------------------------------------------------

_SKIP_PARAMS = frozenset({"self", "cls"})


def python_params(module_name: str, class_name: str, method_name: str) -> set[str]:
    """Return the set of explicit parameter names for the given method.

    VAR_POSITIONAL (*args) and VAR_KEYWORD (**kwargs) parameters are excluded.
    """
    mod = importlib.import_module(module_name)
    sig = inspect.signature(getattr(getattr(mod, class_name), method_name))
    return {
        name
        for name, param in sig.parameters.items()
        if name not in _SKIP_PARAMS
        and param.kind
        not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    }


# ---------------------------------------------------------------------------
# Main comparison logic
# ---------------------------------------------------------------------------

def compare(
    *,
    spec_path: Path | str,
    module_name: str,
    class_name: str,
    method_name: str,
    schema_name: str,
    sdk_only_params: set[str] | None = None,
    spec_only_allowed: set[str] | None = None,
) -> CompareResult:
    """Compare Python method params against OpenAPI schema properties.

    Parameters
    ----------
    spec_path:
        Path to a downloaded watsonx-ai JSON spec file.
    module_name:
        Dotted import path, e.g. ``"ibm_watsonx_ai.gateway.gateway_inference"``.
    class_name:
        Class name inside that module, e.g. ``"GatewayInference"``.
    method_name:
        Method to inspect, e.g. ``"chat"`` or ``"__init__"``.
    schema_name:
        Top-level schema key in ``components.schemas``, e.g. ``"CreateChatsRequest"``.
    sdk_only_params:
        Python params intentionally absent from the spec (transport / retry params).
    spec_only_allowed:
        Spec fields intentionally absent from Python (e.g. ``"stream"``).
    """
    sdk_only = sdk_only_params or set()
    spec_allowed = spec_only_allowed or set()

    py_params = python_params(module_name, class_name, method_name)
    spec_props = OpenAPISpec(spec_path).schema_properties(schema_name)

    result = CompareResult(
        module_name=module_name,
        class_name=class_name,
        method_name=method_name,
        schema_name=schema_name,
    )

    for p in sorted(py_params):
        if p in sdk_only:
            continue
        if p in spec_props:
            result.matched.append(p)
        else:
            result.python_only.append(p)

    for p in sorted(spec_props):
        if p not in spec_allowed and p not in py_params:
            result.spec_only.append(p)

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare Python method params with OpenAPI schema properties.",
    )
    parser.add_argument(
        "--module", dest="module_name", default=None,
        help="Dotted Python module path, e.g. ibm_watsonx_ai.gateway.gateway_inference",
    )
    parser.add_argument(
        "--class", dest="class_name", default=None,
        help="Class name inside --module",
    )
    parser.add_argument(
        "--method", dest="method_name", default=None,
        help='Method name, e.g. "chat" or "__init__"',
    )
    parser.add_argument(
        "--schema", dest="schema_name", default=None,
        help="Schema name in components.schemas, e.g. CreateChatsRequest",
    )
    parser.add_argument(
        "--sdk-only", nargs="*", default=[], metavar="PARAM",
        help="Python params to treat as SDK-only (not flagged as missing from spec)",
    )
    parser.add_argument(
        "--spec-only-allowed", nargs="*", default=[], metavar="FIELD",
        help="Spec fields to treat as intentionally absent from Python",
    )
    parser.add_argument(
        "--config", default=str(DEFAULT_CONFIG_PATH),
        help="Path to the YAML checks config (default: api_params_checks.yaml next to this script)",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Also print the list of matched parameters",
    )
    return parser


def _print_summary(failed: list[CompareResult]) -> None:
    """Print a compact summary of all failed checks at the end of the run."""
    print("=" * 60)
    if not failed:
        print("  ✅  SUMMARY: all checks passed.")
    else:
        print(f"  ❌  SUMMARY: {len(failed)} check(s) failed:\n")
        for r in failed:
            label = f"{r.class_name}.{r.method_name}  vs  {r.schema_name}"
            print(f"  •  {label}")
            for p in sorted(r.python_only):
                print(f"       ⚠️  in Python only:  {p}")
            for p in sorted(r.spec_only):
                print(f"       ❌  missing in Python: {p}")
    print("=" * 60)


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    # Validate config / yaml availability before downloading the spec
    if not any([args.module_name, args.class_name, args.method_name, args.schema_name]):
        checks = load_checks(args.config)
        if not checks:
            sys.exit(1)
    else:
        checks = None  # single-check path

    spec_path = download_spec()

    failed: list[CompareResult] = []
    try:
        if checks is not None:
            # --- run all configured checks ------------------------------------
            for (mod, cls, meth, schema, sdk_only, spec_allowed) in checks:
                result = compare(
                    spec_path=spec_path,
                    module_name=mod,
                    class_name=cls,
                    method_name=meth,
                    schema_name=schema,
                    sdk_only_params=sdk_only,
                    spec_only_allowed=spec_allowed,
                )
                result.print_report(verbose=args.verbose)
                if not result.ok:
                    failed.append(result)

        else:
            # --- single custom check -----------------------------------------
            missing = [
                flag for flag, val in [
                    ("--module", args.module_name),
                    ("--class", args.class_name),
                    ("--method", args.method_name),
                    ("--schema", args.schema_name),
                ] if not val
            ]
            if missing:
                parser.error(f"When running a custom check you must supply: {', '.join(missing)}")

            result = compare(
                spec_path=spec_path,
                module_name=args.module_name,
                class_name=args.class_name,
                method_name=args.method_name,
                schema_name=args.schema_name,
                sdk_only_params=set(args.sdk_only),
                spec_only_allowed=set(args.spec_only_allowed),
            )
            result.print_report(verbose=args.verbose)
            if not result.ok:
                failed.append(result)

    finally:
        if spec_path.exists():
            spec_path.unlink()
            print(f"  🗑️  Removed spec file '{spec_path}'.\n")
        _print_summary(failed)

    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    main()
