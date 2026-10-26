"""
Headless tests for MaxMCP instance pinning (no 3ds Max needed).

Spins up fake TCP listeners that mimic the Max-side protocol, then drives
mcp_server_3dsmax.send_to_max through: endpoint resolution, token match,
foreign-instance refusal, stale-endpoint fallback, and legacy listeners.

Usage:  python test_instance_pinning.py
Exit code 0 = all checks passed.
"""

import json
import os
import socket
import sys
import tempfile
import threading
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TMP = tempfile.mkdtemp(prefix="maxmcp_pin_")
os.environ["MAXMCP_ENDPOINT"] = os.path.join(TMP, "endpoint.json")

import mcp_server_3dsmax as S  # noqa: E402  (env must be set first)


def free_port():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.close()
    return port


class FakeMax:
    """Minimal newline-delimited JSON listener with a fixed instance token."""

    def __init__(self, token=None, legacy=False):
        self.token = token if token is not None else uuid.uuid4().hex
        self.legacy = legacy
        self.requests = []
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(5)
        self._srv.settimeout(0.5)
        self.port = self._srv.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def halt(self):
        self._stop.set()
        self._thread.join(timeout=3.0)
        try:
            self._srv.close()
        except Exception:
            pass

    def _loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                conn.settimeout(5.0)
                data = b""
                while not data.endswith(b"\n"):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                try:
                    req = json.loads(data.decode("utf-8"))
                except Exception:
                    req = {}
                self.requests.append(req)
                if req.get("hello"):
                    resp = {"id": req.get("id"), "status": "ok",
                            "output": "hello fake",
                            "result": {"instance_id": self.token,
                                      "version": "test", "source": "fake"},
                            "error": None, "version": "test"}
                else:
                    resp = {"id": req.get("id"), "status": "ok",
                            "output": "fake-ok",
                            "__mcp_result__": "unused",
                            "result": "pong", "error": None,
                            "version": "test"}
                if not self.legacy:
                    resp["instance_id"] = self.token
                conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass


def write_endpoint(host, port, token):
    with open(os.environ["MAXMCP_ENDPOINT"], "w",
              encoding="utf-8") as handle:
        json.dump({"host": host, "port": port, "instance_id": token,
                   "pid": 99999, "version": "test",
                   "source": "fake"}, handle)


passed = []


def check(cond, label):
    print("[%s] %s" % ("PASS" if cond else "FAIL", label))
    assert cond, label
    passed.append(label)


def main():
    print("== instance pinning tests ==")

    # 1. Endpoint match: request lands on the advertised Max, token verified.
    fake_a = FakeMax().start()
    write_endpoint("127.0.0.1", fake_a.port, fake_a.token)
    resp = S.send_to_max('print("ping-ok")\n__mcp_result__ = "pong"',
                         timeout=10.0)
    check(resp.get("result") == "pong", "endpoint target answers")
    check(resp.get("instance_id") == fake_a.token, "token stamped by Max")
    kinds = sorted(r.get("hello", False) and "hello" or "code"
                   for r in fake_a.requests)
    check(kinds == ["code", "hello"], "one probe + one code, then cached")
    n_before = len(fake_a.requests)
    resp = S.send_to_max('print("ping-ok")\n__mcp_result__ = "pong"',
                         timeout=10.0)
    check(resp.get("result") == "pong", "cached pinning still answers")
    check(len(fake_a.requests) == n_before + 1, "steady state: no re-probe")

    # 2. Foreign instance: same port, different token -> refused, never run.
    fake_a.requests.clear()
    write_endpoint("127.0.0.1", fake_a.port, "WRONG" * 8)
    try:
        S.send_to_max('print("SHOULD-NOT-RUN")', timeout=10.0)
        check(False, "foreign instance refused")
    except S.MaxConnectionError as exc:
        check("DIFFERENT" in str(exc), "mismatch error names the problem")
    ran_foreign = [r for r in fake_a.requests
                   if r.get("code") == 'print("SHOULD-NOT-RUN")']
    check(not ran_foreign, "refused before any code could run")
    fake_a.halt()

    # 3. Stale endpoint (dead Max) falls back to env target.
    dead = free_port()
    write_endpoint("127.0.0.1", dead, "DEAD" * 8)
    fake_b = FakeMax().start()
    os.environ["MAX_HOST"] = "127.0.0.1"
    os.environ["MAX_PORT"] = str(fake_b.port)
    S.MAX_HOST = "127.0.0.1"
    S.MAX_PORT = fake_b.port
    resp = S.send_to_max('print("ping-ok")\n__mcp_result__ = "pong"',
                         timeout=10.0)
    check(resp.get("result") == "pong", "stale endpoint falls back to env")
    fake_b.halt()

    # 4. Legacy listener (no stamp) is allowed through.
    fake_c = FakeMax(legacy=True).start()
    write_endpoint("127.0.0.1", fake_c.port, fake_c.token)
    resp = S.send_to_max('print("ping-ok")\n__mcp_result__ = "pong"',
                         timeout=10.0)
    check(resp.get("result") == "pong", "legacy unstamped listener allowed")
    fake_c.halt()

    # 5. probe_instance identifies without executing scene code.
    fake_d = FakeMax().start()
    ident = S.probe_instance("127.0.0.1", fake_d.port)
    check(ident is not None and ident["instance_id"] == fake_d.token,
          "hello probe identifies instance")
    check(all("code" not in r for r in fake_d.requests),
          "probe sent no code requests")
    check(S.probe_instance("127.0.0.1", free_port()) is None,
          "probe of dead port returns None")
    fake_d.halt()

    print("INSTANCE PINNING TESTS PASSED (%d)." % len(passed))


if __name__ == "__main__":
    main()
