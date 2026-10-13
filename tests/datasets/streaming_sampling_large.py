"""Fresh-process memory evidence; JSON output contains no source row text."""
import hashlib
import json
from pathlib import Path
import resource
import sys

from modelbuilder.dataset import DatasetRegistry, MAX_FILE_BYTES, MAX_ROWS
from modelbuilder.models import ServiceError

root = Path(sys.argv[1])
count = int(sys.argv[2]) if len(sys.argv) > 2 else 120_001
assert count > MAX_ROWS
path = root / 'large.jsonl'
with path.open('w') as stream:
    for i in range(count):
        stream.write(json.dumps({'id': f'item-{i}', 'text': str(i) + 'x' * 300, 'label': i % 3}) + '\n')
with path.open('rb') as stream:
    sha = hashlib.file_digest(stream, 'sha256').hexdigest()
spec = {'kind': 'rows.sample', 'version': '1', 'split_ids': ['train'], 'fields': ['label'],
        'fraction': 0.1, 'seed': 'large', 'allocation': 'proportional', 'min_per_stratum': 1,
        'required_strata': [], 'max_share_error': 0.001}
manifest = {'name': 'bounded memory', 'benchmark': 'generated-local', 'license': 'CC0',
    'provenance': {'source': 'generated fixture'}, 'access_mode': 'streaming',
    'sources': [{'id': 'source', 'root_id': 'data', 'path': path.name, 'format': 'jsonl', 'sha256': sha}],
    'splits': [{'id': 'train', 'role': 'train', 'source_ids': ['source']}], 'id_column': 'id',
    'row_schema': {'type': 'object', 'properties': {'id': {'type': 'string'}, 'text': {'type': 'string'},
                   'label': {'type': 'integer'}}, 'required': ['id', 'text', 'label']}, 'transformations': [spec]}
registry = DatasetRegistry(None, project_roots={'p': {'data': root}})
try:
    registry._materialize('p', {**manifest, 'access_mode': 'eager'})
except ServiceError as error:
    assert error.code == 'dataset_limit_exceeded'
else:
    raise AssertionError('Fixture must cross the actual eager resource policy')
_, index, _, report = registry._materialize('p', manifest)
with index.connect() as db:
    assert db.execute('SELECT COUNT(*) FROM samples WHERE alive=1').fetchone()[0] == count // 10
    assert not db.execute("SELECT name FROM sqlite_master WHERE name='sample_rank'").fetchall()
rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print(json.dumps({'source_rows': index.summary['source_row_count'], 'selected_rows': len(index),
                  'source_bytes': path.stat().st_size, 'index_bytes': index.path.stat().st_size,
                  'peak_index_bytes': index.peak_index_bytes, 'sampling_strata': index.summary['sampling_strata'],
                  'peak_rss_bytes': rss if sys.platform == 'darwin' else rss * 1024,
                  'eager_file_limit': MAX_FILE_BYTES, 'eager_row_limit': MAX_ROWS,
                  'selection_digest': report[0]['selection_digest']}))
