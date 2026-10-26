"""Interactive creator contract; no remote services or containers are started."""
import errno
import os
import pty
import select
import shutil
import subprocess
import time
from pathlib import Path

import pytest

CONFIG = Path(__file__).resolve().parents[1] / "config.sh"
EXAMPLE = "#required: token\n#secret\n#create-with: creator.py\nTOKEN=\n"
HELPER = '''import argparse
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument('--env-file')
p.add_argument('--config-file')
p.add_argument('--key')
p.add_argument('mode')
a = p.parse_args()
root = Path(a.env_file).parent
with (root / 'calls').open('a') as f:
    f.write(a.mode + ' ' + a.key + '\\n')
if a.mode == 'check':
    if (root / 'unavailable').exists():
        raise SystemExit(1)
    print('Enter existing bearer|Create new virtual key in LiteLLM')
else:
    if (root / 'failure').exists():
        raise SystemExit(1)
    print('generated-test-bearer')
'''


def setup_files(path, example=EXAMPLE, existing=""):
    shutil.copyfile(CONFIG, path / "config.sh")
    (path / "env.example").write_text(example)
    (path / "creator.py").write_text(HELPER)
    (path / ".env").write_text(existing)


def interactive(path, steps):
    master, slave = pty.openpty()
    process = subprocess.Popen(["bash", "config.sh", "--no-container"], cwd=path,
        stdin=slave, stdout=slave, stderr=slave, close_fds=True)
    os.close(slave)
    output = bytearray()
    remaining = list(steps)
    deadline = time.monotonic() + 20
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], .05)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
                    break
                if not chunk:
                    break
                output.extend(chunk)
                if remaining and remaining[0][0].encode() in output:
                    _, answer = remaining.pop(0)
                    os.write(master, (answer + "\n").encode())
            elif process.poll() is not None:
                break
        else:
            pytest.fail("Interactive setup timed out: " + output.decode())
        assert not remaining, output.decode()
        return process.wait(timeout=3), output.decode()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)


@pytest.mark.parametrize("choice", ["1", "2"])
def test_manual_or_explicit_creation(tmp_path, choice):
    setup_files(tmp_path)
    steps = [("Choose [1/2]", choice)]
    if choice == "1":
        steps.append(("TOKEN :", "manual-test-bearer"))
    status, output = interactive(tmp_path, steps)
    assert status == 0, output
    expected = "generated-test-bearer" if choice == "2" else "manual-test-bearer"
    assert "TOKEN=" + expected in (tmp_path / ".env").read_text().splitlines()
    assert expected not in output
    assert (tmp_path / "calls").read_text() == "check TOKEN\n" + ("create TOKEN\n" if choice == "2" else "")


def test_existing_and_noninteractive_never_call_helper(tmp_path):
    setup_files(tmp_path, existing="TOKEN=existing-bearer\n")
    assert interactive(tmp_path, [])[0] == 0
    assert not (tmp_path / "calls").exists()
    (tmp_path / ".env").write_text("")
    result = subprocess.run(["bash", "config.sh", "--no-container"], cwd=tmp_path,
        input="manual-test-bearer\n", capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (tmp_path / "calls").exists()


def test_unavailable_check_keeps_manual_input(tmp_path):
    setup_files(tmp_path)
    (tmp_path / "unavailable").touch()
    status, output = interactive(tmp_path, [("TOKEN :", "manual-test-bearer")])
    assert status == 0, output
    assert "Create new virtual key" not in output
    assert (tmp_path / "calls").read_text() == "check TOKEN\n"


def test_failed_creation_stops_without_saving(tmp_path):
    setup_files(tmp_path)
    (tmp_path / "failure").touch()
    assert interactive(tmp_path, [("Choose [1/2]", "2")])[0] != 0
    assert "TOKEN=" not in (tmp_path / ".env").read_text()


def test_repeat_new_targets_next_pair_and_skip_calls_nothing(tmp_path):
    example = "#repeat-group: USER suffix02 TOKEN\n" + EXAMPLE
    setup_files(tmp_path, example, "TOKEN=existing-bearer\n")
    assert interactive(tmp_path, [("USER [skip/new]", "skip")])[0] == 0
    assert not (tmp_path / "calls").exists()
    status, output = interactive(tmp_path, [("USER [skip/new]", "new"), ("Choose [1/2]", "2")])
    assert status == 0, output
    assert (tmp_path / "calls").read_text() == "check TOKEN_02\ncreate TOKEN_02\n"
    assignments = [line for line in (tmp_path / ".env").read_text().splitlines() if line.startswith("TOKEN")]
    assert assignments == ["TOKEN=existing-bearer", "TOKEN_02=generated-test-bearer"]


def test_optional_orchestrator_new_skip_and_openssl(tmp_path):
    example = '''#repeat-group: ORCHESTRATOR suffix02 NAME BEARER KEY_NAMES
#repeat-optional-group: ORCHESTRATOR
#required: name
NAME=orchestrator
#required: login
#secret
BEARER=example: openssl rand -hex 32
#required: target names
KEY_NAMES=
'''
    setup_files(tmp_path, example)
    assert interactive(tmp_path, [('ORCHESTRATOR [skip/new]', 'skip')])[0] == 0
    assert 'BEARER=' not in (tmp_path / '.env').read_text()
    status, output = interactive(tmp_path, [('ORCHESTRATOR [skip/new]', 'new'),
        ('NAME', ''), ('Choose [1/2]', '2'), ('KEY_NAMES', 'alice,bob')])
    assert status == 0, output
    text = (tmp_path / '.env').read_text()
    bearer = next(line.split('=', 1)[1] for line in text.splitlines() if line.startswith('BEARER='))
    assert len(bearer) == 64 and all(c in '0123456789abcdef' for c in bearer)
    assert bearer not in output
    assert 'KEY_NAMES=alice,bob' in text


def test_namespaced_container_metadata_and_bool_ports(tmp_path):
    setup_files(tmp_path, '')
    root = CONFIG.parent
    shutil.copyfile(root / 'container.example', tmp_path / 'container.example')
    (tmp_path / 'container.conf').write_text('''LITELLM_MCP_SWITCHER_CONTAINER_NAME=custom-switcher
LITELLM_MCP_SWITCHER_IMAGE=example.test/switcher:latest
LITELLM_MCP_SWITCHER_WEBUI_PUBLISH_PORT=28080
LITELLM_MCP_SWITCHER_MCP_PUBLISH_PORT=
LITELLM_MCP_SWITCHER_ADDITIONAL_LINE=ReadOnly=true
''')
    (tmp_path / 'config.conf').write_text('LITELLM_MCP_SWITCHER_WEBUI_ENABLED=1\n')
    result = subprocess.run(['bash', 'config.sh', '--render-container'], cwd=tmp_path,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    unit = (tmp_path / 'custom-switcher.container').read_text()
    assert 'Image=example.test/switcher:latest' in unit
    assert 'ContainerName=custom-switcher' in unit
    assert any(line.startswith('PublishPort=') and line.endswith(':28080:8080') for line in unit.splitlines()), unit
    assert ':8000' not in unit
    assert 'ReadOnly=true' in unit
