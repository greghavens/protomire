#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
HARNESS="$(cd "$(dirname "$0")" && pwd)"
WORK="${MODELBUILDER_PORTABLE_WORK:-$ROOT/.runtime/portable-runtime-d51a90d}"
EVIDENCE="${MODELBUILDER_PORTABLE_EVIDENCE:-$WORK/evidence}"
COMMIT=d51a90d93df4a150c088c2d9800c340168b911b4
if [[ "$WORK" != /* || "$EVIDENCE" != /* ]]; then
 echo 'WORK and EVIDENCE paths must be absolute.' >&2
 exit 2
fi
if [[ -e "$WORK" || -L "$WORK" || -e "$EVIDENCE" || -L "$EVIDENCE" ]]; then
 echo 'Replay requires new WORK and EVIDENCE directories; existing receipts are never overwritten.' >&2
 exit 2
fi
export ROOT WORK EVIDENCE COMMIT
export UV_CACHE_DIR="$ROOT/.cache/uv" OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TOKENIZERS_PARALLELISM=false ZENML_ANALYTICS_OPT_IN=false WANDB_MODE=disabled
unset PYTHONPATH VIRTUAL_ENV MODELBUILDER_WANDB_API_KEY WANDB_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY MODELBUILDER_TRACKING_PYTHON
mkdir -p "$WORK/logs" "$EVIDENCE"
cd "$WORK"
"$ROOT/.venv/bin/python" -I - <<'PY'
import os,json,hashlib,subprocess
from pathlib import Path
r=Path(os.environ['ROOT']);e=Path(os.environ['EVIDENCE']);c=os.environ['COMMIT'];records={}
for name in ('custom-pytorch','pretrained-small'):
 recipe=json.loads((r/f'examples/{name}/recipe.json').read_text()); assert recipe['source']['commit']==c
 for f in recipe['source']['files']:
  data=(r/f['path']).read_bytes(); committed=subprocess.check_output(['git','show',f'{c}:{f["path"]}'],cwd=r)
  assert data==committed and hashlib.sha256(data).hexdigest()==f['sha256'],f['path']
 records[name]={'source_commit':c,'source_files':len(recipe['source']['files']),'matches_commit_and_manifest':True,'manifest_sha256':hashlib.sha256((r/f'examples/{name}/recipe.json').read_bytes()).hexdigest()}
(e/'frozen-source-before.json').write_text(json.dumps({'status':'PASS','fixtures':records},indent=2)+'\n')
PY
"$ROOT/.venv/bin/python" -I - <<'PY'
from datetime import datetime,timezone
import hashlib,json,os,subprocess
from pathlib import Path
r=Path(os.environ['ROOT']);e=Path(os.environ['EVIDENCE']);xml=e/'final-portability.xml'
if xml.exists():
 raise FileExistsError('A new portability JUnit report is required')
command=[str(r/'.venv/bin/python'),'-m','pytest','tests/portability','-q','-o','addopts=',
         '--junitxml='+str(xml)]
env={**os.environ,'MODELBUILDER_REQUIRE_PARQUET':'1','PYTEST_ADDOPTS':''}
started=datetime.now(timezone.utc).isoformat()
with (e/'final-portability.log').open('x') as log:
 result=subprocess.run(command,cwd=r,env=env,stdout=log,stderr=subprocess.STDOUT,check=False)
receipt={'command':command,'cwd':str(r),'started_at':started,
         'finished_at':datetime.now(timezone.utc).isoformat(),'returncode':result.returncode,
         'junit_absent_before_run':True,'junit_sha256':hashlib.sha256(xml.read_bytes()).hexdigest() if xml.is_file() else None,
         'require_parquet':env['MODELBUILDER_REQUIRE_PARQUET'],'pytest_addopts':env['PYTEST_ADDOPTS']}
with (e/'portability-test-run.json').open('x') as stream:
 stream.write(json.dumps(receipt,indent=2)+'\n')
if result.returncode:
 raise SystemExit('Portability tests failed; see '+str(e/'final-portability.log'))
PY
"$ROOT/.venv/bin/python" "$HARNESS/verify-final-replay.py" --check-junit "$EVIDENCE/final-portability.xml"
for fixture in custom pretrained; do
 recipe=examples/custom-pytorch/recipe.json
 [[ "$fixture" != pretrained ]] || recipe=examples/pretrained-small/recipe.json
 "$ROOT/.venv/bin/python" -I "$ROOT/scripts/replay_bundle.py" export "$ROOT/$recipe" --root "$ROOT" --commit "$COMMIT" --output "$WORK/$fixture.recipe.tar" > "$EVIDENCE/$fixture-export.json"
 for copy in origin replay; do
  "$ROOT/.venv/bin/python" -I "$ROOT/scripts/replay_bundle.py" import "$WORK/$fixture.recipe.tar" --destination "$WORK/$fixture-$copy-source" > "$WORK/logs/$fixture-$copy-import.json"
 done
done
for copy in origin replay; do
 for kind in service backend; do
  uv venv --allow-existing --python "$ROOT/.venv/bin/python" "$WORK/$copy-$kind" > "$WORK/logs/$copy-$kind-venv.log" 2>&1
  lock="$WORK/custom-$copy-source/requirements/profiles/service-linux-x64.lock"
  extra=()
  if [[ "$kind" == backend ]]; then
   lock="$WORK/custom-$copy-source/requirements/backend.lock"
   extra=(--extra-index-url https://download.pytorch.org/whl/cpu --index-strategy unsafe-best-match)
  fi
  uv pip sync --offline --python "$WORK/$copy-$kind/bin/python" --require-hashes "$lock" "${extra[@]}" > "$WORK/logs/$copy-$kind-sync.log" 2>&1
  if [[ "$kind" == backend ]]; then
   uv pip install --offline --python "$WORK/$copy-$kind/bin/python" --require-hashes --only-binary :all: --no-deps -r "$WORK/custom-$copy-source/requirements/profiles/trainer.lock" > "$WORK/logs/$copy-$kind-trainer.log" 2>&1
  fi
  "$ROOT/.venv/bin/python" -I "$ROOT/scripts/replay_bundle.py" wheel "$WORK/custom-$copy-source" --kind "$kind" --destination "$WORK/$copy-wheels" > "$EVIDENCE/$copy-$kind-wheel.json"
  wheel=modelbuilder-0.1.0-py3-none-any.whl
  [[ "$kind" != backend ]] || wheel=modelbuilder_runtime-0.1.0-py3-none-any.whl
  uv pip install --offline --python "$WORK/$copy-$kind/bin/python" --no-deps "$WORK/$copy-wheels/$wheel" > "$WORK/logs/$copy-$kind-install.log" 2>&1
  uv pip check --python "$WORK/$copy-$kind/bin/python" > "$WORK/logs/$copy-$kind-check.log" 2>&1
  echo "READY installed $copy $kind $(date -u +%FT%TZ)"
 done
done
"$WORK/origin-service/bin/python" -I - <<'PY'
import os,json,hashlib,shutil
from pathlib import Path
r=Path(os.environ['ROOT']);w=Path(os.environ['WORK']);e=Path(os.environ['EVIDENCE']);recipe=json.loads((w/'pretrained-origin-source/recipe.json').read_text());rows=[]
for role in ('origin','replay'):
 d=w/f'{role}-bert-tiny';d.mkdir()
 for resource in recipe['resources']:
  name=resource['url'].rsplit('/',1)[-1];source=r/'.cache/models/bert-tiny'/name;target=d/name
  assert hashlib.sha256(source.read_bytes()).hexdigest()==resource['sha256']
  shutil.copyfile(source,target)
  assert hashlib.sha256(target.read_bytes()).hexdigest()==resource['sha256'] and not source.samefile(target)
  rows.append({'copy':role,**resource,'bytes':target.stat().st_size,'resolved_path':str(target),'separate_file_copy':True})
(e/'pretrained-resource-resolution.json').write_text(json.dumps({'status':'PASS','resolution':'Independent copies of previously downloaded immutable resources, every SHA256 reverified; no downloads','resources':rows},indent=2)+'\n')
PY
for fixture in custom pretrained; do
 for copy in origin replay; do
  extra=()
  [[ "$fixture" != pretrained ]] || extra=(--pretrained-path "$WORK/$copy-bert-tiny")
  echo "RUN $fixture $copy $(date -u +%FT%TZ)"
  "$WORK/$copy-service/bin/python" -I "$WORK/$fixture-$copy-source/scripts/replay_bundle.py" run "$WORK/$fixture-$copy-source" --data-dir "$WORK/$fixture-$copy-data" --backend-python "$WORK/$copy-backend/bin/python" --trust-repository-source --output "$EVIDENCE/$fixture-$copy.json" "${extra[@]}" > "$WORK/logs/$fixture-$copy-run.log" 2>&1
  cat "$WORK/logs/$fixture-$copy-run.log"
 done
 "$WORK/replay-service/bin/python" -I "$WORK/$fixture-replay-source/scripts/replay_bundle.py" compare "$EVIDENCE/$fixture-origin.json" "$EVIDENCE/$fixture-replay.json" --output "$EVIDENCE/$fixture-comparison.json" > "$WORK/logs/$fixture-comparison.log" 2>&1
 cat "$WORK/logs/$fixture-comparison.log"
done
for copy in origin replay; do
 for kind in service backend; do
  "$WORK/$copy-$kind/bin/python" -I "$HARNESS/probe-installed-wheel.py" "$kind" "$EVIDENCE/$copy-$kind-imports.json" > "$EVIDENCE/$copy-$kind-imports.log" 2>&1
 done
done
"$ROOT/.venv/bin/python" "$HARNESS/verify-final-replay.py" --evidence-dir "$EVIDENCE" --work-dir "$WORK"
