"""Subprocess utilities with proper process-group cleanup on timeout.

The standard ``subprocess.run`` only sends SIGTERM to the top-level process
when a timeout expires, leaving child processes orphaned.  The functions here
run each command in its own process group (``start_new_session=True``) and
send SIGKILL to the entire group on timeout or interrupt.
"""

import os
import shlex
import signal
import subprocess


def safe_killpg(pid: int, sig: int):
  """Send *sig* to the process group *pid*, ignoring already-exited groups."""
  try:
    os.killpg(pid, sig)
  except ProcessLookupError:
    pass  # Ignore if there is no such process


def spawn_process(
  cmd, stdout, stderr, timeout, **kwargs
) -> subprocess.CompletedProcess:
  """Run *cmd* in a new process group and wait up to *timeout* seconds.

  On timeout or interrupt the **entire** process group is killed via SIGKILL,
  ensuring no child processes are leaked.

  Returns a :class:`subprocess.CompletedProcess` with captured output.
  """
  with subprocess.Popen(
    cmd, stdout=stdout, stderr=stderr, start_new_session=True, **kwargs
  ) as proc:
    try:
      output, err_msg = proc.communicate(timeout=timeout)
    except:  # Including TimeoutExpired, KeyboardInterrupt, communicate handled that.
      safe_killpg(os.getpgid(proc.pid), signal.SIGKILL)
      # We don't call proc.wait() as .__exit__ does that for us.
      raise
    ecode = proc.poll()
  return subprocess.CompletedProcess(proc.args, ecode, output, err_msg)


def check_call(cmd: str, timeout: int = 60, **kwargs):
  """Run *cmd* (shell string) and raise on non-zero exit code."""
  proc = spawn_process(
    shlex.split(cmd),
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    timeout=timeout,
    **kwargs,
  )
  proc.check_returncode()


def getoutput(cmd: str, timeout: int = 60, check=True, **kwargs) -> bytes:
  """Run *cmd* and return its combined stdout+stderr as bytes.

  Raises :class:`subprocess.CalledProcessError` if *check* is True and
  the command exits with a non-zero code.
  """
  proc = spawn_process(
    shlex.split(cmd),
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    timeout=timeout,
    **kwargs,
  )
  if check:
    proc.check_returncode()
  return proc.stdout


def redirect_stdout(
  cmd: str, stdout: str, timeout: int = 60, check=True, **kwargs
) -> bytes:
  """Run *cmd*, writing stdout to the file at *stdout* path.

  Returns the captured stderr as bytes.
  """
  with open(stdout, "w") as fou:
    proc = spawn_process(
      shlex.split(cmd),
      stdout=fou,
      stderr=subprocess.PIPE,
      timeout=timeout,
      **kwargs,
    )
  if check:
    proc.check_returncode()
  return proc.stderr


def check_output(cmd: str, timeout: int = 60, **kwargs) -> bytes:
  """Run *cmd* and return stdout+stderr; raise on non-zero exit."""
  return getoutput(cmd, timeout, check=True, **kwargs)
