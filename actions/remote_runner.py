"""Remote execution helpers for offloading heavy work to an SSH-accessible shell.

The remote backend is opt-in. If `remote_execution.enabled` is not set in
config/api_keys.json, callers should continue to run locally.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"


@dataclass(frozen=True)
class RemoteExecutionConfig:
    enabled: bool = False
    host: str = ""
    user: str = ""
    port: int = 22
    identity_file: str = ""
    remote_root: str = "/tmp/jeeves"
    provider: str = "ssh"  # "ssh" or "codespace"
    codespace: str = ""    # codespace name when provider == "codespace"
    codespace_workdir: str = ""  # workdir inside codespace (e.g. /workspaces/repo)

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def get_remote_execution_config() -> RemoteExecutionConfig:
    data = _load_config().get("remote_execution", {}) or {}
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = {}

    return RemoteExecutionConfig(
        enabled=bool(data.get("enabled", False)),
        host=str(data.get("host", "")).strip(),
        user=str(data.get("user", "")).strip(),
        port=int(data.get("port", 22) or 22),
        identity_file=str(data.get("identity_file", "")).strip(),
        remote_root=str(data.get("remote_root", "/tmp/jeeves")).strip() or "/tmp/jeeves",
        provider=str(data.get("provider", "ssh")).strip() or "ssh",
        codespace=str(data.get("codespace", "")).strip(),
        codespace_workdir=str(data.get("codespace_workdir", "")).strip(),
    )


def remote_execution_enabled() -> bool:
    cfg = get_remote_execution_config()
    return cfg.enabled and bool(cfg.host)


def _ssh_base_args(cfg: RemoteExecutionConfig) -> list[str]:
    args = ["ssh", "-p", str(cfg.port), "-o", "BatchMode=yes"]
    if cfg.identity_file:
        args += ["-i", cfg.identity_file]
    if cfg.user:
        args.append(f"{cfg.user}@{cfg.host}")
    else:
        args.append(cfg.host)
    return args


def _scp_base_args(cfg: RemoteExecutionConfig) -> list[str]:
    args = ["scp", "-P", str(cfg.port), "-o", "BatchMode=yes"]
    if cfg.identity_file:
        args += ["-i", cfg.identity_file]
    return args


def _run_local(command: list[str], cwd: str | None = None, input_text: str | None = None, timeout: int | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        timeout=timeout,
    )


# SSH/scp handshake calls carry a hard timeout: an unreachable host can
# otherwise block the whole action for minutes (TCP connect default).
_SSH_IO_TIMEOUT = 30


def run_python_text(text: str, timeout: int = 120) -> str:
    cfg = get_remote_execution_config()
    if not remote_execution_enabled():
        raise RuntimeError("Remote execution is not enabled.")
    if cfg.provider == "codespace":
        if not shutil.which("gh"):
            raise RuntimeError("The GitHub CLI ('gh') is required for Codespaces execution.")
        gh_cmd = ["gh", "codespace", "exec", "--codespace", cfg.codespace, "--", "bash", "-lc", f"cd {cfg.codespace_workdir or '.'} && python3 -"]
        result = _run_local(gh_cmd, cwd=str(BASE_DIR), input_text=text, timeout=timeout)
    else:
        remote_cmd = "python3 - <<'PY'\nimport sys\ncode = sys.stdin.read()\nexec(compile(code, '<jeeves-remote>', 'exec'), {'__name__': '__main__'})\nPY"
        result = _run_local(_ssh_base_args(cfg) + [remote_cmd], cwd=str(BASE_DIR), input_text=text, timeout=timeout)
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    if result.returncode != 0:
        raise RuntimeError(stderr or stdout or f"Remote execution failed with exit code {result.returncode}")
    return stdout or "Task completed successfully."


def run_script_file(script_path: Path, args: list[str] | None = None, timeout: int = 120) -> str:
    cfg = get_remote_execution_config()
    if not remote_execution_enabled():
        raise RuntimeError("Remote execution is not enabled.")

    args = list(args or [])
    if not shutil.which("scp") or not shutil.which("ssh"):
        raise RuntimeError("ssh/scp are required for remote execution.")

    if cfg.provider == "codespace":
        if not shutil.which("gh"):
            raise RuntimeError("The GitHub CLI ('gh') is required for Codespaces execution.")
        # Uploading file contents into remote codespace is non-trivial; assume the
        # repository is already available inside the codespace. Run the script by
        # changing to the provided workdir and executing it.
        cmd = ["gh", "codespace", "exec", "--codespace", cfg.codespace, "--", "bash", "-lc", f"cd {cfg.codespace_workdir or '.'} && python3 {script_path.name} {' '.join(args)}"]
        result = _run_local(cmd, cwd=str(BASE_DIR), timeout=timeout)
    else:
        with tempfile.TemporaryDirectory(prefix="jeeves_remote_") as tmp_dir:
            temp_root = Path(tmp_dir) / script_path.parent.name
            shutil.copytree(script_path.parent, temp_root, dirs_exist_ok=True)

            remote_dir = f"{cfg.remote_root.rstrip('/')}/{script_path.parent.name}"
            subprocess.run(_ssh_base_args(cfg) + [f"mkdir -p {cfg.remote_root!s}"], capture_output=True, text=True, timeout=_SSH_IO_TIMEOUT)

            # Upload each file in the directory tree. This keeps the implementation simple
            # and avoids depending on rsync on the remote host.
            for file_path in temp_root.rglob("*"):
                if not file_path.is_file():
                    continue
                rel = file_path.relative_to(temp_root)
                remote_file = f"{remote_dir}/{rel.as_posix()}"
                subprocess.run(_ssh_base_args(cfg) + [f"mkdir -p $(dirname {remote_file!r})"], capture_output=True, text=True, timeout=_SSH_IO_TIMEOUT)
                scp_cmd = _scp_base_args(cfg) + [str(file_path), f"{cfg.target}:{remote_file}"]
                upload = subprocess.run(scp_cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=_SSH_IO_TIMEOUT)
                if upload.returncode != 0:
                    raise RuntimeError(upload.stderr.strip() or upload.stdout.strip() or f"Failed to upload {rel}")

            remote_script = f"{remote_dir}/{script_path.name}"
            remote_run = f"cd {remote_dir} && python3 {script_path.name} {' '.join(args)}"
            result = _run_local(_ssh_base_args(cfg) + [remote_run], cwd=str(BASE_DIR), timeout=timeout)
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        if result.returncode != 0:
            raise RuntimeError(stderr or stdout or f"Remote execution failed with exit code {result.returncode}")
        return stdout or "Executed remotely with no output."


def remote_run_command(command: str, project_dir: Path, timeout: int = 120) -> str:
    cfg = get_remote_execution_config()
    if not remote_execution_enabled():
        raise RuntimeError("Remote execution is not enabled.")

    if not shutil.which("ssh"):
        raise RuntimeError("ssh is required for remote execution.")

    remote_dir = f"{cfg.remote_root.rstrip('/')}/{project_dir.name}"
    mkdir_result = _run_local(_ssh_base_args(cfg) + [f"mkdir -p {remote_dir}"], cwd=str(BASE_DIR), timeout=timeout)
    if mkdir_result.returncode != 0:
        raise RuntimeError(mkdir_result.stderr.strip() or mkdir_result.stdout.strip() or "Could not prepare remote directory.")

    # Best-effort directory sync using SCP for each file; keeps working for code projects
    # without requiring rsync on the host.
    with tempfile.TemporaryDirectory(prefix="jeeves_sync_") as tmp_dir:
        temp_root = Path(tmp_dir) / project_dir.name
        temp_root.mkdir(parents=True, exist_ok=True)
        for src in project_dir.rglob("*"):
            rel = src.relative_to(project_dir)
            dest = temp_root / rel
            if src.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(src.read_bytes())

        for src in temp_root.rglob("*"):
            if not src.is_file():
                continue
            rel = src.relative_to(temp_root)
            remote_file = f"{remote_dir}/{rel.as_posix()}"
            subprocess.run(_ssh_base_args(cfg) + [f"mkdir -p $(dirname {remote_file!r})"], capture_output=True, text=True, timeout=_SSH_IO_TIMEOUT)
            upload = subprocess.run(
                _scp_base_args(cfg) + [str(src), f"{cfg.target}:{remote_file}"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_SSH_IO_TIMEOUT,
            )
            if upload.returncode != 0:
                raise RuntimeError(upload.stderr.strip() or upload.stdout.strip() or f"Failed to upload {rel}")

    remote_cmd = f"cd {remote_dir} && {command}"
    result = _run_local(_ssh_base_args(cfg) + [remote_cmd], cwd=str(BASE_DIR), timeout=timeout)
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    if result.returncode != 0:
        raise RuntimeError(stderr or stdout or f"Remote command failed with exit code {result.returncode}")
    return stdout or "Ran remotely with no output."