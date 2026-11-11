"""Keep host-bound TCP state in one namespace while job containers are replaced.

Docker Desktop can drop an immediate reuse of the same bridge-IP/source-port
tuple on host.docker.internal. Jobs reach this relay over the Linux bridge;
one persistent namespace allocates the upstream sockets. Bytes pass through
unchanged, with no HTTP, SQL, or connection retries.
"""

import subprocess
import sys
import time
from contextlib import contextmanager
from uuid import uuid4

BOUNDARY_HOST = "shifde-native-boundary"
BOUNDARY_PORT = 8765

RELAY = """
import select,socket,socketserver,sys

class Handler(socketserver.BaseRequestHandler):
 def handle(self):
  self.request.settimeout(10)
  with socket.create_connection(('host.docker.internal',int(sys.argv[1])),timeout=10) as upstream:
   peers={self.request:upstream,upstream:self.request}
   active=set(peers)
   while active:
    readable=select.select(list(active),[],[],30)[0]
    if not readable:return
    for source in readable:
     data=source.recv(65536)
     destination=peers[source]
     if data:
      destination.sendall(data)
     else:
      active.remove(source)
      try:destination.shutdown(socket.SHUT_WR)
      except OSError:pass

class Server(socketserver.ThreadingTCPServer):
 allow_reuse_address=True
 daemon_threads=True

server=Server(('0.0.0.0',8765),Handler)
print('native-boundary-ready',flush=True)
server.serve_forever()
"""


@contextmanager
def native_http_relay(image, network, upstream_port):
    name = "shifde-native-relay-" + uuid4().hex
    argv = [
        "docker",
        "run",
        "--detach",
        "--rm",
        "--name",
        name,
        "--network",
        network,
        "--network-alias",
        BOUNDARY_HOST,
        "--read-only",
        "--user",
        "10001:10001",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges=true",
        "--pids-limit",
        "128",
        "--memory",
        "128m",
        "--cpus",
        "1",
    ]
    if sys.platform == "linux":
        argv += ["--add-host", "host.docker.internal:host-gateway"]
    argv += [
        "--entrypoint",
        "/opt/venv/bin/python",
        image,
        "-u",
        "-c",
        RELAY,
        str(upstream_port),
    ]
    try:
        subprocess.run(argv, capture_output=True, text=True, check=True, timeout=20)
        until = time.monotonic() + 10
        while True:
            logs = subprocess.check_output(["docker", "logs", name], text=True, timeout=5)
            if "native-boundary-ready" in logs:
                break
            if time.monotonic() >= until:
                raise TimeoutError("native HTTP relay did not start")
            time.sleep(0.05)
        yield
    finally:
        subprocess.run(
            ["docker", "rm", "--force", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
        )
