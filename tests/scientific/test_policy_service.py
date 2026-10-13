import pytest
from modelbuilder.models import Actor, ServiceError
from modelbuilder.service import OperationService
from .test_policy import policy


def test_active_policy_blocks_hand_entered_acceptance_and_cross_project_lookup(tmp_path):
    service=OperationService(tmp_path/'science.sqlite')
    actor=Actor('owner',{'read','modify','execute'})
    p=service.invoke('project.create',{'slug':'scientific','model_name':'Scientific','idempotency_key':'create'},actor)
    q=service.invoke('project.create',{'slug':'other','model_name':'Other','idempotency_key':'other'},actor)
    pid={'project_id':p['project_id']}
    registered=service.invoke('scientific.policy.register',{**pid,'policy':policy(),'idempotency_key':'register'},actor)
    again=service.invoke('scientific.policy.register',{**pid,'policy':policy(),'idempotency_key':'register-again'},actor)
    assert again==registered
    with pytest.raises(ServiceError):
        service.invoke('scientific.policy.get',{'project_id':q['project_id'],'policy_digest':registered['policy_digest']},actor)
    active=service.invoke('scientific.policy.activate',{**pid,'policy_digest':registered['policy_digest'],'expected_revision':p['revision'],'idempotency_key':'activate'},actor)
    assert active['scientific_policy_digest']==registered['policy_digest']
    with pytest.raises(ServiceError) as stale:
        service.invoke('scientific.policy.activate',{**pid,'policy_digest':registered['policy_digest'],'expected_revision':p['revision'],'idempotency_key':'stale'},actor)
    assert stale.value.code=='revision_conflict'
    recipe=service.invoke('recipe.get',{**pid,'recipe_id':p['recipe_id']},actor)
    service.store.put('operation','op',p['project_id'],{'operation_id':'op',**pid,'backend_run_id':'actual-native-id-fixture','recipe_id':recipe['recipe_id'],'recipe_revision':recipe['revision'],'recipe_digest':recipe['digest'],'revision':1})
    hypothesis=service.invoke('hypothesis.create',{**pid,'proposed_change':'Test configuration','expected_outcome':'Lower loss','acceptance_metric':{'name':'loss'},'idempotency_key':'hypothesis'},actor)
    request={**pid,'recipe_id':recipe['recipe_id'],'recipe_revision':recipe['revision'],'hypothesis_id':hypothesis['hypothesis_id'],
             'run_ids':['actual-native-id-fixture'],'selection_rule':{'metric':'loss','direction':'minimize'},'outcome':'accepted','rationale':'I say accepted','idempotency_key':'decision'}
    with pytest.raises(ServiceError) as denied:
        service.invoke('decision.record',request,actor)
    assert denied.value.code=='scientific_gate_required'
    assert service.invoke('decision.list',pid,actor)['decisions']==[]
    assert service.invoke('decision.record',{**request,'outcome':'rejected','idempotency_key':'rejected'},actor)['outcome']=='rejected'
    service.store.close()
