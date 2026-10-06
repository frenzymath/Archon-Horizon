"""Shared identities for administrative API-journal recovery."""

import shlex
from uuid import UUID, uuid5

JOURNAL_BLOCKER_PREFIX = 'Reconcile the local API intent journal'
INTENT_REPAIR_PREFIX = 'Record repair of API intent '


def blocker_id(assignment_id):
    return uuid5(UUID(str(assignment_id)), 'agent-intent-reconciliation')


def repair_command(key):
    return ('horizon-pipeline agent resolve-intent ' + shlex.quote(str(key))
            + ' --note "Describe the authoritative outcome and corrected operation or owner"')
