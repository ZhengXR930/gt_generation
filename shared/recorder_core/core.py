from __future__ import annotations

import json
from pathlib import Path
from typing import Any

VALID_KINDS = {'source', 'sink', 'edge', 'root_cause', 'vulnerability_state', 'retraction'}
VALID_STATUSES = {'hypothesis', 'confirmed', 'retracted'}
VALID_EVIDENCE = {'description', 'code', 'runtime', 'submit', 'inference', ''}
VALID_STAGES = {'', 'early', 'partial', 'pre_submit', 'revision', 'final'}
VALID_CONFIDENCE = {'', 'low', 'medium', 'high'}
VALID_ROLES = {
    '',
    'source',
    'tainted_read',
    'input_materialization',
    'materialization',
    'field_assignment',
    'propagation',
    'parameter_pass',
    'return_value',
    'dispatch',
    'alias',
    'condition',
    'size_calculation',
    'allocation',
    'root_cause',
    'free',
    'sink_access',
    'post_free_use',
    'control_dependency',
}


def parse_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        number = int(str(value).strip())
    except ValueError:
        return None
    return number if number > 0 else None


def normalize_located_claim(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    claim = dict(item)
    if not claim.get('file') and claim.get('location'):
        claim['file'] = claim.get('location')
    if not claim.get('code') and claim.get('statement'):
        claim['code'] = claim.get('statement')
    if not claim.get('text'):
        claim['text'] = (
            claim.get('note')
            or claim.get('description')
            or claim.get('detail')
            or claim.get('condition')
            or ''
        )
    claim['line'] = parse_int(claim.get('line'))
    claim['evidence'] = claim.get('evidence') or 'inference'
    claim['text'] = claim.get('note') or claim.get('text') or ''
    return claim


def normalize_edge(item: Any) -> dict[str, Any]:
    edge = normalize_located_claim(item)
    edge['from'] = edge.get('from') or edge.get('from_') or ''
    edge['to'] = edge.get('to') or ''
    edge['relation'] = edge.get('relation') or edge.get('condition') or ''
    return edge


def record_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def normalize_record(raw_record: dict[str, Any]) -> dict[str, Any]:
    record = dict(raw_record)
    record['status'] = record.get('status') or 'hypothesis'
    record['evidence'] = record.get('evidence') or 'inference'
    record['line'] = parse_int(record.get('line'))
    if record.get('kind') == 'vulnerability_state':
        record['sources'] = [
            normalize_located_claim(item) for item in record_list(record.get('sources'))
        ]
        record['root_causes'] = [
            normalize_located_claim(item) for item in record_list(record.get('root_causes'))
        ]
        record['sinks'] = [
            normalize_located_claim(item) for item in record_list(record.get('sinks'))
        ]
        record['edges'] = [normalize_edge(item) for item in record_list(record.get('edges'))]
        record['open_questions'] = [
            str(item)
            for item in record_list(record.get('open_questions'))
            if str(item).strip()
        ]
    if not str(record.get('text') or '').strip():
        record['text'] = synthesize_event_text(record)
    return record


def prepare_record(
    raw_record: dict[str, Any], *, event_id: int | None = None
) -> tuple[dict[str, Any], list[str], list[str], list[str]]:
    record = normalize_record(raw_record)
    if event_id is not None:
        record['event_id'] = event_id
    errors, warnings, missing = validate_record(record)
    record['warnings'] = warnings
    record['missing_fields'] = missing
    return record, errors, warnings, missing


def process_record(
    raw_record: dict[str, Any],
    previous_records: list[dict[str, Any]],
    *,
    event_id: int | None = None,
    strict: bool = True,
) -> dict[str, Any]:
    record, errors, warnings, missing = prepare_record(raw_record, event_id=event_id)
    validation_errors = list(errors)
    previous_records = [
        normalize_record(item)
        for item in previous_records
        if event_id is None or item.get('event_id') != event_id
    ]
    if not strict:
        state = reduce_records(previous_records + [record])
        record['validation_errors'] = validation_errors
        record['validation_warnings'] = warnings
        record['valid_for_gate'] = not validation_errors
        state['latest_recorded_snapshot'] = record
        state['recording_policy'] = 'evaluation_permissive'
        content = (
            f'recorded reasoning event {event_id}: {record.get("kind")}'
            if not validation_errors
            else (
                f'recorded reasoning event {event_id}: {record.get("kind")} '
                f'with validation issues: {"; ".join(validation_errors)}'
            )
        )
        return {
            'accepted': True,
            'effective': bool(record.get('valid_for_gate')),
            'duplicate': False,
            'content': content,
            'record': record,
            'state': state,
            'errors': validation_errors,
            'warnings': warnings,
            'missing_fields': missing,
            'next_missing': state.get('next_missing', []),
            'next_tools': state.get('next_tools', []),
        }
    previous_state = reduce_records(previous_records)
    previous_complete = not previous_state.get('next_missing', [])
    current_state = reduce_records([record])
    current_complete = not current_state.get('next_missing', [])
    if (
        previous_complete
        and record.get('kind') == 'vulnerability_state'
        and record.get('status') == 'confirmed'
        and current_complete
        and record.get('stage') not in {'revision', 'final'}
        and not errors
    ):
        previous_snapshot = best_snapshot(active_records(previous_records))
        if _snapshot_signature(previous_snapshot) == _snapshot_signature(record):
            errors.append(
                'complete vulnerability_state already exists with the same '
                'localized facts. Continue with record_candidate_plan, '
                'build_candidate, run_candidate, or submit_candidate. Only call '
                'record_vulnerability_state again if your source, root cause, '
                'edge, or sink locations changed.'
            )
        else:
            errors.append(
                'complete vulnerability_state already exists. If your source, '
                'root cause, edge, or sink locations changed, submit the new '
                'complete snapshot explicitly with stage="revision"; implicit '
                'revision is not accepted in strict harness mode.'
            )
    if (
        previous_complete
        and record.get('kind') == 'vulnerability_state'
        and record.get('status') == 'confirmed'
        and not current_complete
        and not errors
    ):
        errors.append(
            'regressive vulnerability_state: a complete vulnerability state '
            'was already recorded. If your understanding changed, record a '
            'new complete snapshot with source, root_cause, edge, and sink; '
            'do not replace it with a partial/source-only snapshot.'
        )

    candidate_records = annotate_duplicate_records(previous_records + [record])
    state = reduce_records(candidate_records)
    current_record = next(
        (
            item
            for item in candidate_records
            if event_id is not None and item.get('event_id') == event_id
        ),
        record,
    )
    duplicate = bool(current_record.get('duplicate'))
    accepted = not errors and not duplicate
    effective = bool(current_record.get('effective') and accepted)
    record['duplicate'] = duplicate
    record['effective'] = effective
    if duplicate:
        next_tools = state.get('next_tools', [])
        next_tool_text = ', '.join(next_tools) if next_tools else 'none'
        errors = [
            f'duplicate {record.get("kind")} record: same file/function/line/var/code '
            'was already accepted. Revise this claim only if the location or claim changed; '
            f'otherwise use one of the next accepted recorder tools: {next_tool_text}.'
        ]
        accepted = False
        effective = False
    if not accepted:
        state = previous_state
    content = (
        f'recorded reasoning event {event_id}: {record.get("kind")}'
        if accepted
        else '; '.join(errors)
    )
    return {
        'accepted': accepted,
        'effective': effective,
        'duplicate': duplicate,
        'content': content,
        'record': record,
        'state': state,
        'errors': errors,
        'warnings': warnings,
        'missing_fields': missing,
        'next_missing': state.get('next_missing', []),
        'next_tools': state.get('next_tools', []),
    }


def synthesize_event_text(record: dict[str, Any]) -> str:
    kind = str(record.get('kind') or 'event')
    file = str(record.get('file') or '')
    function = str(record.get('function') or '')
    line = record.get('line')
    var = str(record.get('var') or '')
    from_value = str(record.get('from') or record.get('from_') or '')
    to_value = str(record.get('to') or '')
    relation = str(record.get('relation') or '')
    role = str(record.get('role') or '')
    location = ':'.join(part for part in [file, function, str(line or '')] if part)
    if kind == 'edge':
        role_text = f'{role} ' if role else ''
        return f'{role_text}{from_value} -> {to_value} via {relation} at {location}'.strip()
    if kind == 'vulnerability_state':
        return str(record.get('text') or record.get('note') or 'vulnerability state snapshot')
    if var:
        return f'{kind} claim for {var} at {location}'.strip()
    return f'{kind} claim at {location}'.strip()


def is_harness_entry_source_record(record: dict[str, Any]) -> bool:
    return (
        record.get('kind') == 'source'
        and record.get('function') == 'LLVMFuzzerTestOneInput'
    )


def is_harness_entry_source_claim(claim: dict[str, Any]) -> bool:
    return claim.get('function') == 'LLVMFuzzerTestOneInput'


def missing_located_claim_fields(claim: dict[str, Any]) -> list[str]:
    required = [
        ('file', claim.get('file')),
        ('function', claim.get('function')),
        ('line', claim.get('line')),
        ('code', claim.get('code')),
        ('text', claim.get('text') or claim.get('note')),
    ]
    return [
        name
        for name, value in required
        if value is None or value == '' or value == []
    ]


def missing_edge_fields(edge: dict[str, Any]) -> list[str]:
    required = [
        ('file', edge.get('file')),
        ('function', edge.get('function')),
        ('line', edge.get('line')),
        ('from', edge.get('from') or edge.get('from_')),
        ('to', edge.get('to')),
        ('relation', edge.get('relation')),
        ('code', edge.get('code')),
        ('text', edge.get('text') or edge.get('note')),
    ]
    return [
        name
        for name, value in required
        if value is None or value == '' or value == []
    ]


def missing_record_fields(record: dict[str, Any]) -> list[str]:
    kind = record.get('kind')
    required: list[tuple[str, Any]] = []
    if kind in {'source', 'sink', 'root_cause'}:
        required = [
            ('file', record.get('file')),
            ('function', record.get('function')),
            ('line', record.get('line')),
            ('code', record.get('code')),
            ('text', record.get('text')),
        ]
    elif kind == 'edge':
        required = [
            ('file', record.get('file')),
            ('function', record.get('function')),
            ('line', record.get('line')),
            ('from', record.get('from') or record.get('from_')),
            ('to', record.get('to')),
            ('relation', record.get('relation')),
            ('code', record.get('code')),
            ('text', record.get('text')),
        ]
    elif kind == 'vulnerability_state':
        required = [
            ('stage', record.get('stage')),
            ('text', record.get('text')),
            ('confidence', record.get('confidence')),
        ]
        if not has_recordable_snapshot_fact(record):
            required.append(('recordable_snapshot_fact', None))
    missing = [
        name
        for name, value in required
        if value is None or value == '' or value == []
    ]
    if is_harness_entry_source_record(record):
        missing.append('project_parser_or_load_source')
    return missing


def validate_record(record: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if record.get('kind') not in VALID_KINDS:
        errors.append(f'invalid kind: {record.get("kind")!r}')
    if record.get('status') not in VALID_STATUSES:
        errors.append(f'invalid status: {record.get("status")!r}')
    if record.get('evidence', '') not in VALID_EVIDENCE:
        errors.append(f'invalid evidence: {record.get("evidence")!r}')
    if record.get('kind') == 'vulnerability_state':
        if record.get('stage', '') not in VALID_STAGES:
            errors.append(f'invalid stage: {record.get("stage")!r}')
        if record.get('confidence', '') not in VALID_CONFIDENCE:
            errors.append(f'invalid confidence: {record.get("confidence")!r}')
        for field in ('sources', 'root_causes', 'edges', 'sinks', 'open_questions'):
            if not isinstance(record.get(field, []), list):
                errors.append(f'{field} must be a list')
        for name in ('sources', 'root_causes', 'sinks'):
            for index, claim in enumerate(record.get(name, []), start=1):
                item_missing = missing_located_claim_fields(claim)
                if name == 'sources' and is_harness_entry_source_claim(claim):
                    item_missing.append('project_parser_or_load_source')
                if item_missing:
                    errors.append(
                        f'{name}[{index}] missing required fields: {", ".join(item_missing)}'
                    )
                if claim.get('evidence', '') not in VALID_EVIDENCE:
                    errors.append(f'{name}[{index}] invalid evidence: {claim.get("evidence")!r}')
        for index, edge in enumerate(record.get('edges', []), start=1):
            item_missing = missing_edge_fields(edge)
            if item_missing:
                errors.append(
                    f'edges[{index}] missing required fields: {", ".join(item_missing)}'
                )
            if edge.get('evidence', '') not in VALID_EVIDENCE:
                errors.append(f'edges[{index}] invalid evidence: {edge.get("evidence")!r}')
        if not has_recordable_snapshot_fact(record):
            errors.append(
                'empty vulnerability_state: include at least one complete source, '
                'root_cause, edge, or sink claim before calling record_vulnerability_state'
            )
    if str(record.get('role') or '') not in VALID_ROLES:
        errors.append(f'invalid role: {record.get("role")!r}')
    missing = missing_record_fields(record)
    if record.get('status') == 'confirmed' and missing:
        errors.append(
            f'confirmed {record.get("kind")} missing required fields: '
            f'{", ".join(missing)}. Use status=hypothesis if this claim is not localized yet.'
        )
    if is_harness_entry_source_record(record):
        warnings.append(
            'LLVMFuzzerTestOneInput is a fuzz harness entry, not an accepted project-level source.'
        )
    return errors, warnings, missing


def is_confirmed_complete_record(record: dict[str, Any]) -> bool:
    if record.get('kind') == 'vulnerability_state':
        return (
            record.get('status') == 'confirmed'
            and not missing_record_fields(record)
            and has_recordable_snapshot_fact(record)
        )
    return record.get('status') == 'confirmed' and not missing_record_fields(record)


def record_fingerprint(record: dict[str, Any]) -> tuple[Any, ...] | None:
    kind = record.get('kind')
    if kind in {'source', 'sink', 'root_cause'}:
        return (
            kind,
            record.get('file'),
            record.get('function'),
            record.get('line'),
            record.get('var'),
            _normalize_code(record.get('code')),
        )
    if kind == 'edge':
        return (
            kind,
            record.get('role'),
            record.get('from') or record.get('from_'),
            record.get('to'),
            record.get('relation'),
            record.get('file'),
            record.get('function'),
            record.get('line'),
            _normalize_code(record.get('code')),
        )
    if kind == 'vulnerability_state':
        # Vulnerability-state records are snapshots. Repeating the same snapshot is
        # harmless and should not trap the agent in a duplicate-record loop.
        return None
    return None


def _normalize_code(value: Any) -> str:
    return ' '.join(str(value or '').split())


def annotate_duplicate_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    annotated: list[dict[str, Any]] = []
    for record in records:
        current = dict(record)
        fingerprint = record_fingerprint(current)
        duplicate = bool(
            fingerprint
            and fingerprint in seen
            and is_confirmed_complete_record(current)
        )
        current['duplicate'] = duplicate
        current['effective'] = bool(
            is_confirmed_complete_record(current)
            and not duplicate
            and current.get('kind') != 'retraction'
        )
        if fingerprint and is_confirmed_complete_record(current) and not duplicate:
            seen.add(fingerprint)
        annotated.append(current)
    return annotated


def active_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    retracted_ids = {
        record.get('retracts')
        for record in records
        if record.get('kind') == 'retraction' and record.get('retracts') is not None
    }
    return [
        record
        for record in records
        if record.get('event_id') not in retracted_ids
        and record.get('kind') != 'retraction'
        and not record.get('duplicate')
    ]


def choose_latest(records: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    matching = [
        record
        for record in records
        if record.get('kind') == kind and is_confirmed_complete_record(record)
    ]
    return matching[-1] if matching else {}


def event_to_state(record: dict[str, Any]) -> dict[str, Any]:
    if not record:
        return {}
    fields = [
        'status',
        'file',
        'function',
        'line',
        'var',
        'role',
        'code',
        'text',
        'evidence',
        'event_id',
        'missing_fields',
    ]
    return {field: record.get(field) for field in fields if record.get(field) not in (None, '')}


def edge_to_state(record: dict[str, Any], step: int) -> dict[str, Any]:
    return {
        'step': step,
        'status': record.get('status'),
        'from': record.get('from') or record.get('from_'),
        'to': record.get('to'),
        'relation': record.get('relation'),
        'file': record.get('file'),
        'function': record.get('function'),
        'line': record.get('line'),
        'var': record.get('var'),
        'role': record.get('role'),
        'code': record.get('code'),
        'text': record.get('text'),
        'evidence': record.get('evidence'),
        'event_id': record.get('event_id'),
    }


def claim_to_state(claim: dict[str, Any], event_id: Any = None) -> dict[str, Any]:
    if not claim:
        return {}
    fields = [
        'file',
        'function',
        'line',
        'var',
        'code',
        'text',
        'note',
        'evidence',
    ]
    state = {field: claim.get(field) for field in fields if claim.get(field) not in (None, '')}
    if event_id is not None:
        state['event_id'] = event_id
    return state


def edge_claim_to_state(edge: dict[str, Any], step: int, event_id: Any = None) -> dict[str, Any]:
    state = {
        'step': step,
        'from': edge.get('from') or edge.get('from_'),
        'to': edge.get('to'),
        'relation': edge.get('relation'),
        'file': edge.get('file'),
        'function': edge.get('function'),
        'line': edge.get('line'),
        'var': edge.get('var'),
        'code': edge.get('code'),
        'text': edge.get('text') or edge.get('note'),
        'evidence': edge.get('evidence'),
    }
    if event_id is not None:
        state['event_id'] = event_id
    return {key: value for key, value in state.items() if value not in (None, '')}


def complete_snapshot_claims(
    record: dict[str, Any], field: str, *, reject_harness_source: bool = False
) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    for claim in record.get(field, []):
        if not isinstance(claim, dict):
            continue
        if missing_located_claim_fields(claim):
            continue
        if reject_harness_source and is_harness_entry_source_claim(claim):
            continue
        claims.append(claim)
    return claims


def complete_snapshot_edges(record: dict[str, Any]) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    for edge in record.get('edges', []):
        if not isinstance(edge, dict):
            continue
        if missing_edge_fields(edge):
            continue
        edges.append(edge)
    return edges


def has_recordable_snapshot_fact(record: dict[str, Any]) -> bool:
    if record.get('kind') != 'vulnerability_state':
        return True
    return any(
        (
            complete_snapshot_claims(record, 'sources', reject_harness_source=True),
            complete_snapshot_claims(record, 'root_causes'),
            complete_snapshot_edges(record),
            complete_snapshot_claims(record, 'sinks'),
        )
    )


def snapshot_completeness_score(record: dict[str, Any]) -> tuple[int, int, int, int, int]:
    return (
        len(complete_snapshot_claims(record, 'sources', reject_harness_source=True)),
        len(complete_snapshot_claims(record, 'root_causes')),
        len(complete_snapshot_edges(record)),
        len(complete_snapshot_claims(record, 'sinks')),
        int(record.get('event_id') or 0),
    )


def snapshot_fact_score(record: dict[str, Any]) -> tuple[int, int, int, int]:
    if not record:
        return (0, 0, 0, 0)
    return (
        len(complete_snapshot_claims(record, 'sources', reject_harness_source=True)),
        len(complete_snapshot_claims(record, 'root_causes')),
        len(complete_snapshot_edges(record)),
        len(complete_snapshot_claims(record, 'sinks')),
    )


def _claim_signature(claim: dict[str, Any]) -> tuple[Any, ...]:
    return (
        claim.get('file'),
        claim.get('function'),
        claim.get('line'),
        claim.get('var'),
        _normalize_code(claim.get('code')),
    )


def _edge_signature(edge: dict[str, Any]) -> tuple[Any, ...]:
    return (
        edge.get('file'),
        edge.get('function'),
        edge.get('line'),
        edge.get('from') or edge.get('from_'),
        edge.get('to'),
        edge.get('relation'),
        edge.get('var'),
        _normalize_code(edge.get('code')),
    )


def _snapshot_signature(record: dict[str, Any]) -> tuple[Any, ...]:
    if not record:
        return ()
    return (
        tuple(
            _claim_signature(claim)
            for claim in complete_snapshot_claims(
                record, 'sources', reject_harness_source=True
            )
        ),
        tuple(_claim_signature(claim) for claim in complete_snapshot_claims(record, 'root_causes')),
        tuple(_edge_signature(edge) for edge in complete_snapshot_edges(record)),
        tuple(_claim_signature(claim) for claim in complete_snapshot_claims(record, 'sinks')),
    )


def best_snapshot(records: list[dict[str, Any]]) -> dict[str, Any]:
    snapshots = [
        record
        for record in records
        if record.get('kind') == 'vulnerability_state'
        and is_confirmed_complete_record(record)
    ]
    if not snapshots:
        return {}
    complete = [
        record
        for record in snapshots
        if all(count > 0 for count in snapshot_fact_score(record))
    ]
    if complete:
        return max(complete, key=lambda record: int(record.get('event_id') or 0))
    return max(
        snapshots,
        key=lambda record: (
            int(record.get('event_id') or 0),
            sum(snapshot_fact_score(record)),
        ),
    )


def state_from_snapshot(
    snapshot: dict[str, Any], records: list[dict[str, Any]]
) -> dict[str, Any]:
    sources = complete_snapshot_claims(
        snapshot, 'sources', reject_harness_source=True
    )
    root_causes = complete_snapshot_claims(snapshot, 'root_causes')
    sinks = complete_snapshot_claims(snapshot, 'sinks')
    edges = complete_snapshot_edges(snapshot)
    coverage = {
        'source_events': len(snapshot.get('sources', [])),
        'sink_events': len(snapshot.get('sinks', [])),
        'edge_events': len(snapshot.get('edges', [])),
        'root_cause_events': len(snapshot.get('root_causes', [])),
        'snapshot_events': sum(
            1 for r in records if r.get('kind') == 'vulnerability_state'
        ),
        'duplicate_events': sum(1 for r in records if r.get('duplicate')),
        'harness_source_warnings': sum(
            1
            for claim in snapshot.get('sources', [])
            if isinstance(claim, dict) and is_harness_entry_source_claim(claim)
        ),
        'confirmed_complete_source_events': len(sources),
        'confirmed_complete_sink_events': len(sinks),
        'confirmed_complete_edge_events': len(edges),
        'confirmed_complete_root_cause_events': len(root_causes),
        'incomplete_hypothesis_events': 0,
        'incomplete_confirmed_events': 0,
        'retractions': sum(1 for r in records if r.get('kind') == 'retraction'),
    }
    next_missing = missing_targets_from_coverage(coverage)
    event_id = snapshot.get('event_id')
    return {
        'primary_source': claim_to_state(sources[0], event_id) if sources else {},
        'all_sources': [claim_to_state(source, event_id) for source in sources],
        'primary_sink': claim_to_state(sinks[-1], event_id) if sinks else {},
        'all_sinks': [claim_to_state(sink, event_id) for sink in sinks],
        'trace': [
            edge_claim_to_state(edge, index + 1, event_id)
            for index, edge in enumerate(edges)
        ],
        # In a vulnerability_state snapshot, agents tend to list the primary
        # root-cause mechanism first and then add contextual facts such as the
        # buffer declaration or outer loop. Keep all entries, but expose the
        # first one as the primary state used by evaluators.
        'primary_root_cause': claim_to_state(root_causes[0], event_id) if root_causes else {},
        'all_root_causes': [
            claim_to_state(root_cause, event_id) for root_cause in root_causes
        ],
        'snapshot': {
            'stage': snapshot.get('stage'),
            'confidence': snapshot.get('confidence'),
            'note': snapshot.get('text'),
            'open_questions': snapshot.get('open_questions', []),
            'event_id': event_id,
        },
        'coverage': coverage,
        'next_missing': next_missing,
        'next_tools': next_tools_for_missing(next_missing),
        'last_event_id': records[-1].get('event_id') if records else None,
    }


def reduce_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    records = annotate_duplicate_records(records)
    active = active_records(records)
    snapshot = best_snapshot(active)
    if snapshot:
        return state_from_snapshot(snapshot, records)
    complete = [record for record in active if is_confirmed_complete_record(record)]
    trace = [
        edge_to_state(record, index + 1)
        for index, record in enumerate(
            record
            for record in active
            if record.get('kind') == 'edge' and is_confirmed_complete_record(record)
        )
    ]
    coverage = {
        'source_events': sum(1 for r in active if r.get('kind') == 'source'),
        'sink_events': sum(1 for r in active if r.get('kind') == 'sink'),
        'edge_events': sum(1 for r in active if r.get('kind') == 'edge'),
        'root_cause_events': sum(1 for r in active if r.get('kind') == 'root_cause'),
        'duplicate_events': sum(1 for r in records if r.get('duplicate')),
        'harness_source_warnings': sum(1 for r in active if is_harness_entry_source_record(r)),
        'confirmed_complete_source_events': sum(1 for r in complete if r.get('kind') == 'source'),
        'confirmed_complete_sink_events': sum(1 for r in complete if r.get('kind') == 'sink'),
        'confirmed_complete_edge_events': sum(1 for r in complete if r.get('kind') == 'edge'),
        'confirmed_complete_root_cause_events': sum(1 for r in complete if r.get('kind') == 'root_cause'),
        'incomplete_hypothesis_events': sum(
            1 for r in active if r.get('status') == 'hypothesis' and missing_record_fields(r)
        ),
        'incomplete_confirmed_events': sum(
            1 for r in active if r.get('status') == 'confirmed' and missing_record_fields(r)
        ),
        'retractions': sum(1 for r in records if r.get('kind') == 'retraction'),
    }
    next_missing = missing_targets_from_coverage(coverage)
    return {
        'primary_source': event_to_state(choose_latest(active, 'source')),
        'all_sources': [
            event_to_state(record)
            for record in active
            if record.get('kind') == 'source' and is_confirmed_complete_record(record)
        ],
        'primary_sink': event_to_state(choose_latest(active, 'sink')),
        'all_sinks': [
            event_to_state(record)
            for record in active
            if record.get('kind') == 'sink' and is_confirmed_complete_record(record)
        ],
        'trace': trace,
        'primary_root_cause': event_to_state(choose_latest(active, 'root_cause')),
        'all_root_causes': [
            event_to_state(record)
            for record in active
            if record.get('kind') == 'root_cause' and is_confirmed_complete_record(record)
        ],
        'coverage': coverage,
        'next_missing': next_missing,
        'next_tools': next_tools_for_missing(next_missing),
        'last_event_id': records[-1].get('event_id') if records else None,
    }


def missing_targets_from_coverage(coverage: dict[str, int]) -> list[str]:
    targets: list[str] = []
    if coverage.get('confirmed_complete_source_events', 0) <= 0:
        targets.append('source')
    if coverage.get('confirmed_complete_root_cause_events', 0) <= 0:
        targets.append('root_cause')
    if coverage.get('confirmed_complete_edge_events', 0) <= 0:
        targets.append('edge')
    if coverage.get('confirmed_complete_sink_events', 0) <= 0:
        targets.append('sink')
    return targets


def next_tools_for_missing(missing: list[str]) -> list[str]:
    return ['record_vulnerability_state'] if missing else []


def load_reasoning_events(events_path: Path) -> list[dict[str, Any]]:
    if not events_path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in events_path.read_text(encoding='utf-8', errors='replace').splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(normalize_record(item))
    return records


def next_event_id(records: list[dict[str, Any]]) -> int:
    current = [
        parse_int(record.get('event_id')) or 0
        for record in records
        if isinstance(record, dict)
    ]
    return max(current or [0]) + 1


def append_reasoning_record(
    raw_record: dict[str, Any],
    *,
    events_path: Path,
    state_path: Path | None = None,
    accepted_only: bool = True,
    event_id: int | None = None,
    strict: bool = True,
) -> dict[str, Any]:
    events_path.parent.mkdir(parents=True, exist_ok=True)
    records = load_reasoning_events(events_path)
    assigned_event_id = event_id if event_id is not None else next_event_id(records)
    result = process_record(
        raw_record, records, event_id=assigned_event_id, strict=strict
    )
    if result['accepted'] or not accepted_only:
        with events_path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(result['record'], ensure_ascii=False) + '\n')
        records = records + [result['record']]
    state = reduce_records(records)
    result['state'] = state
    if state_path is not None:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(state, indent=2, ensure_ascii=False) + '\n',
            encoding='utf-8',
        )
    return result
