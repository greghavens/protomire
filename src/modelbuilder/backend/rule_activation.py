"""Before-dispatch verification of the service-owned active safeguard snapshot."""
import json
import sqlite3
from urllib.parse import quote
from ..evaluation import digest
from .execution_rights import _store_guard


def recheck(spec):
    authorization = spec.get('rule_authorization')
    if not authorization:
        return
    guard = authorization['store_guard']
    if not guard or _store_guard(guard['path']) != guard:
        raise ValueError('active_rule_authority_unavailable')
    with sqlite3.connect('file:' + quote(guard['path'], safe='/') + '?mode=ro', uri=True) as connection:
        connection.execute('PRAGMA query_only=ON')
        row = connection.execute('SELECT body FROM resources WHERE kind=? AND id=?', ('project', authorization['project_id'])).fetchone()
        if row is None or digest(json.loads(row[0]).get('rule_policy_activation')) != digest(authorization['activation']):
            raise ValueError('active_rule_policy_changed')
    if _store_guard(guard['path']) != guard:
        raise ValueError('active_rule_authority_changed')
