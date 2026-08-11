#!/usr/bin/env python3
"""Make new-contract invariant IDs globally unique in migrated GT packages."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def dump(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def unique(base: str, used: set[str]) -> str:
    candidate = base
    index = 2
    while candidate in used:
        candidate = f"{base}_{index}"
        index += 1
    used.add(candidate)
    return candidate


def _object_key(kind: str, index: int) -> str:
    return f"{kind}:{index}"


def _object_label(item: dict[str, Any], kind: str, index: int) -> dict[str, Any]:
    return {
        "key": _object_key(kind, index),
        "kind": kind,
        "index": index,
        "id": str(item.get("invariant_id") or ""),
        "role": item.get("role"),
        "verified_by": item.get("verified_by"),
    }


def _choose_keeper(
    objects: list[dict[str, Any]],
    *,
    assertions: list[dict[str, Any]],
    criterion_id: str,
) -> str:
    """Pick which duplicate object keeps the original id."""
    if len({obj["kind"] for obj in objects}) > 1:
        node_objects = [obj for obj in objects if obj["kind"] == "node"]
        if node_objects:
            return node_objects[0]["key"]

    assertion_ids = {
        str(assertion.get("id") or "")
        for assertion in assertions
        if objects[0]["id"] in {str(x) for x in assertion.get("invariants", [])}
    }
    covered = [
        obj for obj in objects
        if str(obj.get("verified_by") or "") in assertion_ids
    ]
    if covered:
        return covered[0]["key"]

    root_nodes = [
        obj for obj in objects
        if obj["kind"] == "node"
        and obj["id"] == criterion_id
        and obj.get("role") == "root_cause"
    ]
    if root_nodes:
        return root_nodes[0]["key"]

    non_source_nodes = [
        obj for obj in objects
        if obj["kind"] == "node" and obj.get("role") != "source"
    ]
    if non_source_nodes:
        return non_source_nodes[0]["key"]
    return objects[0]["key"]


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def fix_dir(result_dir: Path, apply: bool) -> dict[str, Any]:
    vi_path = result_dir / "verified_invariants.json"
    va_path = result_dir / "verified_assertions.json"
    if not vi_path.is_file() or not va_path.is_file():
        return {"sample_id": result_dir.name, "status": "skipped", "reason": "missing artifacts"}
    vi = load(vi_path)
    va = load(va_path)
    if "schema_version" in vi or "schema_version" in va:
        return {"sample_id": result_dir.name, "status": "skipped", "reason": "not new contract"}

    nodes = [item for item in vi.get("nodes", []) or [] if isinstance(item, dict)]
    edges = [item for item in vi.get("edges", []) or [] if isinstance(item, dict)]
    assertions = [
        item for item in va.get("assertions", []) or [] if isinstance(item, dict)
    ]
    criterion = vi.get("root_cause_criterion")
    criterion_id = (
        str(criterion.get("invariant_id") or "")
        if isinstance(criterion, dict)
        else ""
    )
    objects: list[dict[str, Any]] = []
    objects.extend(
        _object_label(item, "node", index)
        for index, item in enumerate(nodes)
        if item.get("invariant_id")
    )
    objects.extend(
        _object_label(item, "edge", index)
        for index, item in enumerate(edges)
        if item.get("invariant_id")
    )
    by_id: dict[str, list[dict[str, Any]]] = {}
    for obj in objects:
        by_id.setdefault(obj["id"], []).append(obj)

    duplicate_groups = {
        invariant_id: group
        for invariant_id, group in by_id.items()
        if len(group) > 1
    }
    used = {invariant_id for invariant_id, group in by_id.items() if len(group) == 1}
    rename_by_key: dict[str, str] = {}
    changed = False
    for old_id, group in sorted(duplicate_groups.items()):
        keeper = _choose_keeper(
            group,
            assertions=assertions,
            criterion_id=criterion_id,
        )
        used.add(old_id)
        for obj in group:
            if obj["key"] == keeper:
                rename_by_key[obj["key"]] = old_id
                continue
            suffix = "_node" if obj["kind"] == "node" else "_edge"
            rename_by_key[obj["key"]] = unique(f"{old_id}{suffix}", used)
            changed = True

    for key, new_id in rename_by_key.items():
        kind, raw_index = key.split(":", 1)
        item = nodes[int(raw_index)] if kind == "node" else edges[int(raw_index)]
        if item.get("invariant_id") != new_id:
            item["invariant_id"] = new_id

    if not changed:
        return {"sample_id": result_dir.name, "status": "unchanged"}

    # Endpoints point to node ids.
    def resolve_node_ref(old_id: Any, direction: str) -> Any:
        raw = str(old_id or "")
        group = duplicate_groups.get(raw)
        if not group:
            return old_id
        node_candidates = [obj for obj in group if obj["kind"] == "node"]
        if not node_candidates:
            return old_id
        preferred_roles = (
            ("source", "root_cause", "intermediate", "sink")
            if direction == "from"
            else ("sink", "intermediate", "root_cause", "source")
        )
        for role in preferred_roles:
            for obj in node_candidates:
                if obj.get("role") == role:
                    return rename_by_key.get(obj["key"], raw)
        return rename_by_key.get(node_candidates[0]["key"], raw)

    for edge in edges:
        if not isinstance(edge, dict):
            continue
        edge["from_node"] = resolve_node_ref(edge.get("from_node"), "from")
        edge["to_node"] = resolve_node_ref(edge.get("to_node"), "to")
    if isinstance(criterion, dict):
        raw = str(criterion.get("invariant_id") or "")
        group = duplicate_groups.get(raw)
        if group:
            root = [
                obj for obj in group
                if obj["kind"] == "node" and obj.get("role") == "root_cause"
            ]
            if root:
                criterion["invariant_id"] = rename_by_key.get(root[0]["key"], raw)

    for assertion in assertions:
        if not isinstance(assertion, dict) or not isinstance(assertion.get("invariants"), list):
            continue
        assertion_id = str(assertion.get("id") or "")
        new_invariants: list[str] = []
        for invariant_id in assertion["invariants"]:
            raw = str(invariant_id)
            group = duplicate_groups.get(raw)
            if not group:
                new_invariants.append(raw)
                continue
            matched = [
                rename_by_key.get(obj["key"], raw)
                for obj in group
                if str(obj.get("verified_by") or "") == assertion_id
            ]
            if not matched:
                matched = [
                    rename_by_key.get(obj["key"], raw)
                    for obj in group
                    if rename_by_key.get(obj["key"]) == raw
                ]
            new_invariants.extend(matched)
        assertion["invariants"] = _dedupe_preserve_order(new_invariants)

    renamed = {
        obj["key"]: {
            "old": obj["id"],
            "new": rename_by_key[obj["key"]],
            "kind": obj["kind"],
            "role": obj.get("role"),
            "verified_by": obj.get("verified_by"),
        }
        for group in duplicate_groups.values()
        for obj in group
        if obj["key"] in rename_by_key and rename_by_key[obj["key"]] != obj["id"]
    }

    if apply:
        dump(vi_path, vi)
        dump(va_path, va)
    return {
        "sample_id": result_dir.name,
        "status": "fixed" if apply else "would_fix",
        "renamed": renamed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gt-root", type=Path, default=ROOT / "gt_results")
    parser.add_argument("--sample-id", action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path, default=Path("/tmp/gt_contract_duplicate_id_fix_report.json"))
    args = parser.parse_args()
    targets = [args.gt_root / sid for sid in args.sample_id] if args.sample_id else sorted(args.gt_root.iterdir())
    reports = [
        fix_dir(path, args.apply)
        for path in targets
        if path.is_dir() and not path.name.startswith("_") and ".repair-staging" not in path.name
    ]
    summary = {
        "apply": args.apply,
        "total": len(reports),
        "fixed": sum(1 for item in reports if item["status"] in {"fixed", "would_fix"}),
        "reports": reports,
    }
    args.report.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("apply", "total", "fixed")}, indent=2))
    print(args.report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
