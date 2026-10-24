import base64
import contextlib
import importlib.util
import io
import os
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("provision", ROOT / "deploy" / "vultr" / "provision.py")
provision = importlib.util.module_from_spec(spec)
spec.loader.exec_module(provision)

KEY, INF = "VULTR-API-KEY-SHOULD-NEVER-PRINT", "INFERENCE-KEY-SHOULD-NEVER-PRINT"
ARGS = ["--repo", "https://github.com/me/foresight", "--ssh-cidr", "203.0.113.7/32"]


class MockVultr:
    def __init__(self, fail_on=None):
        self.calls, self.fail_on = [], fail_on

    def __call__(self, method, url, body=None, headers=None, timeout=None):
        self.calls.append((method, url.replace(provision.API, ""), body, headers))
        path = url.replace(provision.API, "")
        if self.fail_on and path.startswith(self.fail_on):
            return 400, {"error": "Invalid plan"}
        if path.startswith("/os"):
            return 200, {"os": [{"id": 1743, "name": "Ubuntu 22.04 LTS x64"}, {"id": 2284, "name": "Ubuntu 24.04 LTS x64"}]}
        if path == "/firewalls":
            return 201, {"firewall_group": {"id": "fw-1"}}
        if "/rules" in path:
            return 201, {"firewall_rule": {"id": 1}}
        if path == "/startup-scripts":
            return 201, {"startup_script": {"id": "sc-1"}}
        if path == "/instances":
            return 202, {"instance": {"id": "in-1", "main_ip": "0.0.0.0"}}
        if path == "/instances/in-1":
            return 200, {"instance": {"id": "in-1", "main_ip": "45.76.1.2"}}
        return 404, {}


def run(argv, http, env):
    out = io.StringIO()
    with mock.patch.dict(os.environ, env, clear=False), contextlib.redirect_stdout(out), \
            mock.patch.object(provision.time, "sleep", lambda s: None):
        r = provision.main(argv, http=http, wait=True)
    return r, out.getvalue()


class Provision(unittest.TestCase):
    def test_dry_run_prints_plan_and_calls_nothing(self):
        http = MockVultr()
        r, out = run(ARGS, http, {"VULTR_API_KEY": KEY, "VULTR_INFERENCE_KEY": INF})
        self.assertIsNone(r)
        self.assertEqual(http.calls, [])
        for s in ("POST https://api.vultr.com/v2/firewalls", '"port": "443"', '"port": "22"', "203.0.113.7",
                  '"backups": "enabled"', "startup-scripts", "sslip.io", "Dry run"):
            self.assertIn(s, out)
        self.assertNotIn(KEY, out)
        self.assertNotIn(INF, out)
        self.assertIn("VULTR_INFERENCE_KEY", out)  # key *name* listed, value hidden

    def test_ssh_closed_by_default(self):
        _, out = run(["--repo", "x"], MockVultr(), {})
        self.assertNotIn('"port": "22"', out)
        self.assertIn("port 22 closed", out)

    def test_apply_sequence_and_bodies(self):
        http = MockVultr()
        r, out = run(ARGS + ["--apply"], http, {"VULTR_API_KEY": KEY, "VULTR_INFERENCE_KEY": INF})
        seq = [(m, p) for m, p, *_ in http.calls]
        self.assertEqual(seq[:2], [("GET", "/os?per_page=500"), ("POST", "/firewalls")])
        self.assertEqual(seq[2:7], [("POST", "/firewalls/fw-1/rules")] * 5)
        self.assertEqual(seq[7:9], [("POST", "/startup-scripts"), ("POST", "/instances")])
        self.assertEqual(seq[9], ("GET", "/instances/in-1"))
        self.assertTrue(all(h["Authorization"] == f"Bearer {KEY}" for *_, h in http.calls))

        rules = [b for m, p, b, _ in http.calls if "/rules" in p]
        self.assertEqual({(r["ip_type"], r["port"]) for r in rules[:4]}, {("v4", "80"), ("v4", "443"), ("v6", "80"), ("v6", "443")})
        self.assertEqual((rules[4]["subnet"], rules[4]["subnet_size"], rules[4]["port"]), ("203.0.113.7", 32, "22"))
        for r_ in rules:
            self.assertLessEqual(set(r_), {"ip_type", "protocol", "subnet", "subnet_size", "port", "notes", "source"})

        script = next(b for m, p, b, _ in http.calls if p == "/startup-scripts")
        self.assertEqual(script["type"], "boot")
        text = base64.b64decode(script["script"]).decode()
        self.assertIn("docker compose", text)
        self.assertIn("https://github.com/me/foresight", text)
        self.assertNotIn(INF, text)  # secrets go in per-instance user_data, not the account-wide script

        inst = next(b for m, p, b, _ in http.calls if p == "/instances")
        self.assertEqual((inst["os_id"], inst["firewall_group_id"], inst["script_id"], inst["backups"]),
                         (2284, "fw-1", "sc-1", "enabled"))
        self.assertIn(f"VULTR_INFERENCE_KEY={INF}", base64.b64decode(inst["user_data"]).decode())
        self.assertEqual(r["host"], "45-76-1-2.sslip.io")
        self.assertIn("https://45-76-1-2.sslip.io", out)
        self.assertNotIn(KEY, out)
        self.assertNotIn(INF, out)

    def test_apply_needs_key_and_aborts_on_error(self):
        with self.assertRaises(SystemExit):
            run(ARGS + ["--apply"], MockVultr(), {"VULTR_API_KEY": ""})
        http = MockVultr(fail_on="/instances")
        with self.assertRaises(SystemExit) as e:
            run(ARGS + ["--apply"], http, {"VULTR_API_KEY": KEY})
        self.assertIn("Invalid plan", str(e.exception))
        self.assertNotIn(KEY, str(e.exception))


if __name__ == "__main__":
    unittest.main()
