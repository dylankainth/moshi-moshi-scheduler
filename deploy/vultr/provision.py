"""One-command Vultr deploy for the Foresight cloud server.

Creates: a firewall group (80/443 open, SSH only from --ssh-cidr or closed), a boot startup script that installs
Docker and brings up Compose (app + Caddy with automatic HTTPS on <ip>.sslip.io unless --domain), and an instance
with auto-backups. Dry run by default; --apply spends money.

  VULTR_API_KEY=...  VULTR_INFERENCE_KEY=...  python deploy/vultr/provision.py --repo https://github.com/you/foresight
  ... --apply

Request bodies follow Vultr API v2 as implemented by Vultr's official clients (govultr / vultr-cli).
The API key is read from the environment and never printed or logged.
"""
import argparse
import base64
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from foresight.netutil import http_json  # noqa: E402

API = "https://api.vultr.com/v2"
SCRIPT_PATH = Path(__file__).with_name("startup.sh")


class Vultr:
    def __init__(self, key, http=http_json, pause=0.1):
        self._key, self._http, self._pause = key, http, pause  # ~30 req/s limit; we stay far below it

    def call(self, method, path, body=None):
        st, r = self._http(method, API + path, body, {"Authorization": f"Bearer {self._key}"}, 30)
        time.sleep(self._pause)
        if st not in (200, 201, 202, 204):
            raise SystemExit(f"Vultr {method} {path} failed: HTTP {st} {(r or {}).get('error', '')}")
        return r or {}


def b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def startup_script(repo: str, ref: str) -> str:
    return SCRIPT_PATH.read_text().replace("__REPO__", repo).replace("__REF__", ref)


def env_file(a, token) -> str:
    lines = [f"FORESIGHT_TOKEN={token}", f"FORESIGHT_DEMO_SPEED={a.demo_speed}"]
    for k in ("VULTR_INFERENCE_KEY", "OPENAI_API_KEY", "OPENAI_MODEL"):
        if os.environ.get(k):
            lines.append(f"{k}={os.environ[k]}")
    if a.model:
        lines.append(f"VULTR_MODEL={a.model}")
    if a.domain:
        lines.append(f"DOMAIN={a.domain}")
    return "\n".join(lines) + "\n"


def rules(ssh_cidr):
    out = [{"ip_type": t, "protocol": "tcp", "subnet": s, "subnet_size": 0, "port": p, "notes": f"foresight {p}"}
           for t, s in (("v4", "0.0.0.0"), ("v6", "::")) for p in ("80", "443")]
    if ssh_cidr:
        subnet, size = ssh_cidr.split("/") if "/" in ssh_cidr else (ssh_cidr, "32")
        out.append({"ip_type": "v6" if ":" in subnet else "v4", "protocol": "tcp", "subnet": subnet,
                    "subnet_size": int(size), "port": "22", "notes": "foresight ssh (restricted)"})
    return out


def plan(a, token):
    script = startup_script(a.repo, a.ref)
    instance = {"region": a.region, "plan": a.plan, "os_id": a.os_id or "<Ubuntu 24.04 LTS x64, looked up via GET /os>",
                "label": a.label, "hostname": a.label, "backups": "enabled", "enable_ipv6": True,
                "tags": ["foresight"], "firewall_group_id": "<from step 1>", "script_id": "<from step 3>",
                "user_data": b64(env_file(a, token))}
    return {"firewall_group": {"description": a.label}, "firewall_rules": rules(a.ssh_cidr),
            "startup_script": {"name": a.label, "type": "boot", "script": b64(script)}, "instance": instance,
            "_script_text": script}


def show(p, a):
    red = dict(p["instance"])
    keys = [l.split("=")[0] for l in base64.b64decode(red["user_data"]).decode().splitlines() if l]
    red["user_data"] = f"<base64 env file, {len(red['user_data'])} chars, keys: {', '.join(keys)}; values hidden>"
    print("Plan (nothing is created without --apply):\n")
    print(f"1. POST {API}/firewalls\n   {json.dumps(p['firewall_group'])}")
    print(f"2. POST {API}/firewalls/<id>/rules  x{len(p['firewall_rules'])}")
    for r in p["firewall_rules"]:
        print(f"   {json.dumps(r)}")
    if not a.ssh_cidr:
        print("   (no SSH rule: port 22 closed. Pass --ssh-cidr YOUR.IP/32 to allow it)")
    print(f"3. POST {API}/startup-scripts\n   {{\"name\": \"{a.label}\", \"type\": \"boot\", \"script\": "
          f"<base64 of deploy/vultr/startup.sh, {len(p['startup_script']['script'])} chars>}}")
    print(f"4. POST {API}/instances\n   {json.dumps(red, indent=2)}")
    print(f"5. GET {API}/instances/<id> until it has a public IP, then print the HTTPS URL")
    print(f"\nHTTPS: Caddy on {a.domain or '<public-ip-dashed>.sslip.io'}; app from {a.repo}@{a.ref}")


def find_os(v):
    oses = v.call("GET", "/os?per_page=500").get("os", [])
    for o in oses:
        if "ubuntu 24.04" in o.get("name", "").lower() and "x64" in o.get("name", "").lower():
            return o["id"]
    raise SystemExit("couldn't find Ubuntu 24.04 x64 in GET /os; pass --os-id")


def apply(v, p, a, token, wait=True):
    os_id = a.os_id or find_os(v)
    fw = v.call("POST", "/firewalls", p["firewall_group"])["firewall_group"]["id"]
    print("firewall group", fw)
    for r in p["firewall_rules"]:
        v.call("POST", f"/firewalls/{fw}/rules", r)
    sc = v.call("POST", "/startup-scripts", p["startup_script"])["startup_script"]["id"]
    print("startup script", sc)
    inst = dict(p["instance"], os_id=os_id, firewall_group_id=fw, script_id=sc)
    i = v.call("POST", "/instances", inst)["instance"]
    print("instance", i["id"], "(auto-backups enabled)")
    ip = i.get("main_ip", "0.0.0.0")
    for _ in range(60 if wait else 1):
        if ip and ip != "0.0.0.0":
            break
        time.sleep(5 if wait else 0)
        ip = v.call("GET", f"/instances/{i['id']}")["instance"].get("main_ip")
    host = a.domain or (f"{ip.replace('.', '-')}.sslip.io" if ip and ip != "0.0.0.0" else None)
    print(f"\nURL (live in ~3-5 min, once Docker and Caddy's certificate are up): https://{host or '<pending ip>'}")
    print(f"Owner link (keep private): https://{host or '<host>'}/#token={token}")
    print(f"Agent on your PC: python -m foresight agent --server https://{host or '<host>'} --token <the token above>")
    return {"firewall_group": fw, "script": sc, "instance": i["id"], "ip": ip, "host": host}


def args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true", help="actually create resources (costs money)")
    p.add_argument("--region", default="ewr")
    p.add_argument("--plan", default="vc2-1c-1gb")
    p.add_argument("--os-id", type=int)
    p.add_argument("--label", default="foresight")
    p.add_argument("--repo", help="git URL the VM clones (default: this repo's origin)")
    p.add_argument("--ref", default="main")
    p.add_argument("--domain", help="your domain (A record -> VM); default <ip>.sslip.io")
    p.add_argument("--ssh-cidr", help="allow SSH only from this CIDR, e.g. 203.0.113.7/32 (default: SSH closed)")
    p.add_argument("--model", help="preferred Vultr inference model id (default: picked from /v1/models)")
    p.add_argument("--demo-speed", type=float, default=30)
    a = p.parse_args(argv)
    if not a.repo:
        try:
            a.repo = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True, text=True,
                                    check=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            a.repo = "<set --repo>"
    return a


def main(argv=None, http=http_json, wait=True):
    a = args(argv)
    token = os.environ.get("FORESIGHT_TOKEN") or secrets.token_urlsafe(24)
    p = plan(a, token)
    show(p, a)
    if not a.apply:
        print("\nDry run. Re-run with --apply to create these (billed by Vultr).")
        return None
    key = os.environ.get("VULTR_API_KEY")
    if not key:
        raise SystemExit("set VULTR_API_KEY")
    if a.repo.startswith("<"):
        raise SystemExit("pass --repo (a git URL the VM can clone)")
    if not (os.environ.get("VULTR_INFERENCE_KEY") or os.environ.get("OPENAI_API_KEY")):
        print("note: neither VULTR_INFERENCE_KEY nor OPENAI_API_KEY set; the server will run with rules/template only")
    return apply(Vultr(key, http), p, a, token, wait)


if __name__ == "__main__":
    main()
