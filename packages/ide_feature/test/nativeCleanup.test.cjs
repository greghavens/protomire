/* Cleanup protocol regression tests: real leases and Python processes, no native models. */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const cp = require('node:child_process');
const {EventEmitter} = require('node:events');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const {setTimeout: delay} = require('node:timers/promises');

const root = path.resolve(__dirname, '../../..');
const python = process.env.MODELBUILDER_GUI_PYTHON || path.join(root, '.venv/bin/python');
const helper = path.join(__dirname, 'waitNativeQuiescence.py');
const options = {timeout: 15000, skip: process.platform === 'win32' ? 'POSIX inherited launch leases' : false};

// The supervisor and worker have genuine psutil birth identities. In held mode
// both exit, but a late configuration writer inherits the original launch fd.
const fixtureCode = String.raw`
import fcntl, json, os, pathlib, subprocess, sys, time
import psutil
base, mode, name = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
job = base / 'backend' / 'jobs' / name
job.mkdir(parents=True)
def write(name, value):
    (job / name).write_text(json.dumps(value))
def gate(name):
    deadline = time.monotonic() + 12
    while not (base / name).exists():
        if time.monotonic() >= deadline:
            raise RuntimeError('fixture gate timed out: ' + name)
        time.sleep(.01)
lease = (job / 'launch.lock').open('w')
fcntl.flock(lease.fileno(), fcntl.LOCK_EX)
worker = subprocess.run([sys.executable, '-c',
    "import json,os,psutil; print(json.dumps({'pid':os.getpid(), 'identity':'psutil:'+psutil.Process().create_time().hex()}))"],
    check=True, capture_output=True, text=True, start_new_session=True)
worker = json.loads(worker.stdout)
state = {'supervisor_pid': os.getpid(), 'supervisor_identity': 'psutil:' + psutil.Process().create_time().hex(),
         'worker_pid': worker['pid'], 'worker_identity': worker['identity'],
         'worker_reaped': True, 'process_group_terminated': True, 'supervisor_state': 'finished'}
write('spawn_attempt.json', {'attempt_id': name})
write('state.json', state)
(base / 'config.json').write_text('original config\n')
if mode == 'held':
    late_writer = r'''
import json, os, pathlib, sys, time
import psutil
base, lease_fd = pathlib.Path(sys.argv[1]), int(sys.argv[2])
(base / 'writer.ready').write_text(json.dumps({'pid': os.getpid(), 'identity': 'psutil:' + psutil.Process().create_time().hex()}))
def gate(name):
    deadline = time.monotonic() + 12
    while not (base / name).exists():
        if time.monotonic() >= deadline:
            raise RuntimeError('late writer gate timed out: ' + name)
        time.sleep(.01)
gate('allow-write')
(base / 'config.json').write_text('late config write\n')
(base / 'writer.wrote').write_text('written')
gate('allow-exit')
os.close(lease_fd)
(base / 'writer.done').write_text('done')
'''
    subprocess.Popen([sys.executable, '-c', late_writer, str(base), str(lease.fileno())],
                     pass_fds=(lease.fileno(),), start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    gate('writer.ready')
lease.close()
(base / (name + '.ready')).write_text(json.dumps(state))
if mode == 'live':
    gate('allow-exit')
`;

// Observe the actual failed nonblocking flock, rather than guessing when the
// helper has run from a sleep. The real helper and kernel locking are unchanged.
const observedHelperCode = String.raw`
import fcntl, pathlib, runpy, sys
helper, marker = sys.argv[1], pathlib.Path(sys.argv[2])
sys.argv = [helper, *sys.argv[3:]]
original_flock = fcntl.flock
def observed_flock(fd, operation):
    try:
        return original_flock(fd, operation)
    except BlockingIOError:
        marker.write_text('blocked on inherited lease')
        raise
fcntl.flock = observed_flock
runpy.run_path(helper, run_name='__main__')
`;

async function exists(file) {
  try { await fs.access(file); return true; } catch (error) {
    if (error.code === 'ENOENT') return false;
    throw error;
  }
}

async function waitFor(file, processHandle) {
  const deadline = Date.now() + 5000;
  while (!(await exists(file))) {
    if (processHandle?.settled) {
      const result = await processHandle.done;
      assert.fail(`Process exited before ${path.basename(file)}: ${JSON.stringify(result)}`);
    }
    assert(Date.now() < deadline, `Timed out waiting for ${file}`);
    await delay(10);
  }
}

async function context(t) {
  const base = await fs.mkdtemp(path.join(os.tmpdir(), 'modelbuilder-cleanup-'));
  const ctx = {base, backend: path.join(base, 'backend'), children: []};
  t.after(async () => {
    // Release only our gated fixture processes, then remove only this test's temp directory.
    await fs.writeFile(path.join(base, 'allow-write'), 'cleanup');
    await fs.writeFile(path.join(base, 'allow-exit'), 'cleanup');
    if (await exists(path.join(base, 'writer.ready'))) await waitFor(path.join(base, 'writer.done'));
    for (const child of ctx.children) {
      if (!child.settled) child.process.kill('SIGTERM');
      await child.done;
    }
    await fs.rm(base, {recursive: true, force: true});
  });
  return ctx;
}

function start(ctx, args) {
  const process = cp.spawn(python, args, {cwd: root, stdio: ['ignore', 'pipe', 'pipe']});
  const handle = {process, settled: false, stdout: '', stderr: ''};
  process.stdout.on('data', data => { handle.stdout += data; });
  process.stderr.on('data', data => { handle.stderr += data; });
  handle.done = new Promise(resolve => {
    process.on('error', error => { handle.error = error.message; });
    process.on('close', (code, signal) => {
      handle.settled = true;
      resolve({code, signal, stdout: handle.stdout, stderr: handle.stderr, error: handle.error});
    });
  });
  ctx.children.push(handle);
  return handle;
}

async function fixture(ctx, mode, name = 'job') {
  const owner = start(ctx, ['-c', fixtureCode, ctx.base, mode, name]);
  await waitFor(path.join(ctx.base, name + '.ready'), owner);
  if (mode !== 'live') assertSuccess(await owner.done);
  return owner;
}

function waitNative(ctx, timeout = 2, marker) {
  return start(ctx, marker
    ? ['-c', observedHelperCode, helper, marker, ctx.backend, String(timeout)]
    : [helper, ctx.backend, String(timeout)]);
}

function assertSuccess(result) {
  assert.equal(result.code, 0, JSON.stringify(result));
  assert.equal(result.signal, null, JSON.stringify(result));
}

function assertRefused(result) {
  assert.equal(result.code, 1, JSON.stringify(result));
  assert.match(result.stderr, /Cleanup refused; retain evidence/);
}

async function snapshot(directory) {
  const values = {};
  for (const entry of await fs.readdir(directory, {withFileTypes: true})) {
    const name = path.join(directory, entry.name);
    values[entry.name] = entry.isDirectory() ? await snapshot(name) : await fs.readFile(name, 'utf8');
  }
  return values;
}

test('native cleanup succeeds with no jobs or a completed, genuinely dead worker', options, async t => {
  const ctx = await context(t);
  assertSuccess(await waitNative(ctx).done);
  await fixture(ctx, 'completed');
  const before = await snapshot(ctx.backend);
  assertSuccess(await waitNative(ctx).done);
  assert.deepEqual(await snapshot(ctx.backend), before);
});

test('terminal-looking state cannot release cleanup before an inherited lease and late config writer', options, async t => {
  const ctx = await context(t);
  // A preceding completed job proves the fence checks every job, not just the first.
  await fixture(ctx, 'completed', 'a-complete');
  await fixture(ctx, 'held', 'z-late');
  const state = JSON.parse(await fs.readFile(path.join(ctx.backend, 'jobs/z-late/state.json'), 'utf8'));
  assert.equal(state.supervisor_state, 'finished');
  assert.equal(state.worker_reaped && state.process_group_terminated, true);
  // Red proof: the old terminal-state-only predicate already permits deletion,
  // even though a real descendant still owns the lease and will write config.
  const oldStateOnlyGuard = state.worker_reaped === true && state.process_group_terminated === true;
  assert.equal(oldStateOnlyGuard, true);
  const marker = path.join(ctx.base, 'helper.blocked');
  const waiter = waitNative(ctx, 5, marker);
  await waitFor(marker, waiter);
  assert.equal(waiter.settled, false);
  assert.equal(await fs.readFile(path.join(ctx.base, 'config.json'), 'utf8'), 'original config\n');
  await fs.writeFile(path.join(ctx.base, 'allow-write'), 'go');
  await waitFor(path.join(ctx.base, 'writer.wrote'), waiter);
  assert.equal(await fs.readFile(path.join(ctx.base, 'config.json'), 'utf8'), 'late config write\n');
  assert.equal(waiter.settled, false, 'cleanup must still await the inherited lease after the late write');
  await fs.writeFile(path.join(ctx.base, 'allow-exit'), 'go');
  assertSuccess(await waiter.done);
  await waitFor(path.join(ctx.base, 'writer.done'));
});

test('cleanup timeout preserves all native evidence and does not kill a lease owner', options, async t => {
  const ctx = await context(t);
  await fixture(ctx, 'held');
  const before = await snapshot(ctx.backend);
  const config = await fs.readFile(path.join(ctx.base, 'config.json'), 'utf8');
  assertRefused(await waitNative(ctx, .15).done);
  assert.deepEqual(await snapshot(ctx.backend), before);
  assert.equal(await fs.readFile(path.join(ctx.base, 'config.json'), 'utf8'), config);
  assert.equal(await exists(path.join(ctx.base, 'writer.done')), false);
  // The same owner can still perform its write after the helper refuses cleanup.
  await fs.writeFile(path.join(ctx.base, 'allow-write'), 'go');
  await waitFor(path.join(ctx.base, 'writer.wrote'));
  assert.equal(await fs.readFile(path.join(ctx.base, 'config.json'), 'utf8'), 'late config write\n');
});

test('a released lease and terminal state do not hide a live supervisor birth identity', options, async t => {
  const ctx = await context(t);
  const owner = await fixture(ctx, 'live');
  assertRefused(await waitNative(ctx, .15).done);
  assert.equal(owner.settled, false);
  await fs.writeFile(path.join(ctx.base, 'allow-exit'), 'go');
  assertSuccess(await owner.done);
  assertSuccess(await waitNative(ctx).done);
});

test('unknown attempted spawns refuse cleanup and preserve missing or malformed state', options, async t => {
  const ctx = await context(t);
  const job = path.join(ctx.backend, 'jobs/uncertain');
  await fs.mkdir(job, {recursive: true});
  await fs.writeFile(path.join(job, 'launch.lock'), '');
  await fs.writeFile(path.join(job, 'spawn_attempt.json'), JSON.stringify({attempt_id: 'unknown'}));
  for (const state of [undefined, '{broken JSON', '{}']) {
    if (state !== undefined) await fs.writeFile(path.join(job, 'state.json'), state);
    const before = await snapshot(ctx.backend);
    assertRefused(await waitNative(ctx, .1).done);
    assert.deepEqual(await snapshot(ctx.backend), before);
  }
});

test('dead PIDs alone cannot replace termination flags or owned birth identities', options, async t => {
  const ctx = await context(t);
  await fixture(ctx, 'completed');
  const stateFile = path.join(ctx.backend, 'jobs/job/state.json');
  const state = JSON.parse(await fs.readFile(stateFile, 'utf8'));
  for (const field of ['worker_reaped', 'process_group_terminated', 'supervisor_identity', 'worker_identity']) {
    const incomplete = {...state};
    delete incomplete[field];
    await fs.writeFile(stateFile, JSON.stringify(incomplete));
    assertRefused(await waitNative(ctx, .1).done);
    assert.deepEqual(JSON.parse(await fs.readFile(stateFile, 'utf8')), incomplete);
  }
  await fs.writeFile(stateFile, JSON.stringify(state));
  assertSuccess(await waitNative(ctx).done);
});

test('a submitted job must retain its expected directory and launch lease', options, async t => {
  const ctx = await context(t);
  assertRefused(await start(ctx, [helper, ctx.backend, '.1', 'submitted']).done);
  assert.equal(await exists(ctx.backend), false, 'the checker must not create missing ownership evidence');
  await fixture(ctx, 'completed', 'submitted');
  assertSuccess(await start(ctx, [helper, ctx.backend, '2', 'submitted']).done);
  await fs.unlink(path.join(ctx.backend, 'jobs/submitted/launch.lock'));
  const before = await snapshot(ctx.backend);
  assertRefused(await start(ctx, [helper, ctx.backend, '.1', 'submitted']).done);
  assert.deepEqual(await snapshot(ctx.backend), before);
});

async function nonNativeHarnessCleanup({serviceClosed = true, status = 'FUNCTIONAL_PASS', remove = async () => {}} = {}) {
  const source = await fs.readFile(path.join(__dirname, 'componentAuthoringHttp.cjs'), 'utf8');
  const start = source.indexOf('}finally{');
  const end = source.lastIndexOf('\n }\n})().catch');
  assert(start !== -1 && end > start, 'Find the real harness finally body; do not replace it with a test copy');
  const body = source.slice(start + '}finally{'.length, end);
  const service = new EventEmitter();
  service.exitCode = serviceClosed ? 0 : null;
  service.signalCode = null;
  const events = [];
  service.kill = signal => { events.push(['service.kill', signal]); };
  const report = {status};
  const process = {env: {}, exitCode: undefined};
  const output = [];
  const tmp = path.join(os.tmpdir(), 'simulated-nonnative-cleanup');
  const run = vm.runInNewContext(`(async () => { ${body}\n})()`, {
    assert, path, __dirname, native: false, python: 'must-not-be-started', service,
    report, process, tmp, log: 'captured service output', setTimeout, clearTimeout,
    cp: {spawn() { assert.fail('Non-native cleanup must never invoke the POSIX-only Python fence'); }},
    fs: {
      async rm(directory, options) {
        events.push(['fs.rm', directory, {...options}]);
        await remove(report);
      },
      async writeFile() { assert.fail('No report-file output was requested in this fixture'); },
    },
    console: {log(text) { output.push(text); }},
  });
  return {run, service, report, process, output, events, tmp};
}

test('non-native harness waits for service close and successful removal before reporting PASS without a Python fence', {timeout: 5000}, async () => {
  let signalRemovalStarted, completeRemoval;
  const removalStarted = new Promise(resolve => { signalRemovalStarted = resolve; });
  const removalAllowed = new Promise(resolve => { completeRemoval = resolve; });
  const h = await nonNativeHarnessCleanup({
    serviceClosed: false,
    async remove(report) {
      assert.equal(report.status, 'FUNCTIONAL_PASS');
      signalRemovalStarted();
      await removalAllowed;
    },
  });
  assert.deepEqual(h.events, [['service.kill', 'SIGTERM']]);
  assert.equal(h.report.status, 'FUNCTIONAL_PASS');
  assert.equal(h.output.length, 0);
  h.service.signalCode = 'SIGTERM';
  h.service.emit('close');
  await removalStarted;
  assert.equal(h.report.status, 'FUNCTIONAL_PASS', 'Deletion is still pending');
  assert.equal(h.output.length, 0, 'No premature PASS report');
  completeRemoval();
  await h.run;
  assert.equal(h.report.status, 'PASS');
  assert.equal(h.report.cleanup, 'service_closed_no_native_jobs');
  assert.equal(h.report.retained_evidence, undefined);
  assert.deepEqual(h.events[1], ['fs.rm', h.tmp, {recursive: true, force: true}]);
  assert.equal(JSON.parse(h.output[0]).status, 'PASS');
});

test('non-native harness reports cleanup failures and retains unsuccessful functional evidence', {timeout: 5000}, async () => {
  const failedRemoval = await nonNativeHarnessCleanup({remove: async () => { throw Error('simulated deletion failed'); }});
  await failedRemoval.run;
  assert.equal(failedRemoval.report.status, 'FAIL');
  assert.match(failedRemoval.report.cleanup_error, /simulated deletion failed/);
  assert.equal(failedRemoval.report.retained_evidence, failedRemoval.tmp);
  assert.equal(failedRemoval.process.exitCode, 1);
  assert.equal(JSON.parse(failedRemoval.output[0]).status, 'FAIL');

  const failedFunction = await nonNativeHarnessCleanup({status: 'FAIL'});
  await failedFunction.run;
  assert.equal(failedFunction.report.status, 'FAIL');
  assert.equal(failedFunction.report.retained_evidence, failedFunction.tmp);
  assert.equal(failedFunction.events.length, 0, 'Never remove evidence after a functional failure');
  assert.equal(JSON.parse(failedFunction.output[0]).status, 'FAIL');
});
