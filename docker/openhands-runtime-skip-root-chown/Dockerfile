FROM ghcr.io/all-hands-ai/runtime:0.33-nikolaik

RUN python3 - <<'PY'
from pathlib import Path

path = Path("/openhands/code/openhands/runtime/utils/runtime_init.py")
text = path.read_text()
old = """    command = f'chown -R {username}:root {initial_cwd}'
    output = subprocess.run(command, shell=True, capture_output=True)
    out_str += output.stdout.decode()

"""
new = """    if username != 'root':
        command = f'chown -R {username}:root {initial_cwd}'
        output = subprocess.run(command, shell=True, capture_output=True)
        out_str += output.stdout.decode()

"""
if old not in text:
    raise SystemExit("Expected chown block not found in runtime_init.py")
path.write_text(text.replace(old, new))
PY
