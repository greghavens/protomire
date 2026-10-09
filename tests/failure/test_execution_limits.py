import time
from pathlib import Path
import pytest
from modelbuilder.backend.execution_limits import active_node, bounded_callable, node_timeout


def test_completed_node_clears_deadline(tmp_path):
    with active_node(tmp_path, 'test', 10):
        assert (tmp_path/'active-node.json').exists()
        assert node_timeout(tmp_path) is None
    assert not (tmp_path/'active-node.json').exists()


def test_expired_node_cannot_be_erased_by_next_stage(tmp_path):
    with active_node(tmp_path, 'old', .001):
        time.sleep(.01)
    assert node_timeout(tmp_path)['node_id'] == 'old'
    with pytest.raises(TimeoutError):
        with active_node(tmp_path, 'new', 10):
            pass
    assert node_timeout(tmp_path)['node_id'] == 'old'


def test_typed_signature_is_preserved(tmp_path):
    import inspect
    def fn(value: int) -> int:
        return value * 2
    bound = bounded_callable(fn, Path(tmp_path), {'id':'double', 'timeout':2})
    assert inspect.signature(bound) == inspect.signature(fn)
    assert bound(2) == 4
