from copy import deepcopy
import pytest
from modelbuilder.artifact_rights import combine_parent_rights, check_required_review
from modelbuilder.evaluation import digest


def envelope(repo, policy):
    return {'schema_version':'modelbuilder.artifact-rights/v1','sources':[{'provider':'huggingface','repo_type':'model','repo_id':repo,'commit':'a'*40,'license_evidence_digest':digest(repo),'license_policy_id':policy}], 'unresolved':[], 'parent_rights_digests':[]}


def test_all_distinct_parent_obligations_survive_with_exact_evidence_refs():
    a,b=envelope('owner/a','review-a'),envelope('owner/b','review-b')
    parents=[{'artifact_id':'a','checkpoint_digest':'a'*64,'rights':a},{'artifact_id':'b','checkpoint_digest':'b'*64,'rights':b}]
    merged=combine_parent_rights(parents)
    assert {row['repo_id'] for row in merged['sources']}=={'owner/a','owner/b'}
    assert {row['license_policy_id'] for row in merged['sources']}=={'review-a','review-b'}
    assert merged['parent_rights_digests']==sorted([digest(a),digest(b)])
    assert 'license' not in merged and 'license_compatible' not in merged
    assert combine_parent_rights(list(reversed(parents)))==merged


def test_missing_and_invalid_parent_rights_remain_unresolved_and_block_use():
    for rights in (None,{'license':'guessed-apache-2.0'}):
        merged=combine_parent_rights([{'artifact_id':'unknown','checkpoint_digest':'a'*64,'rights':rights}])
        assert merged['unresolved'][0]['artifact_id']=='unknown'
        with pytest.raises(ValueError,match='upstream license'):
            check_required_review(merged,'Promotion')


def test_nested_rights_obligations_are_never_cleared_by_reblending():
    parent=envelope('owner/a','review-a')
    parent['unresolved']=[{'artifact_id':'grandparent','reason':'Missing notice'}]
    parent['parent_rights_digests']=['c'*64]
    merged=combine_parent_rights([{'artifact_id':'child','checkpoint_digest':'a'*64,'rights':parent}])
    assert merged['unresolved']==parent['unresolved'] and 'c'*64 in merged['parent_rights_digests']
    altered=deepcopy(merged)
    altered['sources'][0]['license_policy_id']='other'
    assert digest(altered)!=digest(merged)
