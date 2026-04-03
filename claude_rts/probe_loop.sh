#!/bin/bash
# Runs inside the utility container as a background loop.
# Probes all profiles in /profiles/ and writes JSON results to /tmp/claude-usage.json

INTERVAL="${PROBE_INTERVAL:-60}"
OUTPUT="/tmp/claude-usage.json"

while true; do
  echo "[probe] Starting probe cycle at $(date)" >&2

  # Collect results into a temp file using Python
  python3 -c "
import subprocess, json, os, glob

profiles = sorted([os.path.basename(d) for d in glob.glob('/profiles/*/') if os.path.isdir(d)])
results = []

for name in profiles:
    claude_dir = f'/profiles/{name}'
    # Check if .claude subdir exists
    if os.path.isdir(f'{claude_dir}/.claude'):
        claude_dir = f'{claude_dir}/.claude'

    print(f'[probe] Probing {name} ({claude_dir})...', flush=True)
    try:
        proc = subprocess.run(
            ['script', '-qc', f'timeout 45 claude-usage --claude-dir {claude_dir} --json', '/dev/null'],
            capture_output=True, text=True, timeout=50
        )
        stdout = proc.stdout.strip().replace('\r', '')
        # Extract JSON from output (may have debug lines before it)
        json_start = stdout.find('{')
        json_end = stdout.rfind('}')
        if json_start >= 0 and json_end > json_start:
            entry = json.loads(stdout[json_start:json_end+1])
            entry['profile'] = name
            entry['cached'] = False
            results.append(entry)
            print(f'[probe] {name}: OK', flush=True)
        else:
            results.append({'profile': name, 'error': 'no JSON in output', 'cached': False})
            print(f'[probe] {name}: no JSON found', flush=True)
    except Exception as e:
        results.append({'profile': name, 'error': str(e), 'cached': False})
        print(f'[probe] {name}: {e}', flush=True)

with open('$OUTPUT', 'w') as f:
    json.dump(results, f, indent=2)
print(f'[probe] Written {len(results)} profiles to $OUTPUT', flush=True)
" 2>&1

  echo "[probe] Cycle complete, sleeping ${INTERVAL}s" >&2
  sleep "$INTERVAL"
done
