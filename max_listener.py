"""
3ds Max 2022 TCP Listener Script (Python 3.7 / pymxs compatible).

LEGACY / MINIMAL bridge. Canonical transport is now max_mcp_ui.McpBridgeServer
(full GUI plugin: skill dispatch, tools/list + await_change, profiles).
This file is kept for headless/minimal use and as protocol reference.
New work should target max_mcp_ui.py; both bind 127.0.0.1:8080 by default,
so run only ONE bridge at a time.

Run inside 3ds Max 2022 via: Scripting -> Run Script -> select this file.
(or drag-drop the file into a 3ds Max viewport, or MAXScript Listener:
    python.ExecuteFile @"C:/path/to/max_listener.py")

What it does:
  - Starts a TCP server on 127.0.0.1:8080 (configurable below).
  - Receives newline-delimited JSON requests: {"id": "<id>", "code": "<python code>"} + "\\n"
  - Executes the code in the MAIN 3ds Max thread (see threading model below).
  - Returns newline-delimited JSON: {"id","status":"ok"|"error","output","result","error"} + "\\n"

Threading model (main-thread safety):
  A background daemon thread does ALL network I/O (accept + recv + send) and
  never touches pymxs. Complete requests wait on a queue. The queue is
  drained by a WinForms Timer Tick, which fires on the thread that created
  it (the Run-Script/main thread), so all pymxs calls run there while the
  Tick never blocks on the network - worst case a few ms of execution.

  IMPORTANT (learned from Max 2022 field testing): this script uses NO
  MAXScript evaluation from Python (pymxs `rt.execute` does not exist in
  3ds Max 2022) and NO registerTimeCallback (it only fires while animation
  time changes, never in a static scene - requests would queue forever).
  The control panel is a plain WinForms dialog, not a Max rollout.
  Startup succeeds ONLY if a pump is actually active; "already running"
  never masks a missing pump.

  FALLBACK MODE (if .NET WinForms is unavailable): a single-threaded blocking
  accept loop with socket timeouts. The Max UI will be UNRESPONSIVE while
  this runs - use only for testing. Press Esc or call stop_max_listener().

Protocol details:
  - One JSON object per line (UTF-8). Each request gets exactly one response line.
  - Request fields:  id (str, echoed back), code (str, Python source to exec).
  - Response fields: id, status ("ok"/"error"), output (captured stdout+stderr),
                     result (value of __mcp_result__ after exec, must be JSON
                     serializable, else converted with repr), error (traceback
                     string or None).
  - The exec namespace persists across calls (global _MCP_NAMESPACE), so
    variables/imports survive between requests. `rt` (pymxs.runtime) and
    `__mcp_result__` are pre-seeded. Set `__mcp_result__` in your code to
    return structured data.

Requires: 3ds Max 2022 built-in Python 3.7 (pymxs). No pip packages needed.
"""

import io
import json
import os
import socket
import sys
import threading
import time
import traceback

try:
    import queue  # Python 3.x
except ImportError:  # pragma: no cover - Python 2 fallback, never in Max 2022
    import Queue as queue

try:
    from contextlib import redirect_stdout, redirect_stderr
except ImportError:  # pragma: no cover - Python 3.7 always has this
    redirect_stdout = None
    redirect_stderr = None

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
HOST = "127.0.0.1"
PORT = 8080
# NOTE: keep the Tick slow. A fast WinForms Timer (e.g. 50 ms) starves 3ds
# Max's UI message loop and makes Max feel frozen. 500 ms is unnoticeable
# for commands but leaves the UI responsive.
POLL_INTERVAL_MS = 500         # WinForms Timer tick interval (queue drain)
PANEL_AUTOSHOW = True          # show the floating control panel on start
ACCEPT_TIMEOUT = 0.5          # background accept-loop poll interval (seconds)
CLIENT_TIMEOUT = 30.0         # max seconds to read one request line
EXEC_WAIT_TIMEOUT = 120.0     # max seconds a client waits for main-thread exec
SOCKET_TIMEOUT = 1.0         # fallback blocking-loop accept timeout (seconds)
BUFFER_SIZE = 65536
MAX_CODE_SIZE = 1024 * 1024  # 1 MB safety limit per request
# Bump on every change; reported in each response so clients can verify
# which script version is actually running inside 3ds Max.
LISTENER_VERSION = "1.4.3"

# ---------------------------------------------------------------------------
# pymxs import (graceful outside of 3ds Max so the file can be syntax-checked)
# ---------------------------------------------------------------------------
try:
    from pymxs import runtime as rt
    _IN_MAX = True
except Exception as exc:  # ImportError when checked outside 3ds Max
    rt = None
    _IN_MAX = False
    print("[max_listener] WARNING: pymxs not available (%s). "
          "Scene calls will fail; socket layer still works." % exc)

# ---------------------------------------------------------------------------
# Persistent execution namespace (survives across requests)
# ---------------------------------------------------------------------------
_MCP_NAMESPACE = {"__name__": "__mcp__", "__mcp_result__": None}
if rt is not None:
    _MCP_NAMESPACE["rt"] = rt

_MCP_RUNNING = False
_MCP_SERVER_SOCKET = None
_MCP_TIMER = None
_MCP_SOCKET_THREAD = None
# File log: independent diagnostic channel (readable even if the socket or
# Max UI is wedged). Lives in %TEMP% so support can inspect it any time.
LOG_PATH = os.path.join(os.environ.get("TEMP", os.path.expanduser("~")),
                        "max_mcp_bridge.log")
_PUMP_TICKS = [0]  # native-callback pump ticks (list = Py3.7-safe counter)
_WIN_TICKS = [0]   # WinForms Timer ticks
_CTYPES_TICKS = [0]  # Win32 SetTimer ticks
_MCP_CTYPES_TIMER = None  # (callback,) tuple kept alive while armed
_NATIVE_ON = [False]  # is the native time callback currently registered?


def _log(msg):
    """Append timestamped line to the bridge log. Never raises."""
    try:
        line = "[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        with open(LOG_PATH, "a") as handle:
            handle.write(line)
    except Exception:
        pass
    try:
        print("[max_listener] %s" % msg)
    except Exception:
        pass
# Queue of (request_line, holder). Holder: {"event": threading.Event,
# "response": dict or None}. Background threads enqueue; ONLY the main
# thread (Timer Tick) dequeues and executes. Network I/O never blocks Max.
_MCP_REQUEST_QUEUE = queue.Queue()


def _json_safe(value):
    """Return value if JSON serializable, else its repr string."""
    try:
        json.dumps(value)
        return value
    except Exception:
        try:
            return repr(value)
        except Exception:
            return "<unrepresentable>"


def execute_code(code, namespace=None):
    """Execute Python source in the (persistent) namespace.

    Returns (status, output, result, error) where status is "ok"/"error".
    Runs in the CALLER's thread - the listener guarantees the caller is the
    main 3ds Max thread (Timer Tick or Run-Script thread).
    """
    if namespace is None:
        namespace = _MCP_NAMESPACE
    if not isinstance(code, str):
        return ("error", "", None, "Field 'code' must be a string.")
    if len(code) > MAX_CODE_SIZE:
        return ("error", "", None,
                "Code exceeds MAX_CODE_SIZE (%d bytes)." % MAX_CODE_SIZE)
    # Reset per-call result slot.
    namespace["__mcp_result__"] = None
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    try:
        if redirect_stdout is not None:
            with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
                exec(compile(code, "<mcp>", "exec"), namespace)
        else:  # pragma: no cover - never happens on 3.7
            old_out, old_err = sys.stdout, sys.stderr
            sys.stdout, sys.stderr = stdout_buf, stderr_buf
            try:
                exec(compile(code, "<mcp>", "exec"), namespace)
            finally:
                sys.stdout, sys.stderr = old_out, old_err
    except Exception:
        output = stdout_buf.getvalue() + stderr_buf.getvalue()
        return ("error", output, None, traceback.format_exc())
    output = stdout_buf.getvalue() + stderr_buf.getvalue()
    result = _json_safe(namespace.get("__mcp_result__"))
    return ("ok", output, result, None)


def handle_request_line(line):
    """Parse one JSON request line, execute it, return the response dict."""
    try:
        request = json.loads(line)
    except Exception as exc:
        return {"id": None, "status": "error", "output": "",
                "result": None, "error": "Invalid JSON: %s" % exc,
                "version": LISTENER_VERSION}
    req_id = request.get("id")
    code = request.get("code")
    if code is None:
        return {"id": req_id, "status": "error", "output": "",
                "result": None, "error": "Missing required field: 'code'.",
                "version": LISTENER_VERSION}
    status, output, result, error = execute_code(code)
    return {"id": req_id, "status": status, "output": output,
            "result": result, "error": error,
            "version": LISTENER_VERSION}


def _create_server_socket():
    """Create, bind and configure the listening socket (blocking mode).

    Blocking is fine: this socket is only ever touched by the background
    accept thread, never by the main UI thread.
    """
    global _MCP_SERVER_SOCKET
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, PORT))
    srv.listen(5)
    srv.settimeout(ACCEPT_TIMEOUT)
    _MCP_SERVER_SOCKET = srv
    return srv


def _read_request_line(conn):
    """Read exactly one newline-terminated request line (background thread)."""
    conn.settimeout(CLIENT_TIMEOUT)
    data = b""
    while not data.endswith(b"\n"):
        chunk = conn.recv(BUFFER_SIZE)
        if not chunk:
            break
        data += chunk
        if len(data) > MAX_CODE_SIZE + 4096:
            break
    if not data.strip():
        return None
    # Only the first line is used (one request per connection).
    return data.decode("utf-8", errors="replace").splitlines()[0]


def _send_response(conn, response):
    try:
        conn.sendall((json.dumps(response) + "\n").encode("utf-8"))
    except Exception:
        pass


def _handle_conn(conn):
    """Serve one client: read request, hand to main thread, send response.

    Runs in a background thread - MUST NOT touch pymxs here.
    """
    try:
        line = _read_request_line(conn)
        if line is None:
            return
        holder = {"event": threading.Event(), "response": None}
        _MCP_REQUEST_QUEUE.put((line, holder))
        # Wait for the main thread (Timer Tick) to execute and respond.
        if not holder["event"].wait(EXEC_WAIT_TIMEOUT):
            _send_response(conn, {"id": None, "status": "error", "output": "",
                                  "result": None,
                                  "error": "Timeout: main 3ds Max thread did "
                                  "not execute the request within %ds. "
                                  "Is 3ds Max frozen or busy?" % EXEC_WAIT_TIMEOUT,
                                  "version": LISTENER_VERSION})
            return
        _send_response(conn, holder["response"])
    except Exception:
        try:
            _send_response(conn, {"id": None, "status": "error", "output": "",
                                  "result": None,
                                  "error": traceback.format_exc(),
                                  "version": LISTENER_VERSION})
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _socket_worker():
    """Background accept loop. Runs in a daemon thread - no pymxs here."""
    global _MCP_SERVER_SOCKET
    srv = _MCP_SERVER_SOCKET
    while _MCP_RUNNING and srv is not None:
        try:
            conn, addr = srv.accept()
        except socket.timeout:
            continue  # poll interval - re-check _MCP_RUNNING flag
        except socket.error:
            if _MCP_RUNNING:
                time.sleep(ACCEPT_TIMEOUT)
            continue
        worker = threading.Thread(target=_handle_conn, args=(conn,))
        worker.daemon = True
        worker.start()


def start_socket_thread():
    """Bind the port and start the background accept thread.

    No WinForms dependency - also used by the fallback mode and by tests.
    Returns True on success.
    """
    global _MCP_RUNNING, _MCP_SOCKET_THREAD
    if _MCP_SOCKET_THREAD is not None and _MCP_SOCKET_THREAD.is_alive():
        return True
    try:
        _create_server_socket()
    except Exception as exc:
        _log("Cannot bind %s:%d: %s" % (HOST, PORT, exc))
        print("[max_listener] Cannot bind %s:%d: %s" % (HOST, PORT, exc))
        return False
    _MCP_RUNNING = True
    worker = threading.Thread(target=_socket_worker)
    worker.daemon = True
    _MCP_SOCKET_THREAD = worker
    worker.start()
    _log("socket bound on %s:%d, accept thread started" % (HOST, PORT))
    return True


def _pump_queue_once():
    """Execute all queued requests. CALL ONLY FROM THE MAIN THREAD.

    Non-blocking: returns immediately when the queue is empty. Each request
    is executed via handle_request_line (which runs exec in the persistent
    namespace), so all pymxs calls happen here in the main thread.
    """
    while True:
        try:
            line, holder = _MCP_REQUEST_QUEUE.get_nowait()
        except queue.Empty:
            break
        try:
            holder["response"] = handle_request_line(line)
        except Exception:
            holder["response"] = {"id": None, "status": "error", "output": "",
                                  "result": None,
                                  "error": traceback.format_exc()}
        finally:
            holder["event"].set()


def pump_once():
    """Public pump entry for the MAXScript time callback (native pump).

    Runs on 3ds Max's true main thread (via python.Execute from MAXScript).
    Every 50th tick appends a heartbeat to the log file so a silent pump
    is distinguishable from a dead socket. Never raises.
    """
    try:
        _pump_queue_once()
        _PUMP_TICKS[0] += 1
        if _PUMP_TICKS[0] % 50 == 0:
            _log("native pump heartbeat tick=%d queue_empty=%s"
                 % (_PUMP_TICKS[0], _MCP_REQUEST_QUEUE.empty()))
    except Exception:
        _log("pump_once FAILED:\n" + traceback.format_exc())


def bridge_status():
    """Return a diagnostics dict (version, pumps, threads, queue depth).

    Callable remotely: send {"code": "__mcp_result__ = bridge_status()"}.
    NOTE: exec namespace is _MCP_NAMESPACE, so import first:
      {"code": "import __main__ as _M\\n__mcp_result__ = _M.bridge_status()"}
    """
    import __main__ as _main_mod  # noqa - proves which module object runs us
    info = {"version": LISTENER_VERSION,
            "running": _MCP_RUNNING,
            "queue_depth": _MCP_REQUEST_QUEUE.qsize(),
            "winforms_ticks": _WIN_TICKS[0],
            "native_ticks": _PUMP_TICKS[0],
            "native_registered": _NATIVE_ON[0],
            "timer_active": _MCP_TIMER is not None,
            "in_max": _IN_MAX,
            "module": getattr(_main_mod, "__name__", "?"),
            "py_thread": threading.current_thread().name,
            "py_main": threading.main_thread().name}
    try:
        import clr  # noqa
        from System.Threading import Thread as NetThread
        from System.Windows.Forms import Application as WinApp
        info["dotnet_thread"] = NetThread.CurrentThread.ManagedThreadId
        info["is_background"] = NetThread.CurrentThread.IsBackground
        info["message_loop"] = bool(WinApp.MessageLoop)
    except Exception as exc:
        info["dotnet_info"] = "unavailable: %s" % exc
    return _json_safe(info)


# ---------------------------------------------------------------------------
# Floating control panel: a plain WinForms dialog (modeless).
# Deliberately NOT a Max rollout: 3ds Max 2022 pymxs has no rt.execute, so
# MAXScript-based UI cannot be built from this script. Buttons call the
# Python start/stop functions directly.
# ---------------------------------------------------------------------------
_MCP_FORM = None


def _set_form_status(text):
    form = _MCP_FORM
    if form is None:
        return
    try:
        controls = form.Controls.Find("lblStatus", True)
        if controls is not None and len(controls) > 0:
            controls[0].Text = text
    except Exception:
        pass


def show_listener_panel():
    """Show the floating MCP Bridge control panel. Safe to call repeatedly."""
    global _MCP_FORM
    if _MCP_FORM is not None:
        try:
            if _MCP_FORM.Visible:
                _set_form_status("Status: RUNNING on %s:%d" % (HOST, PORT))
                return True
        except Exception:
            _MCP_FORM = None
    try:
        import clr
        clr.AddReference("System.Windows.Forms")
        clr.AddReference("System.Drawing")
        from System.Windows.Forms import Form, Button, Label
        from System.Drawing import Size, Point, ContentAlignment
    except Exception as exc:
        _log("panel unavailable (no WinForms): %s" % exc)
        return False
    try:
        form = Form()
        form.Text = "MCP Bridge : Max <-> OpenCode"
        form.Width = 330
        form.Height = 210
        form.TopMost = True
        form.ShowInTaskbar = False
        form.FormBorderStyle = 3  # FixedDialog

        lbl = Label()
        lbl.Name = "lblStatus"
        lbl.Text = "Status: RUNNING on %s:%d" % (HOST, PORT)
        lbl.Location = Point(10, 10)
        lbl.Size = Size(290, 30)
        lbl.TextAlign = ContentAlignment.MiddleCenter

        btn_stop = Button()
        btn_stop.Text = "STOP listener (Max is mine)"
        btn_stop.Location = Point(10, 50)
        btn_stop.Size = Size(290, 40)

        btn_start = Button()
        btn_start.Text = "START listener (allow access)"
        btn_start.Location = Point(10, 100)
        btn_start.Size = Size(290, 40)

        def _on_stop(sender, args):
            stop_max_listener()

        def _on_start(sender, args):
            start_listener()

        btn_stop.Click += _on_stop
        btn_start.Click += _on_start
        form.Controls.Add(lbl)
        form.Controls.Add(btn_stop)
        form.Controls.Add(btn_start)
        form.Show()  # modeless: Max stays usable
        _MCP_FORM = form
        _log("control panel shown (WinForms)")
        return True
    except Exception:
        _log("panel build FAILED:\n" + traceback.format_exc())
        return False


def hide_listener_panel():
    """Close the floating control panel if it is open."""
    global _MCP_FORM
    if _MCP_FORM is None:
        return
    try:
        _MCP_FORM.Close()
    except Exception:
        pass
    _MCP_FORM = None


def set_panel_status(text):
    """Update the panel status label (best effort, never raises)."""
    _set_form_status(text)


# ---------------------------------------------------------------------------
# PRIMARY MODE: socket thread (network I/O) + native MAXScript time callback
# (main-thread exec). No WinForms, no pythonnet - uses only Max's own timer.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# NATIVE time callback: REMOVED. Two field findings killed it:
#  1. registerTimeCallback only fires while animation time changes - in a
#     static scene requests queue forever and clients time out.
#  2. Registering it needs MAXScript evaluation from Python, and 3ds Max
#     2022 pymxs has no rt.execute.
# pump_once() is kept so a callback CAN be registered manually from the
# MAXScript Listener if ever needed:
#     fn mcpPumpFn = ( python.Execute "import __main__ as _M; _M.pump_once()" )
#     registerTimeCallback mcpPumpFn
# ---------------------------------------------------------------------------


def _register_native_pump():
    """Not available (see above). Always False; kept for API compatibility."""
    _log("native pump skipped (rt.execute missing in Max 2022, "
         "time callback static-scene dead)")
    return False


def _unregister_native_pump():
    _NATIVE_ON[0] = False


def start_max_listener_native():
    """Kept for API compatibility. Native pump is unavailable (see above)."""
    print("[max_listener] Native pump unavailable in Max 2022; "
          "use start_listener().")
    return start_listener()


def start_listener():
    """Start socket thread + first working main-thread pump.

    Pump order: WinForms Timer -> Win32 SetTimer (ctypes) -> blocking loop.
    Success requires a REAL active pump: "already running" is reported only
    when a pump object exists (a stale _MCP_RUNNING flag never masks a
    missing pump - that bug left requests queuing forever with no error).
    """
    global _MCP_TIMER
    pump_ok = False
    try:
        if _MCP_TIMER is not None:
            _ = _MCP_TIMER.Enabled  # raises if the timer object is dead
            pump_ok = True
    except Exception:
        _MCP_TIMER = None
    if pump_ok or _MCP_CTYPES_TIMER is not None:
        print("[max_listener] Already running.")
        return True
    if not start_socket_thread():
        return False
    if _start_winforms_timer():
        _log("START version=%s pump=winforms" % LISTENER_VERSION)
    elif _start_ctypes_timer():
        _log("START version=%s pump=ctypes" % LISTENER_VERSION)
    else:
        _log("START version=%s pump=blocking-fallback" % LISTENER_VERSION)
        print("[max_listener] No timer pump available; blocking loop "
              "(UI busy while serving).")
        start_max_listener_blocking()
        return _MCP_RUNNING
    print("[max_listener] Listening on %s:%d. Max UI responsive."
          % (HOST, PORT))
    print("[max_listener] Call stop_max_listener() to stop.")
    if PANEL_AUTOSHOW:
        show_listener_panel()
    else:
        set_panel_status("Status: RUNNING on %s:%d" % (HOST, PORT))
    return True
def _start_ctypes_timer():
    """Arm a Win32 SetTimer pump (pure stdlib ctypes, no pythonnet).

    SetTimer(NULL hwnd) posts WM_TIMER to the CALLING thread's queue, so
    the callback runs on the Run-Script/main thread. Returns True if armed.
    """
    global _MCP_CTYPES_TIMER
    if _MCP_CTYPES_TIMER is not None:
        return True
    try:
        import ctypes
        from ctypes import wintypes
        # NOTE: ctypes.wintypes in Python 3.7 has no UINT_PTR - use c_size_t.
        TIMERPROC = ctypes.WINFUNCTYPE(None, wintypes.HWND, wintypes.UINT,
                                       ctypes.c_size_t, wintypes.DWORD)

        def _on_wm_timer(hwnd, msg, timer_id, dwtime):
            if not _MCP_RUNNING:
                return
            try:
                _pump_queue_once()
                _CTYPES_TICKS[0] += 1
                if _CTYPES_TICKS[0] % 100 == 0:
                    _log("ctypes heartbeat tick=%d queue_empty=%s"
                         % (_CTYPES_TICKS[0], _MCP_REQUEST_QUEUE.empty()))
            except Exception:
                _log("ctypes tick FAILED:\n" + traceback.format_exc())

        callback = TIMERPROC(_on_wm_timer)
        timer_id = ctypes.windll.user32.SetTimer(None, 1, POLL_INTERVAL_MS,
                                                 callback)
        if not timer_id:
            _log("ctypes SetTimer failed (returned 0)")
            return False
        _MCP_CTYPES_TIMER = (callback, timer_id)  # keep callback alive!
        _log("ctypes SetTimer pump armed (%d ms, id=%s)"
             % (POLL_INTERVAL_MS, timer_id))
        return True
    except Exception:
        _log("ctypes timer FAILED:\n" + traceback.format_exc())
        return False


def _stop_ctypes_timer():
    global _MCP_CTYPES_TIMER
    if _MCP_CTYPES_TIMER is None:
        return
    try:
        import ctypes
        ctypes.windll.user32.KillTimer(None, _MCP_CTYPES_TIMER[1])
    except Exception:
        pass
    _MCP_CTYPES_TIMER = None


def _start_winforms_timer():
    """Create + start the WinForms Timer pump. Returns True on success.

    The Tick drains the queue and logs a heartbeat every 100 ticks, so the
    log file proves which thread fires and whether it has a message loop.
    """
    global _MCP_TIMER
    if _MCP_TIMER is not None:
        return True
    try:
        import clr  # provided by pythonnet inside 3ds Max
        clr.AddReference("System.Windows.Forms")
        from System.Windows.Forms import Timer as WinFormsTimer
        from System.Windows.Forms import Application as WinFormsApp
        from System.Threading import Thread as NetThread
        _log("winforms pump thread: managed_id=%s background=%s "
             "message_loop=%s py_thread=%s"
             % (NetThread.CurrentThread.ManagedThreadId,
                NetThread.CurrentThread.IsBackground,
                bool(WinFormsApp.MessageLoop),
                threading.current_thread().name))
    except Exception as exc:
        _log("WinForms pump unavailable: %s" % exc)
        print("[max_listener] WinForms Timer unavailable: %s" % exc)
        return False
    timer = WinFormsTimer()
    timer.Interval = POLL_INTERVAL_MS

    def _on_tick(sender, args):
        if not _MCP_RUNNING:
            return
        try:
            _pump_queue_once()
            _WIN_TICKS[0] += 1
            if _WIN_TICKS[0] % 100 == 0:
                _log("winforms heartbeat tick=%d queue_empty=%s"
                     % (_WIN_TICKS[0], _MCP_REQUEST_QUEUE.empty()))
        except Exception:
            _log("winforms tick FAILED:\n" + traceback.format_exc())

    timer.Tick += _on_tick
    timer.Start()
    _MCP_TIMER = timer
    _log("winforms timer started (%d ms)" % POLL_INTERVAL_MS)
    return True


# NOTE: native time-callback pump was removed (see _register_native_pump stub
# above). No second definition: the stub is the single source of truth, since
# 3ds Max 2022 pymxs has no rt.execute and the callback never fires in a
# static scene.


def start_max_listener_dotnet():
    """Start: socket thread + WinForms Timer pump + panel (kept for compat)."""
    if _MCP_RUNNING and _MCP_TIMER is not None:
        try:
            _ = _MCP_TIMER.Enabled
            print("[max_listener] Already running.")
            return True
        except Exception:
            pass
    if not start_socket_thread():
        return False
    if not _start_winforms_timer():
        return False
    print("[max_listener] Listening on %s:%d (socket thread + main-thread "
          "Timer, %d ms poll)." % (HOST, PORT, POLL_INTERVAL_MS))
    if PANEL_AUTOSHOW:
        show_listener_panel()
    else:
        set_panel_status("Status: RUNNING on %s:%d" % (HOST, PORT))
    return True


def stop_max_listener():
    """Stop the listener (works for native, Timer and fallback modes)."""
    global _MCP_RUNNING, _MCP_TIMER, _MCP_SERVER_SOCKET, _MCP_SOCKET_THREAD
    _MCP_RUNNING = False
    _unregister_native_pump()
    _stop_ctypes_timer()
    if _MCP_TIMER is not None:
        try:
            _MCP_TIMER.Stop()
        except Exception:
            pass
        _MCP_TIMER = None
    if _MCP_SERVER_SOCKET is not None:
        try:
            _MCP_SERVER_SOCKET.close()  # unblocks the accept thread
        except Exception:
            pass
        _MCP_SERVER_SOCKET = None
    _MCP_SOCKET_THREAD = None
    # Drain any unexecuted requests so a restart starts clean.
    try:
        while True:
            _MCP_REQUEST_QUEUE.get_nowait()
    except queue.Empty:
        pass
    set_panel_status("Status: STOPPED - Max is yours")
    _log("STOPPED")
    print("[max_listener] Stopped.")


# ---------------------------------------------------------------------------
# FALLBACK MODE (no WinForms): socket thread + blocking main-thread pump loop.
# The Max UI will be UNRESPONSIVE while this runs - test use only. Stop it by
# executing stop_max_listener() from another Python context, or via Esc.
# ---------------------------------------------------------------------------
def start_max_listener_blocking():
    """Blocking pump loop (fallback). Runs in the Run-Script main thread.

    Network I/O stays on the background socket thread; this loop only drains
    the request queue, so execution is still main-thread safe. Reuses the
    already-bound socket (rebinding the same port in-process would fail).
    The Max UI is busy until the loop exits. Last resort only.
    """
    global _MCP_RUNNING
    if _MCP_SERVER_SOCKET is None and not start_socket_thread():
        _log("blocking fallback: no socket, aborting")
        return
    _MCP_RUNNING = True
    _log("blocking pump loop entered (UI will be busy)")
    print("[max_listener] Listening on %s:%d (blocking pump loop). "
          "WARNING: Max UI is unresponsive until stop_max_listener() runs."
          % (HOST, PORT))
    try:
        while _MCP_RUNNING:
            try:
                _pump_queue_once()
            except Exception:
                print(traceback.format_exc())
            time.sleep(POLL_INTERVAL_MS / 1000.0)
    finally:
        stop_max_listener()


# ---------------------------------------------------------------------------
# Auto-start when run via Scripting -> Run Script (python.ExecuteFile runs the
# file as __main__) or when dropped into a viewport. Importing this module
# from another script/test does NOT start the server unless already inside
# 3ds Max (pymxs available), where starting is harmless due to the guard.
# ---------------------------------------------------------------------------
if __name__ == "__main__" or _IN_MAX:
    if not _MCP_RUNNING:
        start_listener()
