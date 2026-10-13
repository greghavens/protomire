"""Verify actual JUnit results, exact replay bytes and native termination.

The original receipts are immutable. Full verification requires explicit input
locations and creates new output files exclusively; --check-junit is read-only.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[3]
COMMIT='d51a90d93df4a150c088c2d9800c340168b911b4'


def write_new(path, value):
    with path.open('x') as stream:
        stream.write(json.dumps(value,indent=2)+'\n')


def junit_results(path):
    """Read counts and test cases; reject absent, empty or unsuccessful suites."""
    raw=path.read_bytes()
    document=ET.fromstring(raw)
    if document.tag not in {'testsuite','testsuites'}:
        raise ValueError('Expected a JUnit testsuite or testsuites document')
    suites=list(document.iter('testsuite'))
    if not suites or any(suite.findall('testsuite') for suite in suites):
        raise ValueError('Expected one or more non-nested JUnit test suites')
    totals={name:0 for name in ('tests','failures','errors','skipped')}
    for suite in suites:
        counts={}
        for name in totals:
            value=suite.get(name,'')
            if not value.isascii() or not value.isdecimal():
                raise ValueError('Missing or invalid JUnit count: '+name)
            counts[name]=int(value)
        cases=suite.findall('testcase')
        if counts['tests']!=len(cases):
            raise ValueError('JUnit test count does not match testcase records')
        for name,tag in (('failures','failure'),('errors','error'),('skipped','skipped')):
            actual=sum(len(case.findall(tag)) for case in cases)
            if counts[name]!=actual:
                raise ValueError('JUnit outcome count does not match testcase records: '+name)
        for name in totals:
            totals[name]+=counts[name]
    if document.tag=='testsuites':
        for name,total in totals.items():
            value=document.get(name)
            if value is not None and (not value.isascii() or not value.isdecimal() or int(value)!=total):
                raise ValueError('JUnit aggregate count does not match suite records: '+name)
    if not totals['tests']:
        raise ValueError('JUnit report has no executed tests')
    if any(totals[name] for name in ('failures','errors','skipped')):
        raise ValueError('Portability suite contains failures, errors or unexpected skips: '+json.dumps(totals))
    return {**totals,'passed':totals['tests']-totals['failures']-totals['errors']-totals['skipped'],
            'junit':path.name,'junit_sha256':hashlib.sha256(raw).hexdigest()}


def validated_test_evidence(directory):
    path=directory/'final-portability.xml'
    counts=junit_results(path)
    receipt=json.loads((directory/'portability-test-run.json').read_text())
    if receipt.get('returncode')!=0 or receipt.get('junit_absent_before_run') is not True:
        raise ValueError('Missing successful fresh portability test execution')
    if receipt.get('junit_sha256')!=counts['junit_sha256']:
        raise ValueError('JUnit bytes differ from the actual test-run receipt')
    expected=[str(ROOT/'.venv/bin/python'),'-m','pytest','tests/portability','-q','-o','addopts=',
              '--junitxml='+str(path)]
    if receipt.get('command')!=expected or receipt.get('cwd')!=str(ROOT):
        raise ValueError('Unexpected portability test selection or working directory')
    if receipt.get('require_parquet')!='1' or receipt.get('pytest_addopts')!='':
        raise ValueError('Expected complete portability test selection and required Parquet checks')
    return {**counts,'execution_receipt':'portability-test-run.json','command':receipt['command'],
            'started_at':receipt['started_at'],'finished_at':receipt['finished_at'],
            'selection':'Full tests/portability directory; no -k/-m selection and empty addopts. JUnit does not report deselections.'}


def verify(evidence,work):
    EVIDENCE=evidence
    WORK=work
    for name in ('summary.json','frozen-source-after.json','all-runs-termination.json'):
        if (EVIDENCE/name).exists():
            raise FileExistsError('Refusing to overwrite existing receipt: '+str(EVIDENCE/name))
    test_evidence=validated_test_evidence(EVIDENCE)
    rows=[]
    terminations=[]
    comparisons={}
    for fixture in ('custom','pretrained'):
        comparison=json.loads((EVIDENCE/f'{fixture}-comparison.json').read_text())
        assert comparison['source_commit']==COMMIT and comparison['status']=='PASS'
        assert all(comparison['checks'].values())
        for role in ('origin','replay'):
            source=WORK/f'{fixture}-{role}-source'
            recipe=json.loads((source/'recipe.json').read_text())
            assert recipe['source']['commit']==COMMIT
            for item in recipe['source']['files']:
                raw=(source/item['path']).read_bytes()
                committed=subprocess.check_output(['git','show',f'{COMMIT}:{item["path"]}'],cwd=ROOT)
                assert raw==committed and hashlib.sha256(raw).hexdigest()==item['sha256'],item['path']
                assert (ROOT/item['path']).read_bytes()==committed,('working_tree_changed',item['path'])
            jobs=list((WORK/f'{fixture}-{role}-data/backend/jobs').glob('*/state.json'))
            assert len(jobs)==1
            state=json.loads(jobs[0].read_text())
            assert state['worker_reaped'] and state['process_group_terminated'] and state['termination']['confirmed']
            assert state['supervisor_state']=='finished' and not state['cancelled'] and state['worker_exit_code']==0
            terminations.append({'fixture':fixture,'role':role,'state_path':str(jobs[0]),'state':state})
            result=json.loads((EVIDENCE/f'{fixture}-{role}.json').read_text())
            assert result['status']=='PASS'
            expected={item['path']:item['sha256'] for item in recipe['source']['files']}
            for kind in ('service','backend'):
                package=WORK/f'{role}-{kind}/lib/python3.12/site-packages/modelbuilder'
                actual={'src/modelbuilder/'+str(path.relative_to(package)):hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in package.rglob('*.py')}
                assert actual==result[f'{kind}_identity']['installed_source_sha256']
                assert all(expected.get(path)==sha for path,sha in actual.items())
                if kind=='backend':
                    assert 'src/modelbuilder/service.py' not in actual
                    assert result['backend_identity']['distributions']['accelerate']=='1.11.0'
                probe=json.loads((EVIDENCE/f'{role}-{kind}-imports.json').read_text())
                assert probe['status']=='PASS'
            rows.append({'fixture':fixture,'role':role,'source_commit':COMMIT,'source_files':len(expected),
                         'matches_commit_after_execution':True,'working_tree_still_matches_commit':True,
                         'installed_bytes_unchanged':True,'output_files':len(result['output_sha256'])})
        comparisons[fixture]={'status':'PASS','checks_passed':sum(comparison['checks'].values()),
                              'checks_total':len(comparison['checks']),
                              'exact_output_files':len(result['output_sha256'])}
    for kind in ('service','backend'):
        origins=[json.loads((EVIDENCE/f'{role}-{kind}-wheel.json').read_text()) for role in ('origin','replay')]
        assert origins[0]['sha256']==origins[1]['sha256']
        assert all(row['source_commit']==COMMIT and not row['build_hooks_executed'] for row in origins)
        for row in origins:
            assert hashlib.sha256(Path(row['wheel']).read_bytes()).hexdigest()==row['sha256']
    write_new(EVIDENCE/'frozen-source-after.json',{'status':'PASS','source_commit':COMMIT,'snapshots':rows})
    write_new(EVIDENCE/'all-runs-termination.json',{'status':'PASS','runs':terminations})
    summary={'status':'PASS','source_commit':COMMIT,'source_files':rows[0]['source_files'],'comparisons':comparisons,
             'verified':['Four fresh environments, independent source trees and native data/artifact stores',
                         'All committed source files and installed wheel bytes verified before and after execution',
                         'No editable installs, PYTHONPATH, checkout imports, or shared .pth paths',
                         'Both wheel builds are deterministic and execute no build hooks',
                         'Hash-locked backend Trainer overlay installed offline with --no-deps and compatible pip checks',
                         'Source-free imports include streaming, loops, bounded dependency helpers, Trainer/inference and Modal modules',
                         'Both installed service wheels executed actual self-authored PDF extraction through Poppler',
                         'All four native workers reaped, process groups terminated and supervisors reconciled'],
             'portability_tests':test_evidence,
             'installation_report':{'structured_inventory_regenerated':False,'fresh_installation_logs':str(WORK/'logs')},
             'limitations':['Same Linux x64 CPU host; no cross-platform, GPU, remote execution or hostile-code isolation acceptance',
                            'Native replay workloads are repository custom-PyTorch and pretrained-small fixtures, not general Trainer training acceptance',
                            'Optional PyArrow cache absent from new environments; actual eager and streaming helper subprocess tests used the existing pinned PyArrow environment',
                            'PDF system tool versions are recorded; they are system prerequisites, not Python wheel dependencies',
                            'The replay harness does not regenerate the separately captured original final-offline-installation.json inventory; fresh synchronization/install/check logs and runtime distribution inventories support each new run'],
             'reproduction':'replay-exact-runtime.sh','post_verification':'verify-final-replay.py'}
    write_new(EVIDENCE/'summary.json',summary)
    print(json.dumps(summary,indent=2))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-junit',type=Path,help='Read-only validation of one JUnit report')
    parser.add_argument('--evidence-dir',type=Path)
    parser.add_argument('--work-dir',type=Path)
    args=parser.parse_args()
    if args.check_junit:
        if args.evidence_dir or args.work_dir:
            parser.error('--check-junit cannot be combined with full verification')
        print(json.dumps(junit_results(args.check_junit),indent=2))
        return
    if not args.evidence_dir or not args.work_dir:
        parser.error('Full verification requires --evidence-dir and --work-dir')
    if not __debug__:
        parser.error('Verification must not run with Python optimization enabled')
    verify(args.evidence_dir.resolve(),args.work_dir.resolve())


if __name__=='__main__':
    main()
