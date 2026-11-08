"""Exercise a trusted installed module, not a sys.modules-only test double."""

import json
import os
from pathlib import Path
import subprocess
import venv

import pytest

import boundver
from tests._repo_fixtures import commit_all, init_git_repo


@pytest.fixture(scope="module")
def provider_interpreter(tmp_path_factory):
    environment = tmp_path_factory.mktemp("installed-provider-runtime")
    builder = venv.EnvBuilder(with_pip=False)
    builder.create(environment)
    context = builder.ensure_directories(environment)
    interpreter = Path(context.env_exe)
    location = subprocess.run(
        [str(interpreter), "-I", "-c", "import sysconfig;print(sysconfig.get_path('purelib'))"],
        check=True, capture_output=True, text=True, timeout=20,
    )
    site = Path(location.stdout.strip())
    site.relative_to(environment)  # Refuse an unexpected non-private install target.
    # Make this development artifact available without pip/network access or
    # changing any existing environment. The provider itself lives in this
    # disposable interpreter's installed site-packages, outside the checkout.
    runtime_root = Path(boundver.__file__).parent.parent
    (site / "boundver-fixture-runtime.pth").write_text(str(runtime_root) + "\n", encoding="utf-8")
    (site / "boundver_installed_provider_fixture.py").write_text(
        "import json,os,pathlib\n"
        "from boundver.providers import ResolvedBoundary\n"
        "marker=os.environ.get('BOUNDVER_PROVIDER_TEST_MARKER')\n"
        "if marker: pathlib.Path(marker).write_text(json.dumps([os.getpid(),int(os.environ['BOUNDVER_PROVIDER_TEST_CONTROLLER_PID'])]))\n"
        "class FixtureProvider:\n"
        "    name='custom.installed_fixture'\n"
        "    version='1'\n"
        "    def resolve(self,ctx):\n"
        "        return ResolvedBoundary(entries=[('contract',b'stable')])\n",
        encoding="utf-8",
    )
    return interpreter


def invoke(interpreter, repository, marker, *arguments):
    environment = {key: value for key, value in os.environ.items() if key != "BOUNDVER_ALLOW_CUSTOM_PROVIDERS"}
    environment["BOUNDVER_PROVIDER_TEST_MARKER"] = str(marker)
    controller = (
        "import os,runpy,sys;"
        "os.environ['BOUNDVER_PROVIDER_TEST_CONTROLLER_PID']=str(os.getpid());"
        "sys.argv=['boundver',*sys.argv[1:]];"
        "runpy.run_module('boundver',run_name='__main__')"
    )
    process = subprocess.Popen(
        [str(interpreter), "-I", "-c", controller, *arguments],
        cwd=repository, env=environment,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=90)
        return process.pid, process.returncode, stdout, stderr
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def test_installed_provider_requires_authorization_and_matches_timed_worker(provider_interpreter, tmp_path):
    init_git_repo(tmp_path)
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "contract.txt").write_text("contract\n", encoding="utf-8")
    config = {
        "project": "installed-provider",
        "providers": [{"module": "boundver_installed_provider_fixture", "class": "FixtureProvider",
                       "name": "custom.installed_fixture"}],
        "components": {"svc": {"path": "svc", "boundary": {"provider": "custom.installed_fixture"}}},
    }
    (tmp_path / "boundary.config.json").write_text(json.dumps(config), encoding="utf-8")
    commit_all(tmp_path)
    marker = tmp_path / "import-marker"
    arguments = ["generate", "--dry-run", "--format", "json", "--quiet"]
    for extra in ([], ["--timeout", "60"]):
        _, status, stdout, _ = invoke(provider_interpreter, tmp_path, marker, *arguments, *extra)
        assert status == 2 and not stdout
        assert not marker.exists(), "unauthorized provider was imported"
    _, status, ordinary, stderr = invoke(
        provider_interpreter, tmp_path, marker, *arguments, "--allow-custom-providers",
    )
    assert status == 0, stderr
    provider_pid, controller_pid = json.loads(marker.read_text())
    assert provider_pid == controller_pid
    marker.unlink()
    _, status, timed, stderr = invoke(
        provider_interpreter, tmp_path, marker, *arguments, "--allow-custom-providers", "--timeout", "60",
    )
    assert status == 0, stderr
    provider_pid, controller_pid = json.loads(marker.read_text())
    assert provider_pid != controller_pid, "provider ran in the controller"
    assert timed == ordinary
    assert "not a sandbox" in stderr
    assert not (tmp_path / "boundary.lock.json").exists()
