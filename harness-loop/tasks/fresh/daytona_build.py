"""Run one `swegen create` inside a throwaway Daytona sandbox; pull back only the task.

  daytona_build.py --repo OWNER/NAME --pr N --out DIR [--cc-timeout 3200]

SWE-gen's Claude Code session runs with permission checks off and reads untrusted
repo and issue text, so it never runs on this machine. Per task this script:
1. creates a sandbox (Debian + docker + the pinned SWE-gen from requirements.txt), with a
   wall-clock TTL as a backstop;
2. uploads only swegen_shim.py, and passes only SANDBOX_ENV (model routing, the
   Bedrock key, the Gemini key) to the SWE-gen process. DAYTONA_API_KEY stays here;
   SWE-gen's own Harbor validation uses the sandbox's docker;
3. runs `swegen_shim.py create`, polling until it ends;
4. downloads the log and, if SWE-gen passed (its nop=0 / oracle=1), the task folder as
   a tarball, extracted with tarfile's "data" filter (the contents are untrusted);
5. deletes the sandbox, also on failure or Ctrl-C.
Exit code is SWE-gen's. Prints "daytona_seconds: S" for the cost tally.
"""
import argparse
import io
import os
import shlex
import sys
import tarfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SHIM = HERE / "swegen_shim.py"
REQUIREMENTS = HERE / "requirements.txt"

# The only variables the sandboxed SWE-gen process gets.
SANDBOX_ENV = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_REGION",
               "OPENAI_API_KEY", "OPENAI_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "GITHUB_TOKEN",
               "ANTHROPIC_SMALL_FAST_MODEL")
SANDBOX_PREFIXES = ("SWEGEN_", "ANTHROPIC_DEFAULT_")
CPUS, MEMORY_GB, DISK_GB = 4, 8, 10  # Daytona's per-sandbox caps
POLL_SECONDS = 30


def sandbox_env(env):
    return {k: v for k, v in env.items()
            if v and (k in SANDBOX_ENV or k.startswith(SANDBOX_PREFIXES))}


def image():
    from daytona import Image
    return (Image.debian_slim("3.12")
            .run_commands("apt-get update && apt-get install -y --no-install-recommends "
                          "git docker.io ca-certificates && rm -rf /var/lib/apt/lists/*")
            .pip_install_from_requirements(str(REQUIREMENTS)))


def run(sb, cmd, timeout=600):
    r = sb.process.exec(cmd, timeout=timeout)
    if r.exit_code != 0:
        raise RuntimeError(f"sandbox command failed ({r.exit_code}): {cmd}\n{r.result[-2000:]}")
    return r.result


def build(sb, repo, pr, cc_timeout, env, out):
    run(sb, "mkdir -p /work")
    sb.fs.upload_file(SHIM.read_bytes(), "/work/swegen_shim.py")
    run(sb, "(dockerd >/var/log/dockerd.log 2>&1 &) ; for i in $(seq 60); do "
            "docker info >/dev/null 2>&1 && exit 0; sleep 2; done; tail -20 /var/log/dockerd.log; exit 1",
        timeout=180)
    cmd = shlex.join(["python3", "/work/swegen_shim.py", "create", "--repo", repo, "--pr", str(int(pr)),
                      "--output", "/work/out", "--state-dir", "/work/.swegen",
                      "--cc-timeout", str(int(cc_timeout))])
    sb.fs.upload_file(f"cd /work\n{cmd} >/work/log 2>&1\necho $? >/work/rc\n".encode(), "/work/run.sh")
    # The process env is given here only; nothing is written to the sandbox's env.
    sb.process.exec("nohup sh /work/run.sh >/dev/null 2>&1 &", env=env, timeout=30)
    deadline = time.time() + cc_timeout + 3600
    rc = None
    while time.time() < deadline:
        time.sleep(POLL_SECONDS)
        r = sb.process.exec("cat /work/rc 2>/dev/null", timeout=30)
        if r.result.strip():
            rc = int(r.result.strip())
            break
    out.mkdir(parents=True, exist_ok=True)
    log = sb.fs.download_file("/work/log") or b""
    (out.parent / f"{out.name}.swegen.log").write_bytes(log)
    for line in log.decode(errors="replace").splitlines():  # session costs, for build.sh
        if "total_cost_usd" in line:
            print(line, file=sys.stderr)
    if rc is None:
        print("daytona_build: SWE-gen did not finish before the deadline", file=sys.stderr)
        return 124
    if rc == 0:
        run(sb, "tar -C /work/out -czf /work/out.tgz .")
        data = sb.fs.download_file("/work/out.tgz")
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            tar.extractall(out, filter="data")  # untrusted: no links out, no absolute paths
    return rc


def main(argv=None, env=os.environ):
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pr", type=int, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--cc-timeout", type=int, default=3200)
    a = ap.parse_args(argv)
    from daytona import CreateSandboxFromImageParams, Daytona, DaytonaConfig, Resources

    daytona = Daytona(DaytonaConfig(api_key=env["DAYTONA_API_KEY"]))
    start = time.time()
    sb = None
    try:
        sb = daytona.create(CreateSandboxFromImageParams(
            image=image(), resources=Resources(cpu=CPUS, memory=MEMORY_GB, disk=DISK_GB),
            ttl_minutes=a.cc_timeout // 60 + 90, labels={"purpose": "swegen-create"}),
            timeout=1800)
        return build(sb, a.repo, a.pr, a.cc_timeout, sandbox_env(env), a.out)
    finally:
        if sb is not None:
            daytona.delete(sb)
        print(f"daytona_seconds: {round(time.time() - start)}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
