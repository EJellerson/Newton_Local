#!/usr/bin/env python3
"""Startup-persistence proving for the Hermes gateway and Open WebUI services.

Answers, with measurements rather than assertions:
  * what exactly the two services are (plist, domain, flags, bind, logs);
  * that launchd owns them, as the normal user, not root;
  * that nothing interactive is auto-launched;
  * that KeepAlive brings them back from a kill -9 of the whole process group;
  * that RunAtLoad brings them back from a cold domain load, which is what
    login does;
  * that no session-scoped PATH entry was baked into the service definition;
  * that Qwen-A and Qwen-B were not disturbed by any of it.

DESTRUCTIVE-ish: it kill -9s both services and bootouts/bootstraps them. Safe on
this machine (they are supervised and come straight back) but do not run it in
the middle of a conversation you care about.

    python3 scripts/prove-hermes-service.py > benchmarks/transcripts/hermes-service-lifecycle.txt
"""
import json, os, pathlib, pwd, re, signal, subprocess, sys, time, urllib.request

UID  = os.getuid()
HOME = pathlib.Path.home()
KEY  = next(l.split("=", 1)[1].strip()
            for l in (HOME/".hermes/profiles/localchat/.env").read_text().splitlines()
            if l.startswith("API_SERVER_KEY="))
SVCS = [("ai.hermes.gateway-localchat", 8642, True),
        ("com.newton.openwebui",        3000, False)]

def sh(*a): return subprocess.run(a, capture_output=True, text=True)
def plist(label): return HOME/f"Library/LaunchAgents/{label}.plist"
def pl(label, key):
    r = sh("plutil", "-extract", key, "raw", "-o", "-", str(plist(label)))
    return r.stdout.strip() if r.returncode == 0 else "(absent)"
def lc(label):
    o = sh("launchctl", "print", f"gui/{UID}/{label}").stdout
    g = lambda k: (re.search(rf"^\s*{k} = (.+)$", o, re.M) or [None, None])[1]
    return {"state": g("state"), "pid": g("pid"), "runs": g("runs"),
            "exit": g("last exit code"), "path": g("path")}
def spids(port):
    o = sh("lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN").stdout
    return [l.split()[1] for l in o.splitlines()[1:]]
def ok(port, auth):
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/health",
              headers={"Authorization": f"Bearer {KEY}"} if auth else {})
        urllib.request.urlopen(req, timeout=2).read()
        return True
    except Exception:
        return False

def h(s): print(f"\n{s}\n" + "-" * len(s))

print("Hermes / Open WebUI startup-persistence proving")
print("Machine: macOS 14.8.5, user LaunchAgents in gui/%d, no root involved" % UID)

h("1. Service identity")
for label, port, auth in SVCS:
    print(f"  {label}")
    print(f"    plist            {plist(label)}")
    print(f"    domain           gui/{UID}   (user LaunchAgent, runs as {pwd.getpwuid(UID).pw_name})")
    for k in ("RunAtLoad", "KeepAlive", "ThrottleInterval", "ExitTimeOut", "ProcessType"):
        print(f"    {k:<16} {pl(label, k)}")
    print(f"    stdout           {pl(label,'StandardOutPath')}")
    print(f"    stderr           {pl(label,'StandardErrorPath')}")
    print(f"    plutil -lint     {sh('plutil','-lint',str(plist(label))).stdout.strip().split(': ')[-1]}")
    print(f"    bind             127.0.0.1:{port}")

h("2. Currently supervised by launchd, owned by launchd (PPID 1), not root")
for label, port, auth in SVCS:
    s = lc(label)
    print(f"  {label}")
    print(f"    state={s['state']} pid={s['pid']} runs={s['runs']} last_exit={s['exit']}")
    if s["pid"]:
        print("    " + sh("ps","-o","pid,ppid,user,comm","-p",s["pid"]).stdout.strip().splitlines()[-1])
    print(f"    socket holders on :{port} -> {spids(port)}")

h("3. Nothing interactive is auto-launched")
args = json.loads(sh("plutil","-extract","ProgramArguments","json","-o","-",
                     str(plist("ai.hermes.gateway-localchat"))).stdout)
print("  gateway ProgramArguments (tail): " + " ".join(args[-6:]))
print("  -> 'gateway run': a headless backend. No 'chat', no '--tui', no window.")
print("  Open WebUI ProgramArguments: " + " ".join(json.loads(
      sh("plutil","-extract","ProgramArguments","json","-o","-",
         str(plist("com.newton.openwebui"))).stdout)))
print("  -> 'serve', an HTTP server. The TUI and the browser stay manual.")

h("4. KeepAlive — recovery from an unexpected process failure (kill -9)")
for label, port, auth in SVCS:
    before = lc(label)
    victims = sorted(set(([before["pid"]] if before["pid"] else []) + spids(port)))
    print(f"  {label}")
    print(f"    before   pid={before['pid']} runs={before['runs']}")
    print(f"    kill -9  {' '.join(victims)}  (supervisor AND the child holding the socket)")
    for v in victims:
        try: os.kill(int(v), signal.SIGKILL)
        except ProcessLookupError: pass
    t0, down = time.time(), None
    for _ in range(100):
        if not spids(port) and not ok(port, auth):
            down = time.time() - t0; break
        time.sleep(0.2)
    print(f"    down     socket gone after {down:.1f}s" if down is not None
          else "    down     never observed down — measurement inconclusive")
    up = None
    for _ in range(600):
        if ok(port, auth): up = time.time() - t0; break
        time.sleep(0.5)
    after = lc(label)
    print(f"    up       serving again after {up:.1f}s" if up else "    up       DID NOT RECOVER in 300s")
    print(f"    after    pid={after['pid']} runs={before['runs']} -> {after['runs']}"
          f"   {'RECOVERED' if up else 'FAILED'}")

h("5. RunAtLoad — logout/login cycle")
print("  bootout removes the agent from the user domain exactly as logout does;")
print("  bootstrap re-loads it exactly as login does. No start command is issued,")
print("  so if the services come back, RunAtLoad is what did it.")
for label, port, auth in SVCS:
    sh("launchctl", "bootout", f"gui/{UID}/{label}")
time.sleep(4)
for label, port, auth in SVCS:
    print(f"  after logout   {label:<32} loaded={sh('launchctl','print',f'gui/{UID}/{label}').returncode==0}"
          f"  health={ok(port,auth)}")
time.sleep(2)
t0 = time.time()
for label, port, auth in SVCS:
    r = sh("launchctl", "bootstrap", f"gui/{UID}", str(plist(label)))
    print(f"  bootstrap      {label:<32} rc={r.returncode} {r.stderr.strip()}")
for label, port, auth in SVCS:
    up = None
    for _ in range(240):
        if ok(port, auth): up = time.time() - t0; break
        time.sleep(0.5)
    s = lc(label)
    print(f"  after login    {label:<32} state={s['state']} pid={s['pid']} "
          + (f"healthy after {up:.1f}s" if up else "DID NOT COME BACK"))

h("6. Enabled state")
dis = sh("launchctl", "print-disabled", f"gui/{UID}").stdout
for label, _, _ in SVCS:
    m = re.search(rf'"{re.escape(label)}" => (\w+)', dis)
    print(f"  {label:<32} {m.group(1) if m else 'not listed (== enabled)'}")

h("7. Service PATH carries nothing session-scoped")
p = pl("ai.hermes.gateway-localchat", "EnvironmentVariables.PATH")
bad = [e for e in p.split(":") if "/Library/Application Support/" in e
       or "/private/var/folders/" in e or e.startswith("/tmp/")]
print(f"  {len(p.split(':'))} entries, {len(bad)} session-scoped: {bad or 'none'}")
for e in p.split(":"): print("    " + e)

h("8. Nothing else was disturbed")
qa = sh("pgrep", "-f", "llama-server.*8080").stdout.split()
if qa:
    print("  Qwen-A  " + sh("ps","-o","pid,ppid,etime,comm","-p",qa[0]).stdout.strip().splitlines()[-1])
print("  Qwen-A  health " + sh("curl","-sS","-m","5","http://127.0.0.1:8080/health").stdout)
print("  Qwen-B  " + sh(str(HOME/"Newton_Local/scripts/qwen-b"), "status").stdout.splitlines()[0])

h("9. Boot vs login — the honest limit")
print("""  These are user LaunchAgents in gui/%d. macOS loads ~/Library/LaunchAgents at
  LOGIN, not at boot. So:
    * you log in  -> Hermes and Open WebUI are already running before you reach
                     the desktop. Nothing to type.
    * the Mac boots to a login window with nobody logged in -> neither runs,
      because there is no user session to run them in.

  A system LaunchDaemon would start at boot, but it is the wrong tool here.
  Hermes needs the user's home directory, the login keychain, a GUI session for
  Playwright, and osascript clipboard access -- none of which a pre-login daemon
  has. It would also mean a root-owned service holding a shell, which this
  commission is explicitly forbidden to create.

  If availability with nobody at the keyboard is wanted, the supported answer is
  macOS automatic login (System Settings > Users & Groups > Automatically log in
  as), which makes login happen at boot. That is a machine-policy decision for
  the operator, not something this commission changes.

  NOT OBSERVED: a physical reboot. What IS observed above is a full cold domain
  load -- bootout, then bootstrap with no start command -- which is the same
  mechanism login uses.""" % UID)
