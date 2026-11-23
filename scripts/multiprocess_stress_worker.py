"""Deterministic multiprocess stress worker. No external service or MCP SDK.

STRESS-001: collisions on same keys must not produce duplicate dispatch.
STRESS-002: final answers are independent of arrival ordering.
"""
from __future__ import annotations
import json
import sys
import time

from verified_execution.core import LocalAuthority
from verified_execution.worker_leases import LeasedLedger, LeasedToolBridge


def main():
    path, count, iterations, worker_id = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
    ledger = LeasedLedger(path, timeout_seconds=4.0)
    tool = LeasedToolBridge(ledger, principal='multiprocess-test',
                            authority=LocalAuthority.calculator_only())
    result = {'ok':0, 'in_progress':0, 'other':{}, 'total':iterations}
    for step in range(iterations):
        # Shared keys and varied submission orders, but stable intended operation.
        index = (step * 7 + worker_id * 13) % count
        answer = tool.call('checked_add_recorded',
                           {'request_key':f'shared-{index:03d}',
                            'left':index, 'right':1000})
        if answer.ok:
            result['ok'] += 1
        elif answer.error_code == 'IN_PROGRESS':
            result['in_progress'] += 1
        else:
            result['other'][answer.error_code] = result['other'].get(answer.error_code,0)+1
    print(json.dumps(result,sort_keys=True),flush=True)


if __name__ == '__main__':
    main()
