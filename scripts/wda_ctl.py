#!/usr/bin/env python3
"""
Deploy and control WebDriverAgent on a jailbroken iOS device (rootless
palera1n/Dopamine-style, procursus filesystem) purely over SSH -- no Xcode
team/provisioning, no USB, no tidevice/DTX/testmanagerd handshake at runtime.

Why this works at all: WDA's -Runner app is an XCTest "runner" host. Normally
launching one requires Apple's private instruments/testmanagerd protocol
(what appium's own xcodebuild-driven flow, and tidevice, use) because a
regular device can't fork/exec an arbitrary signed app outside that path. A
jailbroken device's root SSH access bypasses that launch confinement
entirely: as long as every Mach-O in the bundle carries at least an ad-hoc
signature (dyld refuses to map a *completely* unsigned binary even with AMFI
patched -- confirmed live), the -Runner binary can just be exec'd directly
from a shell with the same environment variables + XCTestConfiguration file
Apple's own tooling would have set up, and XCTest's bootstrap runs the WDA
test method and starts its HTTP server on its own -- no IDE/testmanagerd
session needs to exist for that part, as long as the config's
reportResultsToIDE is set to False (there's no IDE to report to).

Usage:
    python3 wda_ctl.py deploy  --host <ip> [--username mobile] [--port 8100]
    python3 wda_ctl.py start   --host <ip> [--port 8100]
    python3 wda_ctl.py stop    --host <ip>
    python3 wda_ctl.py status  --host <ip> [--port 8100]
    python3 wda_ctl.py logs    --host <ip>
    python3 wda_ctl.py install-daemon   --host <ip> [--port 8100]  # survive reboots
    python3 wda_ctl.py uninstall-daemon --host <ip>

SSH password is read from the WDA_SSH_PASSWORD env var if set, else prompted.
"""
import argparse
import getpass
import os
import posixpath
import sys
import time
import uuid

import paramiko
from tidevice import bplist

REMOTE_DIR_DEFAULT = "/var/mobile/rbserver"
LOCAL_APP_DEFAULT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "build/wda/Build/Products/Debug-iphoneos/rbserver.app",
)
DAEMON_LABEL = "com.rbserver.webdriveragent"

# This device's minimal shell has no pgrep/pkill (procursus doesn't ship them
# by default) -- confirmed live: `pkill -f ...` silently no-ops with
# "command not found" (exit 127), which a bare `|| true`/`&& echo` swallowed
# and made a still-running old instance look successfully killed. `ps`+`awk`+
# `kill` are always present. Also avoids relying on xargs' empty-input exit
# code (BSD xargs skips the command entirely on empty stdin, which would make
# a plain `... | xargs kill && echo KILLED` print KILLED even when nothing
# was running).
_Q = "'\"'\"'"  # close single-quote, literal double-quoted single-quote, reopen single-quote
_KILL_WDA_CMD = (
    "PIDS=$(ps ax -o pid,command | grep rbserver.app/rbserver | grep -v grep | awk "
    + _Q + "{print $1}" + _Q + "); "
    'if [ -n "$PIDS" ]; then kill -9 $PIDS; echo KILLED; else echo NOT_RUNNING; fi'
)


def connect(host, username, password):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(hostname=host, port=22, username=username, password=password, timeout=10)
    return ssh


def run(ssh, cmd, timeout=20):
    stdin, stdout, stderr = ssh.exec_command(f"zsh -l -c '{cmd}'", timeout=timeout)
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    code = stdout.channel.recv_exit_status()
    return code, out, err


def sudo_run(ssh, cmd, sudo_password, timeout=20):
    escaped = cmd.replace("'", "'\\''")
    full = f"echo {sudo_password!r} | sudo -S -p '' zsh -l -c '{escaped}'"
    stdin, stdout, stderr = ssh.exec_command(full, timeout=timeout)
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    code = stdout.channel.recv_exit_status()
    return code, out, err


def sftp_put_dir(sftp, local_dir, remote_dir):
    _mkdir_p(sftp, remote_dir)
    for entry in os.listdir(local_dir):
        lpath = os.path.join(local_dir, entry)
        rpath = posixpath.join(remote_dir, entry)
        if os.path.islink(lpath):
            continue
        if os.path.isdir(lpath):
            sftp_put_dir(sftp, lpath, rpath)
        else:
            sftp.put(lpath, rpath)


def _mkdir_p(sftp, remote_dir):
    parts = remote_dir.strip("/").split("/")
    path = ""
    for part in parts:
        path += "/" + part
        try:
            sftp.stat(path)
        except FileNotFoundError:
            sftp.mkdir(path)


def _remote_app(remote_dir):
    return posixpath.join(remote_dir, "rbserver.app")


def _build_xctestconfiguration(remote_app):
    """NSKeyedArchiver-encoded XCTestConfiguration -- reuses tidevice's own
    encoder (the same one Appium's own USB/DTX-based launch path relies on)
    rather than reimplementing NSKeyedArchiver's binary-plist format by hand.
    reportResultsToIDE=False is the key change from the normal Appium flow:
    there is no IDE/testmanagerd session behind this launch to report to."""
    xctest_bundle = posixpath.join(remote_app, "PlugIns/rbserver.xctest")
    session_identifier = uuid.uuid4()
    xctest_configuration = bplist.XCTestConfiguration({
        "testBundleURL": bplist.NSURL(None, f"file://{xctest_bundle}"),
        "sessionIdentifier": session_identifier,
        "targetApplicationBundleID": None,
        "targetApplicationArguments": [],
        "targetApplicationEnvironment": {},
        "testsToRun": set(),
        "testsMustRunOnMainThread": True,
        "reportResultsToIDE": False,
        "reportActivities": True,
        "automationFrameworkPath": "/Developer/Library/PrivateFrameworks/XCTAutomationSupport.framework",
    })
    return bplist.objc_encode(xctest_configuration)


def _launch_env(remote_app, port):
    xctest_bundle = posixpath.join(remote_app, "PlugIns/rbserver.xctest")
    xctestconfig_path = posixpath.join(remote_app, "session.xctestconfiguration")
    return {
        "CA_ASSERT_MAIN_THREAD_TRANSACTIONS": "0",
        "CA_DEBUG_TRANSACTIONS": "0",
        "DYLD_FRAMEWORK_PATH": f"{remote_app}/Frameworks:",
        "DYLD_LIBRARY_PATH": f"{remote_app}/Frameworks",
        "NSUnbufferedIO": "YES",
        "SQLITE_ENABLE_THREAD_ASSERTIONS": "1",
        "WDA_PRODUCT_BUNDLE_IDENTIFIER": "",
        "XCTestBundlePath": xctest_bundle,
        "XCTestConfigurationFilePath": xctestconfig_path,
        "XCODE_DBG_XPC_EXCLUSIONS": "com.apple.dt.xctestSymbolicator",
        "MJPEG_SERVER_PORT": "",
        "USE_PORT": str(port),
        "OS_ACTIVITY_DT_MODE": "YES",
    }, xctestconfig_path


def cmd_deploy(ssh, sftp, args):
    remote_app = _remote_app(args.remote_dir)
    print(f"[deploy] Removing any previous copy at {remote_app} ...")
    run(ssh, f"rm -rf {remote_app}")
    print(f"[deploy] Uploading app bundle to {remote_app} ...")
    t0 = time.time()
    sftp_put_dir(sftp, args.local_app, remote_app)
    print(f"[deploy] Upload done in {time.time() - t0:.1f}s")
    run(ssh, f"chmod +x {remote_app}/rbserver")

    _, xctestconfig_path = _launch_env(remote_app, args.port)
    print(f"[deploy] Writing {xctestconfig_path} ...")
    with sftp.open(xctestconfig_path, "wb") as f:
        f.write(_build_xctestconfiguration(remote_app))

    print("[deploy] Done. Run `start` to launch it.")


def _do_start(ssh, remote_app, port):
    env, _ = _launch_env(remote_app, port)
    export_lines = "; ".join(f'export {k}="{v}"' for k, v in env.items())
    launch = (
        f"{export_lines}; "
        f"nohup {remote_app}/rbserver "
        f"> /tmp/wda_launch.log 2>&1 & echo LAUNCHED_PID=$!; disown"
    )
    return run(ssh, launch)


def cmd_start(ssh, sftp, args):
    remote_app = _remote_app(args.remote_dir)
    print("[start] Killing any previous WDA process ...")
    run(ssh, _KILL_WDA_CMD)
    time.sleep(0.5)
    print("[start] Launching WDA directly via SSH (plain process spawn, no DTX)...")
    code, out, err = _do_start(ssh, remote_app, args.port)
    print(f"[start] {out.strip()}")
    ok = _wait_for_status(ssh, args.port)
    if not ok:
        print("[start] Did not come up -- tail of /tmp/wda_launch.log:")
        _, out, _ = run(ssh, "tail -c 3000 /tmp/wda_launch.log 2>&1")
        print(out)
    sys.exit(0 if ok else 1)


def _wait_for_status(ssh, port, timeout=20):
    print(f"[wait] Polling http://127.0.0.1:{port}/status for up to {timeout}s ...")
    for i in range(timeout):
        time.sleep(1)
        code, out, err = run(ssh, f"curl -s -m 2 -o /dev/null -w '%{{http_code}}' http://127.0.0.1:{port}/status")
        if out.strip() == "200":
            print(f"[wait] Ready after {i + 1}s.")
            return True
    return False


def cmd_stop(ssh, sftp, args):
    print("[stop] Killing WDA process ...")
    code, out, err = run(ssh, _KILL_WDA_CMD)
    print(f"[stop] {out.strip()}")


def cmd_status(ssh, sftp, args):
    code, out, err = run(ssh, f"curl -s -m 3 http://127.0.0.1:{args.port}/status")
    if out.strip():
        print(out)
    else:
        print("[status] No response -- WDA is not running or not reachable on that port.")
        sys.exit(1)


def cmd_logs(ssh, sftp, args):
    code, out, err = run(ssh, "tail -c 6000 /tmp/wda_launch.log 2>&1")
    print("--- /tmp/wda_launch.log (foreground `start`) ---")
    print(out)
    code, out, err = run(ssh, "tail -c 6000 /tmp/wda_daemon.log 2>&1")
    print("--- /tmp/wda_daemon.log (install-daemon) ---")
    print(out)


DAEMON_PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key><string>{label}</string>
	<key>UserName</key><string>mobile</string>
	<key>GroupName</key><string>mobile</string>
	<key>ProgramArguments</key>
	<array>
		<string>{binary}</string>
	</array>
	<key>EnvironmentVariables</key>
	<dict>
{env_entries}
	</dict>
	<key>RunAtLoad</key><true/>
	<key>KeepAlive</key><true/>
	<key>JetsamProperties</key>
	<dict>
		<key>JetsamMemoryLimit</key><integer>7777</integer>
	</dict>
	<key>StandardOutPath</key><string>/tmp/wda_daemon.log</string>
	<key>StandardErrorPath</key><string>/tmp/wda_daemon.log</string>
</dict>
</plist>
"""


def cmd_install_daemon(ssh, sftp, args):
    if not args.sudo_password:
        args.sudo_password = getpass.getpass("sudo password (usually same as SSH password): ")
    remote_app = _remote_app(args.remote_dir)
    env, _ = _launch_env(remote_app, args.port)
    env_entries = "\n".join(
        f"\t\t<key>{k}</key><string>{v}</string>" for k, v in env.items()
    )
    plist = DAEMON_PLIST_TEMPLATE.format(
        label=DAEMON_LABEL,
        binary=f"{remote_app}/rbserver",
        env_entries=env_entries,
    )

    remote_plist_tmp = posixpath.join(args.remote_dir, f"{DAEMON_LABEL}.plist")
    print(f"[daemon] Writing LaunchDaemon plist to {remote_plist_tmp} (staged, not yet root-owned) ...")
    _mkdir_p(sftp, args.remote_dir)
    with sftp.open(remote_plist_tmp, "wb") as f:
        f.write(plist.encode())

    print("[daemon] Killing any foreground WDA process first ...")
    run(ssh, _KILL_WDA_CMD)

    dest = f"/var/jb/Library/LaunchDaemons/{DAEMON_LABEL}.plist"
    print(f"[daemon] Installing as root at {dest} and loading via launchctl ...")
    code, out, err = sudo_run(
        ssh,
        f"launchctl unload {dest} 2>/dev/null; "
        f"cp {remote_plist_tmp} {dest} && "
        f"chown root:wheel {dest} && chmod 644 {dest} && "
        f"launchctl load {dest} && echo DAEMON_LOADED",
        args.sudo_password,
    )
    print(out, err)
    if "DAEMON_LOADED" not in out:
        print("[daemon] Failed to install/load the daemon -- see output above.")
        sys.exit(1)

    ok = _wait_for_status(ssh, args.port)
    print("[daemon] Installed. WDA will now auto-start at boot and auto-restart if it crashes."
          if ok else "[daemon] Installed but WDA did not answer /status -- check /tmp/wda_launch.log.")
    sys.exit(0 if ok else 1)


def cmd_uninstall_daemon(ssh, sftp, args):
    if not args.sudo_password:
        args.sudo_password = getpass.getpass("sudo password (usually same as SSH password): ")
    dest = f"/var/jb/Library/LaunchDaemons/{DAEMON_LABEL}.plist"
    code, out, err = sudo_run(
        ssh,
        f"launchctl unload {dest} 2>/dev/null; rm -f {dest} && echo REMOVED",
        args.sudo_password,
    )
    print(out, err)
    run(ssh, _KILL_WDA_CMD)


SUBCOMMANDS = {
    "deploy": cmd_deploy,
    "start": cmd_start,
    "stop": cmd_stop,
    "status": cmd_status,
    "logs": cmd_logs,
    "install-daemon": cmd_install_daemon,
    "uninstall-daemon": cmd_uninstall_daemon,
}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=list(SUBCOMMANDS))
    ap.add_argument("--host", required=True)
    ap.add_argument("--username", default="mobile")
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--remote-dir", default=REMOTE_DIR_DEFAULT)
    ap.add_argument("--local-app", default=LOCAL_APP_DEFAULT)
    ap.add_argument("--sudo-password", default=None, help="only needed for install-daemon/uninstall-daemon")
    args = ap.parse_args()

    password = os.environ.get("WDA_SSH_PASSWORD") or getpass.getpass(
        f"SSH password for {args.username}@{args.host}: "
    )

    ssh = connect(args.host, args.username, password)
    sftp = ssh.open_sftp()
    try:
        SUBCOMMANDS[args.command](ssh, sftp, args)
    finally:
        sftp.close()
        ssh.close()


if __name__ == "__main__":
    main()
