"""Host-side 12h QA supervisor for an explicitly named isolated Docker container."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time

parser=argparse.ArgumentParser()
parser.add_argument('--container',required=True)
parser.add_argument('--output',required=True)
parser.add_argument('--hours',type=float,default=12)
args=parser.parse_args()
if not args.container.startswith('mediktest-reh2-release-soak-') or args.hours<12:
    raise SystemExit('Dedicated soak container and >=12h required')
docker='/Applications/Docker.app/Contents/Resources/bin/docker'
out=Path(args.output).resolve()
if out.exists(): raise SystemExit('New status path required')
out.parent.mkdir(parents=True,exist_ok=True)
state=dict(status='preparing',container=args.container,cycles=0,started_at=time.time(),hours=args.hours)
inputs=[Path(__file__).resolve(),Path(__file__).with_name('reh2_offline_cycle.py').resolve(),out.parent/'fixtures.json']
def input_hashes():
    return {str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in inputs}
state['qa_input_hashes']=input_hashes()
def save():
    tmp=out.with_suffix('.tmp');tmp.write_text(json.dumps(state,ensure_ascii=False,indent=2));tmp.replace(out)
def command(*parts,timeout=180):
    return subprocess.run([docker,*parts],capture_output=True,text=True,timeout=timeout)
def cycle():
    if input_hashes()!=state['qa_input_hashes']: raise RuntimeError('QA inputs changed: repeat soak')
    result=command('exec',args.container,'python','/qa/reh2_offline_cycle.py','cycle')
    if result.returncode: raise RuntimeError(result.stderr[-3000:] or result.stdout[-3000:])
    state['last_result']=json.loads(result.stdout.strip().splitlines()[-1]);state['cycles']+=1
    state['updated_at']=time.time();save()
try:
    image=command('inspect','--format','{{.Image}}',args.container)
    if image.returncode: raise RuntimeError(image.stderr)
    state['image_id']=image.stdout.strip()
    cycle()
    # A different prefix creates fresh partial receipts, even after baseline QA.
    result=command('exec',args.container,'python','/qa/reh2_offline_cycle.py','interrupt')
    if result.returncode!=23: raise RuntimeError('Expected injected exit 23: '+result.stderr)
    restarted=command('restart',args.container)
    if restarted.returncode: raise RuntimeError(restarted.stderr)
    for _ in range(60):
        ready=command('exec',args.container,'python','-c',
            "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8765/api/health',timeout=3)")
        if ready.returncode==0: break
        time.sleep(1)
    else: raise RuntimeError('Container failed to restart')
    recovered=command('exec',args.container,'python','/qa/reh2_offline_cycle.py','recover')
    if recovered.returncode: raise RuntimeError('Recovery failed: '+recovered.stderr)
    cycle()
    state.update(status='running',restart_verified=True,soak_started_at=time.time());save()
    started=time.monotonic()
    while time.monotonic()-started<args.hours*3600:
        before=time.monotonic();time.sleep(60)
        if time.monotonic()-before>120: raise RuntimeError('Host sleep or scheduler gap: repeat soak')
        cycle()
        current=command('inspect','--format','{{.Image}}',args.container)
        if current.stdout.strip()!=state['image_id']: raise RuntimeError('Container image changed')
    state.update(status='passed',finished_at=time.time(),elapsed_seconds=time.monotonic()-started);save()
except Exception as exc:
    state.update(status='failed',error=str(exc),finished_at=time.time());save();raise
