"""Measured family presentations committed only after native optimizer updates.

This ledger is deliberately separate from iterator/prefetch and lineage receipts.
Only the pinned single-process CPU float32, non-AMP Trainer path is supported.
"""
from copy import deepcopy

from ...family_coverage import assess_exposure, canonical_policy_identity, family_for_row, validate_policy


class Exposure:
    def __init__(self, policy):
        validate_policy(policy)
        self.policy = deepcopy(policy)
        self.counts = {family['id']: 0 for family in policy['families']}
        self.unique = {family: set() for family in self.counts}
        self.pending = []
        self.successful_updates = 0
        self.finite = True

    def row(self, row, sample_id):
        return {'family': family_for_row(row, self.policy), 'sample_id': sample_id}

    def stage(self, rows, labels, task, loss):
        """Called only after training_step/backward returns successfully."""
        import torch
        self.finite = self.finite and bool(torch.isfinite(loss.detach()).all())
        if task == 'causal-lm':
            eligible = (labels[..., 1:] != -100).any(dim=-1).tolist()
        else:
            eligible = (labels != -100).tolist()
        if len(rows) != len(eligible):
            raise ValueError('Family exposure batch metadata differs from labels')
        self.pending.extend(row for row, keep in zip(rows, eligible) if keep)

    def commit(self, *, skipped=False, finite_gradients=True):
        if not skipped and finite_gradients and self.finite:
            for row in self.pending:
                family = row['family']
                if family not in self.counts:
                    raise ValueError('Family exposure contains an undeclared family')
                self.counts[family] += 1
                self.unique[family].add(row['sample_id'])
            self.successful_updates += 1
        self.pending = []
        self.finite = True

    def snapshot(self):
        if self.pending:
            raise ValueError('Cannot publish family exposure before the optimizer update boundary')
        return {
            'schema_version': 'modelbuilder.family-exposure/v1',
            'policy_id': canonical_policy_identity(self.policy),
            'counts': deepcopy(self.counts),
            'unique_sample_ids': {key: sorted(value) for key, value in self.unique.items()},
            'successful_updates': self.successful_updates,
            'assessment': assess_exposure(self.policy, self.counts),
            'observation': 'successful_finite_optimizer_updates; repeated presentations; shifted causal targets required',
        }

    def restore(self, saved):
        if not isinstance(saved, dict) or saved.get('policy_id') != canonical_policy_identity(self.policy):
            raise ValueError('Exact resume family policy identity differs')
        counts, unique = saved.get('counts'), saved.get('unique_sample_ids')
        if not isinstance(counts, dict) or not isinstance(unique, dict) or set(counts) != set(self.counts) or set(unique) != set(self.unique):
            raise ValueError('Exact resume family counters are incomplete')
        for family, count in counts.items():
            ids = unique[family]
            if type(count) is not int or count < 0 or not isinstance(ids, list) or any(not isinstance(x, str) for x in ids) or len(set(ids)) != len(ids) or len(ids) > count or bool(ids) != bool(count):
                raise ValueError('Exact resume family counters are malformed')
        updates = saved.get('successful_updates')
        if type(updates) is not int or updates < 0 or updates == 0 and any(counts.values()):
            raise ValueError('Exact resume successful update count is malformed')
        self.counts = deepcopy(counts)
        self.unique = {key: set(value) for key, value in unique.items()}
        self.successful_updates = updates
        if self.snapshot() != saved:
            raise ValueError('Exact resume family assessment conflicts with measured counters')


def validate_runtime(trainer, model):
    import torch
    if (str(trainer.args.device) != 'cpu' or trainer.accelerator.num_processes != 1
            or trainer.accelerator.mixed_precision != 'no' or trainer.args.fp16 or trainer.args.bf16
            or any(p.is_floating_point() and p.dtype != torch.float32 for p in model.parameters())):
        raise ValueError('Family exposure requires native single-process CPU float32 without AMP')


def callback(exposure, trainer_getter):
    from transformers import TrainerCallback
    import torch

    class CommitExposure(TrainerCallback):
        finite_gradients = False

        def on_pre_optimizer_step(self, args, state, control, model=None, **kwargs):
            gradients = [p.grad for p in model.parameters() if p.grad is not None]
            self.finite_gradients = bool(gradients) and all(bool(torch.isfinite(g).all()) for g in gradients)
            return control

        def on_optimizer_step(self, args, state, control, model=None, **kwargs):
            finite_parameters = all(bool(torch.isfinite(p).all()) for p in model.parameters())
            exposure.commit(skipped=trainer_getter().accelerator.optimizer_step_was_skipped,
                            finite_gradients=self.finite_gradients and finite_parameters)
            self.finite_gradients = False
            return control

    return CommitExposure()


def verify_family_exposure(checkpoint, policy):
    """Validate a receipt from an already asset-verified native checkpoint."""
    from . import identity
    data = checkpoint.get('data', {})
    if identity(data.get('family_policy')) != identity(policy):
        raise ValueError('Checkpoint family policy is missing or differs')
    ledger = Exposure(policy)
    ledger.restore(data.get('family_exposure'))
    all_ids = [sample for ids in ledger.unique.values() for sample in ids]
    declared = data.get('split', {}).get('sample_ids')
    if len(set(all_ids)) != len(all_ids) or declared is not None and not set(all_ids) <= set(declared):
        raise ValueError('Family unique sample identities differ from registered checkpoint data')
    if ledger.successful_updates > checkpoint.get('step', -1):
        raise ValueError('Family successful updates exceed checkpoint optimizer cursor')
    return ledger.snapshot()


def eligibility(decision, admitted, receipt):
    complete = receipt['assessment']['status'] == 'passed'
    # Only scientific convergence can be deferred. Cancellation, safety/custom
    # decisions and finite budgets remain authoritative, with incomplete coverage.
    convergence_stop = decision['outcome'] == 'stop' and bool(decision.get('rules')) and all(
        row['outcome'] != 'stop' or row.get('kind') == 'convergence/v1' and row.get('reasons') == ['criterion_met']
        for row in decision.get('rules', [])
    )
    outcome = decision['outcome']
    if not complete and convergence_stop and admitted['outcome'] == 'continue':
        outcome = 'continue'
    if outcome not in {'blocked', 'cancelled'} and admitted['outcome'] in {'stop', 'blocked'}:
        outcome = admitted['outcome']
    return {'outcome': outcome, 'coverage_complete': complete,
            'early_convergence_eligible': complete,
            'reason': 'family_exposure_complete' if complete else 'family_exposure_incomplete'}


def verify_family_exposure_path(path, policy):
    """Require native asset-bound evidence, never caller-authored descriptor counts."""
    import json
    from pathlib import Path
    from . import read_checkpoint, identity
    path = Path(path)
    checkpoint = read_checkpoint(path)
    adapter = checkpoint.get('framework', {}).get('adapter')
    names = {asset['path'] for asset in checkpoint['assets']}
    if adapter == 'modelbuilder.transformers.train/v1':
        if 'family-exposure.json' not in names:
            raise ValueError('Native checkpoint lacks its family exposure asset')
        asset_receipt = json.loads((path / 'family-exposure.json').read_text())
    elif adapter == 'modelbuilder.transformers.train/v2':
        if 'epoch-state.json' not in names:
            raise ValueError('Native checkpoint lacks exact epoch family exposure state')
        saved = json.loads((path / 'epoch-state.json').read_text())
        asset_receipt = saved.get('family_exposure')
        if saved.get('contract', {}).get('train', {}).get('family_policy') != policy:
            raise ValueError('Native exact epoch contract family policy differs')
        if identity(saved.get('contract')) != checkpoint.get('training', {}).get('contract_digest'):
            raise ValueError('Native exact epoch contract identity differs')
    else:
        raise ValueError('Family exposure producer is not a supported native Trainer')
    receipt = verify_family_exposure(checkpoint, policy)
    if asset_receipt != receipt:
        raise ValueError('Native checkpoint family exposure asset differs from manifest')
    if checkpoint.get('execution', {}).get('device') != 'cpu' or checkpoint.get('execution', {}).get('dtype') != 'float32':
        raise ValueError('Native family exposure execution mode is unsupported')
    return receipt



def validate_eager_envelope(data, rows):
    """Check registered composition receipts independently of optimizer exposure.

    Explicit adapter policies without registry receipts are supported by measuring
    the complete eager rows here. Arbitrary source-owned input forgery is outside
    this adapter's provenance boundary; an activated native gate still requires
    the selected checkpoint's actual policy-bound exposure asset.
    """
    from ...dataset import final_family_coverage
    reports = [value['final_dataset_family_coverage'] for value in
               (data, data.get('split', {}), data.get('consumption', {}))
               if 'final_dataset_family_coverage' in value]
    policy = data.get('family_policy')
    if policy is None:
        if reports:
            raise ValueError('Registered final family coverage requires its matching family policy')
        return {}
    validate_policy(policy)
    measured = final_family_coverage(policy, data['split']['id'], rows)
    if reports and ('final_dataset_family_coverage' not in data
                    or 'final_dataset_family_coverage' not in data.get('consumption', {})
                    or any(report != measured for report in reports)):
        raise ValueError('Registered final family coverage differs from measured eager rows or envelope')
    return {'family_policy': deepcopy(policy), 'final_dataset_family_coverage': measured}
