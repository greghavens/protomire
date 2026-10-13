from copy import deepcopy
import pytest
from modelbuilder.evaluation import digest
from modelbuilder.scientific import evaluate_policy
from modelbuilder.backend.scientific_gate_artifacts import validate_result, seal_results
from modelbuilder.backend.components import component_catalog, identity
from .test_policy import policy, evidence


def sealed_fixture():
    entry = next(e for e in component_catalog() if e['id'] == 'modelbuilder.scientific.gate')
    component = {k: entry[k] for k in ('id','version','protocol','role','description','parameter_schema','capabilities')}
    reference = {'path':'gate.py','sha256':'a'*64,'module':'gate','entrypoint':'execute','commit':'b'*40}
    p = policy()
    p['evaluator'] = {'component_digest':identity(component),'implementation_ref':reference}
    e = evidence()
    inputs = {side:{'evaluation_records':{r: pair[side]['record'] for r,pair in e.items()}} for side in ('baseline','candidate')}
    node = {'component':component,'implementation_ref':reference,'parameters':{'policy':p}}
    native = {'producer':{'backend_project_id':'p','backend_run_id':'gate-run'},'inputs':[{'input_port':side,'backend_project_id':'p','backend_run_id':side,'artifact_id':side,'content_sha256':'c'*64} for side in ('baseline','candidate')]}
    result = evaluate_policy(p,e)
    return p,e,inputs,node,native,result


def test_native_gate_seal_binds_pinned_source_policy_and_inputs():
    p,e,inputs,node,native,result = sealed_fixture()
    sealed = seal_results({'passed':True,'result':result},node,native,inputs,{'commit':'x'})
    assert sealed['result']['native_execution']['evaluator'] == p['evaluator']
    assert sealed['result']['native_execution']['inputs'] == native['inputs']
    validate_result(sealed['result'],p,e)


@pytest.mark.parametrize('change',['aggregate','rule','cohort','threshold','policy','records','bool','source','foreign'])
def test_native_gate_never_seals_forged_contract(change):
    p,e,inputs,node,native,result = sealed_fixture()
    results={'passed':True,'result':result}
    if change=='aggregate':
        result['passed']=False
    if change=='rule':
        result['rules'][0]['outcome']='failed'
    if change=='cohort':
        result['rules'][0]['cohort_digest']='wrong'
    if change=='threshold':
        result['rules'][0]['threshold']=99
    if change=='policy':
        result['policy_digest']='wrong'
    if change=='records':
        result['rules'][0]['evidence']['candidate']['record']['value']=10
    if change=='bool':
        results['passed']=False
    if change=='source':
        node['implementation_ref']={**node['implementation_ref'],'sha256':'e'*64}
    if change=='foreign':
        native['inputs'][0]['backend_project_id']='foreign'
    result['digest']=digest({k:v for k,v in result.items() if k!='digest'})
    with pytest.raises(ValueError):
        seal_results(results,node,native,inputs,{'commit':'x'})


def test_even_reviewed_custom_result_cannot_pass_incompatible_cohorts():
    p,e,inputs,node,native,result = sealed_fixture()
    from .test_policy import record
    # A fabricated self-consistent passed report still cannot override the
    # mandatory exact-cohort comparator, regardless of its trusted producer.
    e['retention']['candidate']['record']=record('retention',.5,99,'b')
    result['rules'][1]['evidence']=deepcopy(e['retention'])
    result['digest']=digest({k:v for k,v in result.items() if k!='digest'})
    with pytest.raises(ValueError,match='incompatible'):
        validate_result(result,p,e)


@pytest.mark.parametrize("key,value", [("significance_claim", "true"), ("uncertainty", "precise"), ("sample_adequacy", 100)])
def test_custom_report_requires_typed_optional_uncertainty_fields(key, value):
    p,e,inputs,node,native,result = sealed_fixture()
    result["rules"][0][key] = value
    result["digest"] = digest({k:v for k,v in result.items() if k != "digest"})
    with pytest.raises(ValueError):
        validate_result(result,p,e)


def test_custom_report_cannot_invent_a_family_power_claim():
    p,e,inputs,node,native,result = sealed_fixture()
    result["multiple_testing"]["family_power_claim"] = True
    result["digest"] = digest({k:v for k,v in result.items() if k != "digest"})
    with pytest.raises(ValueError, match="multiplicity"):
        validate_result(result,p,e)
