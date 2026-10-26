"""
Modular MCP GUI plugin for 3ds Max 2022 (Python 3.7 + PySide2 + pymxs).

Run inside 3ds Max 2022 via:  Scripting -> Run Script -> max_mcp_ui.py
(or drag-drop into a viewport).  The panel parents to the Max main window::

    import max_mcp_ui
    max_mcp_ui.show_mcp_ui()

Architecture (decoupled):
  * ``skills_registry`` -- pure scene logic, no sockets, no Qt.
  * ``McpBridgeServer`` (this file) -- daemon ``threading.Thread`` accept
    loop doing ONLY network I/O, plus a main-thread pump queue.
  * ``MaxMcpWindow`` (this file, PySide2) -- controls + skill browser +
    live log console.  Never touched from socket threads except via
    Qt queued signals.

Execution safety:
  Socket threads enqueue (request, holder) and block on holder["event"].
  A QTimer (main/GUI thread -- in Max this IS the scene thread) drains the
  queue via :meth:`McpBridgeServer.pump_once`, so every ``pymxs`` call and
  every ``exec`` runs on the main thread while network waits never block it.

Protocol (newline-delimited JSON, one request -> one response):
  Legacy code request : {"id": "<id>", "code": "<python source>"}
  Skill request       : {"id": "<id>", "skill": "<name>", "params": {...}}
                        ("tool" is accepted as an alias of "skill",
                         "arguments"/"args" as aliases of "params").
  Response            : {"id","status":"ok"|"error","output","result",
                         "error","version"}

Python 3.7 compatible.  PySide2 / qtmax / pymxs imports are guarded so the
transport + registry layers remain importable (and testable) outside Max.
"""

import io
import json
import os
import re
import shutil
import socket
import sys
import threading
import time
import traceback

try:
    import queue
except ImportError:  # pragma: no cover
    import Queue as queue

try:
    from contextlib import redirect_stdout, redirect_stderr
except ImportError:  # pragma: no cover
    redirect_stdout = None
    redirect_stderr = None

# Make sibling skills_registry.py importable when Run-Script execs this file
# with an empty sys.path entry (common inside 3ds Max).
try:
    _HERE = os.path.dirname(os.path.abspath(__file__))
except Exception:
    try:
        _HERE = os.getcwd()
    except Exception:
        _HERE = None
if _HERE and _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    import skills_registry
    _SKILLS_AVAILABLE = True
    _SKILLS_ERROR = None
except Exception as exc:
    skills_registry = None
    _SKILLS_AVAILABLE = False
    _SKILLS_ERROR = exc

try:
    from pymxs import runtime as rt
    _IN_MAX = True
except Exception:
    rt = None
    _IN_MAX = False

try:
    import qtmax
    _QTMAX_AVAILABLE = True
except Exception:
    qtmax = None
    _QTMAX_AVAILABLE = False

try:
    from PySide2 import QtWidgets, QtCore, QtGui
    _QT_AVAILABLE = True
except Exception:
    QtWidgets = None
    QtCore = None
    QtGui = None
    _QT_AVAILABLE = False

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
PUMP_INTERVAL_MS = 300
ACCEPT_TIMEOUT = 0.5
CLIENT_TIMEOUT = 30.0
EXEC_WAIT_TIMEOUT = 120.0
BUFFER_SIZE = 65536
MAX_CODE_SIZE = 1024 * 1024
UI_VERSION = "1.4.1"


# ---------------------------------------------------------------------------
# Persistent exec namespace (mirrors max_listener.py so variables survive
# across code requests).
# ---------------------------------------------------------------------------
_MCP_NAMESPACE = {"__name__": "__mcp_ui__", "__mcp_result__": None}
if rt is not None:
    _MCP_NAMESPACE["rt"] = rt


def _json_safe(value):
    try:
        json.dumps(value)
        return value
    except Exception:
        try:
            return repr(value)
        except Exception:
            return "<unrepresentable>"


def execute_code_snippet(code):
    """Exec *code* in the persistent namespace (CALLER must be main thread)."""
    if not isinstance(code, str):
        return ("error", "", None, "Field 'code' must be a string.")
    if len(code) > MAX_CODE_SIZE:
        return ("error", "", None,
                "Code exceeds MAX_CODE_SIZE (%d bytes)." % MAX_CODE_SIZE)
    _MCP_NAMESPACE["__mcp_result__"] = None
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    try:
        if redirect_stdout is not None:
            with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
                exec(compile(code, "<mcp_ui>", "exec"), _MCP_NAMESPACE)
        else:  # pragma: no cover
            old_out, old_err = sys.stdout, sys.stderr
            sys.stdout, sys.stderr = stdout_buf, stderr_buf
            try:
                exec(compile(code, "<mcp_ui>", "exec"), _MCP_NAMESPACE)
            finally:
                sys.stdout, sys.stderr = old_out, old_err
    except Exception:
        return ("error", stdout_buf.getvalue() + stderr_buf.getvalue(),
                None, traceback.format_exc())
    output = stdout_buf.getvalue() + stderr_buf.getvalue()
    return ("ok", output, _json_safe(_MCP_NAMESPACE.get("__mcp_result__")), None)


# ---------------------------------------------------------------------------
# Transport: daemon-thread listener + main-thread pump queue.
# No Qt here -- usable headless (tests) and inside Max.
#
# Skill-catalog requests (the "tools/list" contract consumed by MCP hosts):
#   {"id", "tools": "list"}                  -> enabled-only catalog + version
#   {"id", "tools": "await_change", "version": N}
#      -> immediate catalog when N is stale, else a parked long-poll that is
#         answered the moment a profile/toggle/reload changes the catalog
#         (bounded by EXEC_WAIT_TIMEOUT; re-watch afterwards).
# ---------------------------------------------------------------------------
_PENDING = object()  # pump sentinel: holder parked, no response to send yet
class McpBridgeServer(object):
    """TCP bridge whose scene work always runs in the pumping (main) thread."""

    def __init__(self, log_fn=None, client_fn=None):
        self._lock = threading.Lock()
        self._running = False
        self._server_socket = None
        self._accept_thread = None
        self._active_clients = 0
        self._total_requests = 0
        self._queue = queue.Queue()
        self.host = DEFAULT_HOST
        self.port = DEFAULT_PORT
        self.log_fn = log_fn or (lambda msg: None)
        self.client_fn = client_fn or (lambda n: None)
        self._watchers = []  # [(holder, req_id)] parked await_change polls
        try:
            if skills_registry is not None:
                skills_registry.on_tools_changed(
                    self._on_registry_tools_changed)
        except Exception:
            pass

    # -- state ----------------------------------------------------------
    @property
    def running(self):
        with self._lock:
            return self._running

    @property
    def active_clients(self):
        with self._lock:
            return self._active_clients

    @property
    def total_requests(self):
        with self._lock:
            return self._total_requests

    @property
    def queue_depth(self):
        return self._queue.qsize()

    def _log(self, msg):
        try:
            self.log_fn(msg)
        except Exception:
            pass

    def _set_clients(self, n):
        try:
            self.client_fn(n)
        except Exception:
            pass

    def _inc_clients(self):
        with self._lock:
            self._active_clients += 1
            n = self._active_clients
        self._set_clients(n)

    def _dec_clients(self):
        with self._lock:
            self._active_clients = max(0, self._active_clients - 1)
            n = self._active_clients
        self._set_clients(n)

    # -- lifecycle ------------------------------------------------------
    def start(self, host=None, port=None):
        """Bind + spawn the daemon accept thread. Returns (ok, message)."""
        with self._lock:
            if self._running:
                return (False, "Already running on %s:%d."
                        % (self.host, self.port))
        host = host or DEFAULT_HOST
        if port is None:
            port = DEFAULT_PORT
        port = int(port)
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((host, port))
        except Exception as exc:
            try:
                srv.close()
            except Exception:
                pass
            return (False, "Cannot bind %s:%d: %s" % (host, port, exc))
        try:
            srv.listen(5)
        except Exception as exc:
            try:
                srv.close()
            except Exception:
                pass
            return (False, "Listen failed: %s" % exc)
        srv.settimeout(ACCEPT_TIMEOUT)
        with self._lock:
            self._server_socket = srv
            self._running = True
            self.host = host
            self.port = port
        worker = threading.Thread(target=self._accept_loop,
                                  name="McpBridgeAccept")
        worker.daemon = True
        with self._lock:
            self._accept_thread = worker
        worker.start()
        self._log("listening on %s:%d (accept thread=%s)"
                  % (host, port, worker.name))
        return (True, "Listening on %s:%d." % (host, port))

    def stop(self):
        with self._lock:
            self._running = False
            srv = self._server_socket
            self._server_socket = None
            self._accept_thread = None
        if srv is not None:
            try:
                srv.close()
            except Exception:
                pass
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass
        with self._lock:
            self._active_clients = 0
        self._set_clients(0)
        # Release parked catalog watchers so no client hangs on stop.
        with self._lock:
            watchers = self._watchers
            self._watchers = []
        for holder, req_id in watchers:
            try:
                holder["response"] = {
                    "id": req_id, "status": "error", "output": "",
                    "result": None, "error": "Server stopped.",
                    "version": UI_VERSION}
            except Exception:
                pass
            try:
                holder["event"].set()
            except Exception:
                pass
        self._log("listener stopped.")

    # -- network (background threads: never touch pymxs/Qt widgets) -----
    def _accept_loop(self):
        while True:
            with self._lock:
                running = self._running
                srv = self._server_socket
            if not running or srv is None:
                break
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except socket.error:
                time.sleep(ACCEPT_TIMEOUT)
                continue
            except Exception:
                time.sleep(ACCEPT_TIMEOUT)
                continue
            worker = threading.Thread(target=self._handle_conn,
                                      args=(conn, addr))
            worker.daemon = True
            worker.start()

    def _read_line(self, conn):
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
        return data.decode("utf-8", errors="replace").splitlines()[0]

    def _send(self, conn, response):
        try:
            conn.sendall((json.dumps(response) + "\n").encode("utf-8"))
        except Exception:
            pass

    def _handle_conn(self, conn, addr):
        self._inc_clients()
        try:
            line = self._read_line(conn)
            if line is None:
                return
            try:
                request = json.loads(line)
            except Exception as exc:
                self._send(conn, {"id": None, "status": "error", "output": "",
                                  "result": None,
                                  "error": "Invalid JSON: %s" % exc,
                                  "version": UI_VERSION})
                return
            if not isinstance(request, dict):
                self._send(conn, {"id": None, "status": "error", "output": "",
                                  "result": None,
                                  "error": "Request must be a JSON object.",
                                  "version": UI_VERSION})
                return
            holder = {"event": threading.Event(), "response": None}
            self._queue.put((request, holder))
            with self._lock:
                self._total_requests += 1
            self._log("request from %s: %s"
                      % (addr, self._summarize(request)))
            if not holder["event"].wait(EXEC_WAIT_TIMEOUT):
                self._send(conn, {
                    "id": request.get("id"), "status": "error", "output": "",
                    "result": None,
                    "error": "Timeout: main 3ds Max thread did not execute "
                             "within %ds. Is 3ds Max frozen or busy?"
                             % int(EXEC_WAIT_TIMEOUT),
                    "version": UI_VERSION})
                self._log("TIMEOUT waiting for main-thread pump.")
                return
            self._send(conn, holder["response"])
        except Exception:
            try:
                self._send(conn, {"id": None, "status": "error", "output": "",
                                  "result": None,
                                  "error": traceback.format_exc(),
                                  "version": UI_VERSION})
            except Exception:
                pass
        finally:
            try:
                conn.close()
            except Exception:
                pass
            self._dec_clients()

    @staticmethod
    def _summarize(request):
        if "code" in request:
            code = str(request.get("code") or "")
            first = code.strip().splitlines()[0] if code.strip() else ""
            return "code: %.80s" % first
        skill = request.get("skill", request.get("tool", "?"))
        return "skill: %s params=%s" % (skill, request.get("params",
                request.get("arguments", request.get("args", {}))))

    # -- advertised skill catalog (MCP tools/list contract) ---------------
    def _tools_list_result(self):
        """Build the enabled-only catalog payload (no id set by caller)."""
        try:
            version = skills_registry.tools_version() \
                if _SKILLS_AVAILABLE else 0
        except Exception:
            version = 0
        try:
            active = skills_registry.get_active_profile() \
                if _SKILLS_AVAILABLE else ""
        except Exception:
            active = ""
        try:
            skills = skills_registry.list_enabled_skills() \
                if _SKILLS_AVAILABLE else []
        except Exception:
            skills = []
        return {"id": None, "status": "ok",
                "output": "tools v%d: %d enabled (profile %r)"
                          % (version, len(skills), active),
                "result": {"version": version,
                           "active_profile": active,
                           "skills": _json_safe(skills)},
                "error": None,
                "version": UI_VERSION,
                "tools_version": version}

    def _answer_watchers(self):
        """Push the fresh catalog to every parked await_change poll."""
        try:
            current = self._tools_list_result()
        except Exception:
            current = None
        with self._lock:
            pending = self._watchers
            self._watchers = []
        for holder, req_id in pending:
            try:
                if current is None:
                    holder["response"] = {
                        "id": req_id, "status": "error", "output": "",
                        "result": None,
                        "error": "Catalog unavailable.",
                        "version": UI_VERSION}
                else:
                    resp = dict(current)
                    resp["id"] = req_id
                    resp["update"] = "changed"
                    holder["response"] = resp
            except Exception:
                try:
                    holder["response"] = {
                        "id": req_id, "status": "error", "output": "",
                        "result": None, "error": "Catalog error.",
                        "version": UI_VERSION}
                except Exception:
                    pass
            try:
                holder["event"].set()
            except Exception:
                pass

    def _on_registry_tools_changed(self, version, count):
        """Registry hook: a toggle/profile/reload changed the catalog."""
        self._answer_watchers()

    # -- main-thread execution ------------------------------------------
    def _execute_request(self, request, holder=None):
        """Build the response dict. CALL ONLY FROM THE MAIN THREAD.

        Returns the _PENDING sentinel (instead of a dict) to park an
        await_change long-poll; pump_once() then leaves the holder waiting.
        """
        req_id = request.get("id")
        try:
            tools_cmd = request.get("tools")
            if isinstance(tools_cmd, str):
                if tools_cmd == "list":
                    resp = self._tools_list_result()
                    resp["id"] = req_id
                    self._log("tools/list served: %s" % resp["output"])
                    return resp
                if tools_cmd == "await_change":
                    try:
                        client_v = int(request.get("version", -1))
                    except Exception:
                        client_v = -1
                    try:
                        cur = skills_registry.tools_version() \
                            if _SKILLS_AVAILABLE else 0
                    except Exception:
                        cur = 0
                    if client_v != cur or holder is None:
                        resp = self._tools_list_result()
                        resp["id"] = req_id
                        return resp
                    with self._lock:
                        self._watchers.append((holder, req_id))
                    self._log("tools watch parked (client v%d, live v%d)."
                              % (client_v, cur))
                    return _PENDING
                return {"id": req_id, "status": "error", "output": "",
                        "result": None,
                        "error": "Unknown tools command %r. Use 'list' or "
                                 "'await_change'." % (tools_cmd,),
                        "version": UI_VERSION}
            if "code" in request and request.get("code") is not None:
                status, output, result, error = execute_code_snippet(
                    request.get("code"))
                self._log("code executed -> %s%s"
                          % (status,
                             (" | " + output.strip().splitlines()[0][:120])
                             if output.strip() else ""))
                return {"id": req_id, "status": status, "output": output,
                        "result": result, "error": error,
                        "version": UI_VERSION}
            skill = request.get("skill", request.get("tool"))
            if skill is not None:
                params = request.get("params",
                           request.get("arguments", request.get("args", {})))
                if params is None:
                    params = {}
                if not _SKILLS_AVAILABLE:
                    error = ("skills_registry unavailable: %s"
                             % (_SKILLS_ERROR,))
                    self._log("skill %s FAILED: %s" % (skill, error))
                    return {"id": req_id, "status": "error", "output": "",
                            "result": None, "error": error,
                            "version": UI_VERSION}
                result = skills_registry.execute_skill(str(skill), params)
                ok = bool(result.get("ok"))
                self._log("skill %s -> %s" % (skill, "ok" if ok else "ERROR"))
                if not ok:
                    self._log("  error: %s"
                              % str(result.get("error"))[:500])
                return {"id": req_id,
                        "status": "ok" if ok else "error",
                        "output": "skill %s ok=%s" % (skill, ok),
                        "result": _json_safe(result),
                        "error": None if ok else result.get("error"),
                        "version": UI_VERSION}
            return {"id": req_id, "status": "error", "output": "",
                    "result": None,
                    "error": "Request needs 'code' or 'skill'. Got keys: %s"
                             % sorted(request.keys()),
                    "version": UI_VERSION}
        except Exception:
            error = traceback.format_exc()
            self._log("request FAILED:\n%s" % error)
            return {"id": req_id, "status": "error", "output": "",
                    "result": None, "error": error, "version": UI_VERSION}

    def pump_once(self):
        """Execute every queued request. CALL ONLY FROM THE MAIN THREAD."""
        count = 0
        while True:
            try:
                request, holder = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                response = self._execute_request(request, holder)
            except Exception:
                response = {
                    "id": None, "status": "error", "output": "",
                    "result": None, "error": traceback.format_exc(),
                    "version": UI_VERSION}
            if response is _PENDING:
                continue  # parked long-poll; holder stays waiting
            try:
                holder["response"] = response
            except Exception:
                pass
            try:
                holder["event"].set()
            except Exception:
                pass
            count += 1
        return count


# ---------------------------------------------------------------------------
# Qt GUI (only defined when PySide2 imports; module stays importable without)
# ---------------------------------------------------------------------------
_MCP_UI_INSTANCE = None


def _max_main_window():
    if _QTMAX_AVAILABLE:
        try:
            return qtmax.GetQMaxMainWindow()
        except Exception:
            return None
    return None


if _QT_AVAILABLE:
    class _BridgeSignals(QtCore.QObject):
        log_line = QtCore.Signal(str)
        clients_changed = QtCore.Signal(int)
        status_changed = QtCore.Signal(bool, str)
        tools_changed = QtCore.Signal(int, int, str)

    class AddSkillDialog(QtWidgets.QDialog):
        """Create (or edit) a custom skill file in custom_skills/.

        The code editor is the source of truth: Save writes its exact text
        to <skill_name>.py after an ast syntax gate.  The fields + parameter
        table only feed the "Generate Template" button.  In edit mode the
        identifier is locked (rename = create a new skill) and the existing
        file text is loaded for editing.
        """

        PARAM_TYPES = ("int", "float", "str", "bool", "node")
        NAME_RE = r"^[A-Za-z_][A-Za-z0-9_]*$"

        def __init__(self, parent=None, edit_path=None):
            super(AddSkillDialog, self).__init__(parent)
            self.edit_path = edit_path
            self.saved_path = None
            self.skill_name = None
            self.setWindowTitle(
                "Edit Skill" if edit_path else "Add New Skill")
            self.setMinimumSize(620, 640)
            self.resize(700, 720)
            self.setModal(True)
            self._build()
            if edit_path:
                self._load_for_edit(edit_path)

        # -- layout --------------------------------------------------
        def _build(self):
            root = QtWidgets.QVBoxLayout(self)
            form = QtWidgets.QFormLayout()
            self.name_edit = QtWidgets.QLineEdit()
            self.name_edit.setPlaceholderText("extrude_walls")
            form.addRow("Skill Name (id):", self.name_edit)
            self.display_edit = QtWidgets.QLineEdit()
            self.display_edit.setPlaceholderText("Extrude Walls")
            form.addRow("Display Name:", self.display_edit)
            self.category_combo = QtWidgets.QComboBox()
            self.category_combo.setEditable(True)
            try:
                cats = sorted(
                    set([s.get("category", "General")
                         for s in skills_registry.list_skills()])
                    | set(["General", "Scene Management",
                           "Geometry & Modifiers", "Materials & Cameras"]))
            except Exception:
                cats = ["General"]
            self.category_combo.addItems(cats)
            form.addRow("Category:", self.category_combo)
            self.desc_edit = QtWidgets.QLineEdit()
            self.desc_edit.setPlaceholderText("What the skill does.")
            form.addRow("Description:", self.desc_edit)
            root.addLayout(form)

            root.addWidget(QtWidgets.QLabel("Parameters:"))
            self.param_table = QtWidgets.QTableWidget(0, 3)
            self.param_table.setHorizontalHeaderLabels(
                ["Name", "Type", "Default"])
            try:
                header = self.param_table.horizontalHeader()
                header.setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
                header.setSectionResizeMode(2, QtWidgets.QHeaderView.Stretch)
            except Exception:
                pass
            root.addWidget(self.param_table, 0)
            prow = QtWidgets.QHBoxLayout()
            add_btn = QtWidgets.QPushButton("Add Param")
            add_btn.clicked.connect(self._on_add_param)
            prow.addWidget(add_btn)
            del_btn = QtWidgets.QPushButton("Remove Selected")
            del_btn.clicked.connect(self._on_del_param)
            prow.addWidget(del_btn)
            prow.addStretch(1)
            root.addLayout(prow)

            root.addWidget(QtWidgets.QLabel("Skill code:"))
            self.editor = QtWidgets.QPlainTextEdit()
            try:
                mono = QtGui.QFontDatabase.systemFont(
                    QtGui.QFontDatabase.FixedFont)
                self.editor.setFont(mono)
            except Exception:
                pass
            self.editor.setPlaceholderText(
                "Press 'Generate Template from Fields' or paste skill code.")
            root.addWidget(self.editor, 1)

            brow = QtWidgets.QHBoxLayout()
            gen_btn = QtWidgets.QPushButton("Generate Template from Fields")
            gen_btn.setToolTip("Fill the editor from the fields above. "
                               "Overwrites editor content after confirm.")
            gen_btn.clicked.connect(self.on_generate_template)
            brow.addWidget(gen_btn)
            brow.addStretch(1)
            buttons = QtWidgets.QDialogButtonBox(
                QtWidgets.QDialogButtonBox.Save |
                QtWidgets.QDialogButtonBox.Cancel)
            buttons.accepted.connect(self.on_save)
            buttons.rejected.connect(self.reject)
            brow.addWidget(buttons)
            root.addLayout(brow)

        # -- params table --------------------------------------------
        def _on_add_param(self, name="", ptype="str", default=""):
            row = self.param_table.rowCount()
            self.param_table.insertRow(row)
            self.param_table.setItem(row, 0, QtWidgets.QTableWidgetItem(name))
            combo = QtWidgets.QComboBox()
            combo.addItems(list(self.PARAM_TYPES))
            try:
                combo.setCurrentIndex(
                    list(self.PARAM_TYPES).index(ptype))
            except Exception:
                pass
            self.param_table.setCellWidget(row, 1, combo)
            self.param_table.setItem(
                row, 2, QtWidgets.QTableWidgetItem(str(default)))

        def _on_del_param(self):
            rows = sorted(set([i.row() for i in
                               self.param_table.selectedIndexes()]),
                          reverse=True)
            for row in rows:
                self.param_table.removeRow(row)

        @staticmethod
        def _parse_default(ptype, text):
            text = (text or "").strip()
            if ptype == "int":
                return int(text) if text else 0
            if ptype == "float":
                return float(text) if text else 0.0
            if ptype == "bool":
                return text.lower() in ("1", "true", "yes", "y", "on")
            return text

        def _current_params(self):
            """Read the table into [{name, type, default, label}]."""
            params = []
            for row in range(self.param_table.rowCount()):
                name_item = self.param_table.item(row, 0)
                def_item = self.param_table.item(row, 2)
                combo = self.param_table.cellWidget(row, 1)
                pname = (name_item.text() if name_item else "").strip()
                if not pname:
                    continue  # skip blank rows silently
                if not re.match(self.NAME_RE, pname):
                    raise ValueError(
                        "Bad parameter name %r (row %d): use letters, "
                        "digits, underscore; must not start with a digit."
                        % (pname, row + 1))
                ptype = str(combo.currentText()) if combo else "str"
                dtext = def_item.text() if def_item else ""
                try:
                    default = self._parse_default(ptype, dtext)
                except Exception:
                    raise ValueError(
                        "Bad default for %r (row %d): %r is not a %s."
                        % (pname, row + 1, dtext, ptype))
                params.append({"name": pname, "type": ptype,
                               "default": default,
                               "label": pname.replace("_", " ").title()})
            seen = set()
            for pdef in params:
                if pdef["name"] in seen:
                    raise ValueError("Duplicate parameter name %r."
                                     % pdef["name"])
                seen.add(pdef["name"])
            return params

        def _field_values(self):
            name = self.name_edit.text().strip()
            if self.edit_path:
                name = self.skill_name or name
            if not re.match(self.NAME_RE, name or ""):
                raise ValueError(
                    "Bad skill name %r: use letters, digits, underscore; "
                    "must not start with a digit." % (self.name_edit.text(),))
            return {
                "name": name,
                "display": self.display_edit.text().strip() or name,
                "category": self.category_combo.currentText().strip()
                or "General",
                "description": self.desc_edit.text().strip(),
            }

        # -- template / load / save ----------------------------------
        def on_generate_template(self):
            try:
                fields = self._field_values()
                params = self._current_params()
            except ValueError as exc:
                QtWidgets.QMessageBox.warning(self, "Skill fields", str(exc))
                return
            if self.editor.toPlainText().strip():
                answer = QtWidgets.QMessageBox.question(
                    self, "Generate Template",
                    "Overwrite the editor content with a fresh template?")
                if answer != QtWidgets.QMessageBox.Yes:
                    return
            try:
                code = skills_registry.skill_template(
                    fields["name"], display=fields["display"],
                    category=fields["category"],
                    description=fields["description"], params=params)
            except Exception:
                QtWidgets.QMessageBox.warning(
                    self, "Generate Template",
                    "Template generation failed:\n%s" % traceback.format_exc())
                return
            self.editor.setPlainText(code)

        def _load_for_edit(self, path):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    code = handle.read()
            except Exception as exc:
                QtWidgets.QMessageBox.warning(
                    self, "Edit Skill", "Cannot read %s:\n%s" % (path, exc))
                return
            self.editor.setPlainText(code)
            stem = os.path.splitext(os.path.basename(path))[0]
            meta = skills_registry.extract_skill_metadata(code) or {}
            self.skill_name = meta.get("name") or stem
            self.name_edit.setText(self.skill_name)
            self.name_edit.setEnabled(False)
            self.name_edit.setToolTip(
                "Locked in edit mode: to rename, create a new skill.")
            self.display_edit.setText(
                str(meta.get("title") or meta.get("name") or stem))
            try:
                idx = self.category_combo.findText(
                    str(meta.get("category") or "General"))
                if idx >= 0:
                    self.category_combo.setCurrentIndex(idx)
                else:
                    self.category_combo.setEditText(
                        str(meta.get("category") or "General"))
            except Exception:
                pass
            self.desc_edit.setText(str(meta.get("description") or ""))
            for pdef in (meta.get("params") or []):
                try:
                    self._on_add_param(
                        str(pdef.get("name", "")),
                        str(pdef.get("type", "str")),
                        "" if pdef.get("default") is None
                        else str(pdef.get("default")))
                except Exception:
                    pass

        def on_save(self):
            if not _SKILLS_AVAILABLE:
                QtWidgets.QMessageBox.warning(
                    self, "Save Skill", "skills_registry unavailable.")
                return
            try:
                fields = self._field_values()
            except ValueError as exc:
                QtWidgets.QMessageBox.warning(self, "Skill fields", str(exc))
                return
            code = self.editor.toPlainText()
            if not code.strip():
                QtWidgets.QMessageBox.warning(
                    self, "Save Skill", "The code editor is empty.")
                return
            ok, err = skills_registry.validate_skill_source(code)
            if not ok:
                QtWidgets.QMessageBox.warning(
                    self, "Save Skill",
                    "Syntax check failed, nothing was written:\n%s" % err)
                return
            try:
                skills_dir = skills_registry.custom_skills_dir()
                if not os.path.isdir(skills_dir):
                    os.makedirs(skills_dir)
            except Exception as exc:
                QtWidgets.QMessageBox.warning(
                    self, "Save Skill",
                    "Cannot create skills folder:\n%s" % exc)
                return
            if self.edit_path:
                path = self.edit_path
            else:
                path = os.path.join(skills_dir, fields["name"] + ".py")
                if os.path.exists(path):
                    answer = QtWidgets.QMessageBox.question(
                        self, "Save Skill",
                        "%s exists. Overwrite?" % os.path.basename(path))
                    if answer != QtWidgets.QMessageBox.Yes:
                        return
            try:
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(code)
            except Exception as exc:
                QtWidgets.QMessageBox.warning(
                    self, "Save Skill", "Write failed:\n%s" % exc)
                return
            self.saved_path = path
            self.skill_name = fields["name"]
            self.accept()

    class MaxMcpWindow(QtWidgets.QWidget):
        """Floating panel: server controls, skills browser, log console."""

        def __init__(self, parent=None):
            super(MaxMcpWindow, self).__init__(parent)
            self.setWindowTitle("MCP Bridge : Server + Skills (3ds Max 2022)")
            self.setMinimumSize(700, 700)
            self.resize(780, 760)
            self.setWindowFlags(QtCore.Qt.Window)
            self.setAttribute(QtCore.Qt.WA_DeleteOnClose, False)

            self.signals = _BridgeSignals()
            self.server = McpBridgeServer(
                log_fn=self._on_bg_log, client_fn=self._on_bg_clients)
            self._param_widgets = {}
            self._current_skill = None
            self._tree_guard = False
            self._profile_guard = False

            self._build_ui()
            self._refresh_skill_tree()
            self._set_status(False, "Stopped.")

            self.signals.log_line.connect(self._append_log)
            self.signals.clients_changed.connect(self._update_clients)
            self.signals.status_changed.connect(self._set_status)
            self.signals.tools_changed.connect(self._on_tools_changed)
            try:
                if _SKILLS_AVAILABLE:
                    skills_registry.on_tools_changed(
                        self._on_registry_tools_changed)
            except Exception:
                pass

            self._pump = QtCore.QTimer(self)
            self._pump.setInterval(PUMP_INTERVAL_MS)
            self._pump.timeout.connect(self._drain_queue)
            self._pump.start()
            self._log("MCP panel ready. Skills: %s"
                      % (len(skills_registry.SKILL_REGISTRY)
                         if _SKILLS_AVAILABLE else "registry missing"))
            try:
                if _SKILLS_AVAILABLE:
                    perr = skills_registry.get_profiles_error()
                    if perr:
                        self._log("profiles note: %s" % perr)
                    self._log("active profile: %r (%d enabled)."
                              % (skills_registry.get_active_profile(),
                                 len([n for n in
                                      skills_registry.SKILL_REGISTRY
                                      if skills_registry.is_skill_enabled(n)])))
            except Exception:
                pass

        # -- background-thread callbacks (signal hop, never touch widgets) --
        def _on_bg_log(self, msg):
            try:
                line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
                self.signals.log_line.emit(line)
            except Exception:
                pass

        def _on_bg_clients(self, n):
            try:
                self.signals.clients_changed.emit(int(n))
            except Exception:
                pass

        def _on_registry_tools_changed(self, version, count):
            # Registry hook (any thread) -> queued signal -> main-thread log.
            try:
                active = skills_registry.get_active_profile()
            except Exception:
                active = ""
            try:
                self.signals.tools_changed.emit(int(version), int(count),
                                                str(active))
            except Exception:
                pass

        def _on_tools_changed(self, version, count, active):
            self._log("[MCP] tools/list_changed broadcast (v%d): %d "
                      "skill(s) advertised (profile %r)."
                      % (version, count, active))

        def _log(self, msg):
            self._append_log("[%s] %s" % (time.strftime("%H:%M:%S"), msg))

        # -- layout ------------------------------------------------------
        def _build_ui(self):
            root = QtWidgets.QVBoxLayout(self)
            root.setContentsMargins(8, 8, 8, 8)
            root.setSpacing(6)

            # Server controls.
            srv = QtWidgets.QGroupBox("MCP Listener")
            grid = QtWidgets.QGridLayout(srv)
            grid.addWidget(QtWidgets.QLabel("Host:"), 0, 0)
            self.host_edit = QtWidgets.QLineEdit(DEFAULT_HOST)
            self.host_edit.setPlaceholderText(DEFAULT_HOST)
            grid.addWidget(self.host_edit, 0, 1)
            grid.addWidget(QtWidgets.QLabel("Port:"), 0, 2)
            self.port_spin = QtWidgets.QSpinBox()
            self.port_spin.setRange(1, 65535)
            self.port_spin.setValue(DEFAULT_PORT)
            grid.addWidget(self.port_spin, 0, 3)
            self.toggle_btn = QtWidgets.QPushButton("Start Server")
            self.toggle_btn.clicked.connect(self.on_toggle_server)
            grid.addWidget(self.toggle_btn, 1, 0, 1, 2)
            status_row = QtWidgets.QHBoxLayout()
            self.status_dot = QtWidgets.QLabel()
            self.status_dot.setFixedSize(14, 14)
            status_row.addWidget(self.status_dot)
            self.status_label = QtWidgets.QLabel("Stopped.")
            status_row.addWidget(self.status_label, 1)
            self.clients_label = QtWidgets.QLabel("Clients: 0")
            status_row.addWidget(self.clients_label)
            grid.addLayout(status_row, 1, 2, 1, 2)
            root.addWidget(srv)

            # Skills: sidebar + dynamic params, plus skill management.
            mid = QtWidgets.QGroupBox("Skills / Actions")
            mid_outer = QtWidgets.QVBoxLayout(mid)
            manage_row = QtWidgets.QHBoxLayout()
            self.add_skill_btn = QtWidgets.QPushButton("Add New Skill")
            self.add_skill_btn.setToolTip(
                "Create a new skill file in custom_skills/ and hot-reload it.")
            self.add_skill_btn.clicked.connect(self.on_add_skill)
            manage_row.addWidget(self.add_skill_btn)
            self.import_skill_btn = QtWidgets.QPushButton("Import Skill (.py)")
            self.import_skill_btn.setToolTip(
                "Copy an external .py skill into custom_skills/ and reload.")
            self.import_skill_btn.clicked.connect(self.on_import_skill)
            manage_row.addWidget(self.import_skill_btn)
            self.edit_skill_btn = QtWidgets.QPushButton("Edit Skill")
            self.edit_skill_btn.setToolTip(
                "Edit the selected custom skill (built-ins are read-only).")
            self.edit_skill_btn.setEnabled(False)
            self.edit_skill_btn.clicked.connect(self.on_edit_skill)
            manage_row.addWidget(self.edit_skill_btn)
            self.folder_btn = QtWidgets.QPushButton("Open Skills Folder")
            self.folder_btn.setToolTip("Open custom_skills/ in Explorer.")
            self.folder_btn.clicked.connect(self.on_open_skills_folder)
            manage_row.addWidget(self.folder_btn)
            self.reload_btn = QtWidgets.QPushButton("Reload Skills")
            self.reload_btn.setToolTip(
                "Re-import custom_skills/ for the UI and MCP dispatch.")
            self.reload_btn.clicked.connect(self.on_reload_skills)
            manage_row.addWidget(self.reload_btn)
            manage_row.addStretch(1)
            mid_outer.addLayout(manage_row)
            # Profile / preset bar: instant switching for UI + MCP catalog.
            prof_row = QtWidgets.QHBoxLayout()
            prof_row.addWidget(QtWidgets.QLabel("Profile:"))
            self.profile_combo = QtWidgets.QComboBox()
            self.profile_combo.setMinimumWidth(160)
            self.profile_combo.setToolTip(
                "Skill preset. Selecting applies it instantly to the "
                "checkboxes and the advertised MCP toolset.")
            self.profile_combo.currentIndexChanged.connect(
                self.on_profile_selected)
            prof_row.addWidget(self.profile_combo, 1)
            self.profile_save_btn = QtWidgets.QPushButton("Save / Overwrite")
            self.profile_save_btn.setToolTip(
                "Store the current checkbox states into the active profile.")
            self.profile_save_btn.clicked.connect(self.on_profile_save)
            prof_row.addWidget(self.profile_save_btn)
            self.profile_new_btn = QtWidgets.QPushButton("New (+)")
            self.profile_new_btn.setToolTip("Create a new named preset.")
            self.profile_new_btn.clicked.connect(self.on_profile_new)
            prof_row.addWidget(self.profile_new_btn)
            self.profile_del_btn = QtWidgets.QPushButton("Delete (-)")
            self.profile_del_btn.setToolTip(
                "Delete the selected preset (All Skills is protected).")
            self.profile_del_btn.clicked.connect(self.on_profile_delete)
            prof_row.addWidget(self.profile_del_btn)
            self.select_all_btn = QtWidgets.QPushButton("Select All")
            self.select_all_btn.setToolTip("Enable every skill.")
            self.select_all_btn.clicked.connect(self.on_select_all)
            prof_row.addWidget(self.select_all_btn)
            self.deselect_all_btn = QtWidgets.QPushButton("Deselect All")
            self.deselect_all_btn.setToolTip("Disable every skill.")
            self.deselect_all_btn.clicked.connect(self.on_deselect_all)
            prof_row.addWidget(self.deselect_all_btn)
            mid_outer.addLayout(prof_row)
            mid_layout = QtWidgets.QHBoxLayout()
            mid_outer.addLayout(mid_layout, 1)
            self.skill_tree = QtWidgets.QTreeWidget()
            self.skill_tree.setColumnCount(2)
            self.skill_tree.setHeaderLabels(["Skill", "Status"])
            self.skill_tree.setMinimumWidth(200)
            # No maximum width: the splitter below lets the user drag the
            # divider, so long category/skill names are never trapped.
            try:
                header = self.skill_tree.header()
                header.setSectionResizeMode(
                    0, QtWidgets.QHeaderView.Stretch)
                header.setSectionResizeMode(
                    1, QtWidgets.QHeaderView.ResizeToContents)
            except Exception:
                pass
            self.skill_tree.itemSelectionChanged.connect(
                self.on_skill_selected)
            self.skill_tree.itemChanged.connect(self.on_tree_item_changed)
            splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
            splitter.addWidget(self.skill_tree)
            mid_layout.addWidget(splitter)

            right = QtWidgets.QWidget()
            right_layout = QtWidgets.QVBoxLayout(right)
            right_layout.setContentsMargins(4, 0, 0, 0)
            self.skill_title = QtWidgets.QLabel("Select a skill")
            font = self.skill_title.font()
            font.setBold(True)
            font.setPointSize(max(font.pointSize(), 10))
            self.skill_title.setFont(font)
            right_layout.addWidget(self.skill_title)
            self.skill_desc = QtWidgets.QLabel("")
            self.skill_desc.setWordWrap(True)
            self.skill_desc.setStyleSheet("color: palette(mid);")
            right_layout.addWidget(self.skill_desc)
            self.param_form = QtWidgets.QFormLayout()
            param_wrap = QtWidgets.QWidget()
            param_wrap.setLayout(self.param_form)
            right_layout.addWidget(param_wrap, 1)
            self.exec_btn = QtWidgets.QPushButton("Execute Locally")
            self.exec_btn.setEnabled(False)
            self.exec_btn.clicked.connect(self.on_execute_locally)
            right_layout.addWidget(self.exec_btn)
            splitter.addWidget(right)
            splitter.setStretchFactor(0, 1)
            splitter.setStretchFactor(1, 2)
            try:
                splitter.setSizes([280, 400])
            except Exception:
                pass
            root.addWidget(mid, 1)

            # Console.
            con = QtWidgets.QGroupBox("Console / Log")
            con_layout = QtWidgets.QVBoxLayout(con)
            self.console = QtWidgets.QPlainTextEdit()
            self.console.setReadOnly(True)
            self.console.setMinimumHeight(150)
            try:
                mono = QtGui.QFontDatabase.systemFont(
                    QtGui.QFontDatabase.FixedFont)
                self.console.setFont(mono)
            except Exception:
                pass
            con_layout.addWidget(self.console, 1)
            btn_row = QtWidgets.QHBoxLayout()
            btn_row.addStretch(1)
            self.clear_btn = QtWidgets.QPushButton("Clear Log")
            self.clear_btn.clicked.connect(self.console.clear)
            btn_row.addWidget(self.clear_btn)
            con_layout.addLayout(btn_row)
            root.addWidget(con, 0)

        # -- status ------------------------------------------------------
        def _set_status(self, active, text):
            color = "#2ecc71" if active else "#e74c3c"
            self.status_dot.setStyleSheet(
                "background-color: %s; border-radius: 7px; "
                "border: 1px solid #555;" % color)
            self.status_label.setText(text)
            self.toggle_btn.setText(
                "Stop Server" if active else "Start Server")

        def _update_clients(self, n):
            self.clients_label.setText("Clients: %d" % int(n))

        def _append_log(self, line):
            try:
                self.console.appendPlainText(str(line))
            except Exception:
                pass

        def _drain_queue(self):
            try:
                self.server.pump_once()
            except Exception:
                self._append_log("pump FAILED:\n%s" % traceback.format_exc())

        # -- server controls ---------------------------------------------
        def on_toggle_server(self):
            if self.server.running:
                self.server.stop()
                self._set_status(False, "Stopped.")
                self._log("server stopped by user.")
                return
            host = self.host_edit.text().strip() or DEFAULT_HOST
            port = int(self.port_spin.value())
            ok, msg = self.server.start(host, port)
            if ok:
                self._set_status(True, "Listening on %s:%d." % (host, port))
                self._log(msg)
            else:
                self._set_status(False, msg)
                self._log("START FAILED: %s" % msg)
                QtWidgets.QMessageBox.warning(self, "MCP Bridge", msg)

        # -- skills browser (checkboxes + live status badges) ------------
        STATUS_COLORS = {"on": "#2ecc71", "off": "#8a8f98", "run": "#f39c12"}

        def _set_skill_visual(self, item, enabled, running=False):
            """Badge cell + name tint. Grey=disabled, green=enabled,
            amber=executing. Never raises."""
            try:
                state = "run" if running else ("on" if enabled else "off")
                item.setText(1, {"on": "● ON", "off": "● OFF",
                                 "run": "● RUN"}[state])
                item.setForeground(
                    1, QtGui.QBrush(QtGui.QColor(self.STATUS_COLORS[state])))
                item.setForeground(
                    0, QtGui.QBrush(QtGui.QColor(
                        "#ffffff" if (enabled or running)
                        else self.STATUS_COLORS["off"])))
            except Exception:
                pass

        def _update_category_state(self, cat_item):
            try:
                states = [cat_item.child(i).checkState(0)
                          for i in range(cat_item.childCount())]
                if not states:
                    return
                if all(s == QtCore.Qt.Checked for s in states):
                    cat_item.setCheckState(0, QtCore.Qt.Checked)
                elif all(s == QtCore.Qt.Unchecked for s in states):
                    cat_item.setCheckState(0, QtCore.Qt.Unchecked)
                else:
                    cat_item.setCheckState(0, QtCore.Qt.PartiallyChecked)
            except Exception:
                pass

        def _refresh_profile_combo(self):
            if not _SKILLS_AVAILABLE:
                return
            self._profile_guard = True
            try:
                combo = self.profile_combo
                combo.blockSignals(True)
                combo.clear()
                combo.addItems(skills_registry.get_all_profiles())
                try:
                    idx = combo.findText(
                        skills_registry.get_active_profile())
                    if idx >= 0:
                        combo.setCurrentIndex(idx)
                except Exception:
                    pass
            except Exception:
                pass
            finally:
                try:
                    self.profile_combo.blockSignals(False)
                except Exception:
                    pass
                self._profile_guard = False

        def _refresh_skill_tree(self):
            self._refresh_profile_combo()
            try:
                self.skill_tree.blockSignals(True)
                self.skill_tree.clear()
                if not _SKILLS_AVAILABLE:
                    item = QtWidgets.QTreeWidgetItem(
                        ["(skills_registry failed to load)", ""])
                    item.setDisabled(True)
                    self.skill_tree.addTopLevelItem(item)
                    self._log("skills_registry import failed: %s"
                              % (_SKILLS_ERROR,))
                    return
                grouped = skills_registry.skills_by_category()
                for category in sorted(grouped):
                    cat_item = QtWidgets.QTreeWidgetItem([category, ""])
                    cat_item.setExpanded(True)
                    font = cat_item.font(0)
                    font.setBold(True)
                    cat_item.setFont(0, font)
                    cat_item.setFlags(cat_item.flags()
                                      | QtCore.Qt.ItemIsUserCheckable
                                      | QtCore.Qt.ItemIsTristate)
                    for skill in sorted(grouped[category],
                                        key=lambda s: s["name"]):
                        label = skill.get("title") or skill["name"]
                        child = QtWidgets.QTreeWidgetItem([label, ""])
                        child.setData(0, QtCore.Qt.UserRole, skill["name"])
                        child.setFlags(child.flags()
                                       | QtCore.Qt.ItemIsUserCheckable)
                        enabled = bool(skill.get("enabled", True))
                        child.setCheckState(
                            0, QtCore.Qt.Checked if enabled
                            else QtCore.Qt.Unchecked)
                        self._set_skill_visual(child, enabled)
                        try:
                            src = skills_registry.skill_source_path(
                                skill["name"])
                            origin = ("custom file:\n%s" % src) if src \
                                else "built-in (skills_registry.py)"
                            child.setToolTip(
                                0, "%s\n%s\n\n%s"
                                % (skill["name"], origin,
                                   skill.get("description", "")))
                        except Exception:
                            try:
                                child.setToolTip(
                                    0, skill.get("description", ""))
                            except Exception:
                                pass
                        cat_item.addChild(child)
                    self._update_category_state(cat_item)
                    self.skill_tree.addTopLevelItem(cat_item)
            finally:
                try:
                    self.skill_tree.blockSignals(False)
                except Exception:
                    pass

        def _find_skill_item(self, name):
            try:
                for ci in range(self.skill_tree.topLevelItemCount()):
                    cat = self.skill_tree.topLevelItem(ci)
                    for ki in range(cat.childCount()):
                        child = cat.child(ki)
                        if str(child.data(0, QtCore.Qt.UserRole)) == name:
                            return child
            except Exception:
                pass
            return None

        def on_tree_item_changed(self, item, column):
            """Checkbox flip -> live registry toggle (MCP syncs instantly)."""
            if column != 0 or not _SKILLS_AVAILABLE:
                return
            if self._tree_guard:
                return
            self._tree_guard = True
            try:
                name = item.data(0, QtCore.Qt.UserRole)
                if not name:
                    # Category header: push its state to every child.
                    state = item.checkState(0)
                    if state == QtCore.Qt.PartiallyChecked:
                        return
                    for i in range(item.childCount()):
                        child = item.child(i)
                        child.setCheckState(0, state)
                        self._apply_child_check(child, state
                                                == QtCore.Qt.Checked)
                else:
                    self._apply_child_check(
                        item, item.checkState(0) == QtCore.Qt.Checked)
                    parent = item.parent()
                    if parent is not None:
                        self._update_category_state(parent)
            finally:
                self._tree_guard = False

        def _apply_child_check(self, child, enabled):
            try:
                name = str(child.data(0, QtCore.Qt.UserRole) or "")
            except Exception:
                return
            if not name:
                return
            try:
                ok, msg = skills_registry.set_skill_enabled(name, enabled)
            except Exception:
                self._log("toggle FAILED:\n%s" % traceback.format_exc())
                return
            try:
                live = skills_registry.is_skill_enabled(name)
            except Exception:
                live = enabled
            self._set_skill_visual(child, live)
            self._log("[TOGGLE] %s" % msg)
            if not ok:
                QtWidgets.QMessageBox.warning(self, "Skill toggle", msg)

        def on_skill_selected(self):
            items = self.skill_tree.selectedItems()
            if not items:
                return
            name = items[0].data(0, QtCore.Qt.UserRole)
            if not name:
                return  # category header
            self._show_skill(str(name))

        def _clear_param_form(self):
            while self.param_form.rowCount():
                self.param_form.removeRow(0)
            self._param_widgets = {}

        def _show_skill(self, name):
            spec = skills_registry.get_skill(name) if _SKILLS_AVAILABLE else None
            self._clear_param_form()
            try:
                self.edit_skill_btn.setEnabled(False)
            except Exception:
                pass
            if spec is None:
                self.skill_title.setText("Unknown skill: %s" % name)
                self.exec_btn.setEnabled(False)
                return
            self._current_skill = name
            try:
                enabled_now = skills_registry.is_skill_enabled(name)
            except Exception:
                enabled_now = True
            title = spec.title or name
            if not enabled_now:
                title += " (disabled)"
            self.skill_title.setText(title)
            self.skill_desc.setText(spec.description)
            try:
                src = skills_registry.skill_source_path(name)
                if src:
                    self.edit_skill_btn.setEnabled(True)
                    self.edit_skill_btn.setToolTip("Edit %s" % src)
            except Exception:
                pass
            for pdef in spec.params:
                widget = self._make_param_widget(pdef)
                label = pdef.get("label") or pdef["name"]
                if pdef.get("help"):
                    widget.setToolTip(str(pdef["help"]))
                self.param_form.addRow(label + ":", widget)
                self._param_widgets[pdef["name"]] = (pdef, widget)
            self.exec_btn.setEnabled(True)
            self._log("selected skill: %s (%s)" % (name, spec.category))

        def _make_param_widget(self, pdef):
            ptype = pdef.get("type", "str")
            default = pdef.get("default")
            if ptype == "bool":
                box = QtWidgets.QCheckBox()
                box.setChecked(bool(default))
                return box
            if ptype == "int":
                spin = QtWidgets.QSpinBox()
                spin.setRange(int(pdef.get("min", -1000000)),
                              int(pdef.get("max", 1000000)))
                try:
                    spin.setValue(int(default or 0))
                except Exception:
                    spin.setValue(0)
                return spin
            if ptype == "float":
                spin = QtWidgets.QDoubleSpinBox()
                spin.setDecimals(3)
                spin.setRange(float(pdef.get("min", -1e9)),
                              float(pdef.get("max", 1e9)))
                try:
                    spin.setValue(float(default or 0.0))
                except Exception:
                    spin.setValue(0.0)
                return spin
            if ptype == "choice":
                combo = QtWidgets.QComboBox()
                choices = [str(c) for c in (pdef.get("choices") or [])]
                combo.addItems(choices)
                try:
                    idx = choices.index(str(default))
                    combo.setCurrentIndex(idx)
                except Exception:
                    pass
                combo.setEditable(False)
                return combo
            if ptype == "node":
                edit = QtWidgets.QLineEdit()
                edit.setText("" if default is None else str(default))
                edit.setPlaceholderText("Scene node name")
                return edit
            edit = QtWidgets.QLineEdit()
            edit.setText("" if default is None else str(default))
            return edit

        def _collect_params(self):
            params = {}
            for pname, (pdef, widget) in self._param_widgets.items():
                ptype = pdef.get("type", "str")
                try:
                    if ptype == "bool":
                        params[pname] = bool(widget.isChecked())
                    elif ptype == "int":
                        params[pname] = int(widget.value())
                    elif ptype == "float":
                        params[pname] = float(widget.value())
                    elif ptype == "choice":
                        params[pname] = str(widget.currentText())
                    elif ptype == "node":
                        params[pname] = str(widget.text())
                    else:
                        params[pname] = str(widget.text())
                except Exception:
                    params[pname] = pdef.get("default")
            return params

        def on_execute_locally(self):
            if not self._current_skill:
                return
            if not _SKILLS_AVAILABLE:
                self._log("cannot execute: skills_registry unavailable.")
                return
            params = self._collect_params()
            try:
                disabled = not skills_registry.is_skill_enabled(
                    self._current_skill)
            except Exception:
                disabled = False
            if disabled:
                self._log("[LOCAL] override: %r is disabled by profile %r; "
                          "explicit click executes anyway (MCP stays blocked)."
                          % (self._current_skill,
                             skills_registry.get_active_profile()))
            self._log("execute locally: %s %s"
                      % (self._current_skill, params))
            item = self._find_skill_item(self._current_skill)
            if item is not None:
                self._set_skill_visual(item, True, running=True)
            try:
                # Already on the main thread (button click) -- direct call
                # is scene-safe.  execute_skill never raises, but guard
                # anyway.  enforce_enabled=False: an explicit local click
                # bypasses the profile gate that remote dispatch enforces.
                result = skills_registry.execute_skill(
                    self._current_skill, params, enforce_enabled=False)
            except Exception:
                result = {"ok": False, "error": traceback.format_exc()}
            if item is not None:
                try:
                    live = skills_registry.is_skill_enabled(
                        self._current_skill)
                except Exception:
                    live = True
                self._set_skill_visual(item, live)
            ok = bool(result.get("ok"))
            self._log("result ok=%s: %s"
                      % (ok, json.dumps(result)[:1000]))
            if not ok and result.get("error"):
                QtWidgets.QMessageBox.warning(
                    self, "Skill failed", str(result["error"])[:1500])

        # -- profiles (instant UI + MCP switching) -----------------------
        def on_profile_selected(self, index):
            if self._profile_guard or not _SKILLS_AVAILABLE:
                return
            try:
                name = str(self.profile_combo.itemText(index))
            except Exception:
                return
            if not name:
                return
            try:
                report = skills_registry.apply_profile(name)
            except Exception:
                self._log("profile apply FAILED:\n%s" % traceback.format_exc())
                return
            if not report.get("ok"):
                self._log("profile FAILED: %s" % report.get("error"))
                QtWidgets.QMessageBox.warning(
                    self, "Profile", str(report.get("error"))[:800])
                self._refresh_profile_combo()
                return
            self._refresh_skill_tree()
            if self._current_skill:
                self._select_skill(self._current_skill)
            self._log("[PROFILE] Switched to %r (%d active skills)."
                      % (report["profile"], report["total"]))
            for dropped in report.get("dropped", []):
                self._log("profile note: unknown skill %r ignored."
                          % dropped)
            if report.get("persist_warning"):
                self._log("profile warning: active choice may not persist: "
                          "%s" % report["persist_warning"])

        def on_profile_save(self):
            if not _SKILLS_AVAILABLE:
                self._log("profile save: skills_registry unavailable.")
                return
            name = str(self.profile_combo.currentText())
            if name == skills_registry.PROFILE_ALL:
                QtWidgets.QMessageBox.information(
                    self, "Save Profile",
                    "%r is dynamic (always all skills) and cannot be "
                    "overwritten. Use New (+) for a custom preset."
                    % skills_registry.PROFILE_ALL)
                return
            try:
                checked = [n for n in skills_registry.SKILL_REGISTRY
                           if skills_registry.is_skill_enabled(n)]
                ok, msg = skills_registry.save_profile(name, checked)
            except Exception:
                self._log("profile save FAILED:\n%s" % traceback.format_exc())
                return
            self._log("[PROFILE] %s" % msg)
            if not ok:
                QtWidgets.QMessageBox.warning(self, "Save Profile", msg)

        def on_profile_new(self):
            if not _SKILLS_AVAILABLE:
                return
            try:
                text, accepted = QtWidgets.QInputDialog.getText(
                    self, "New Profile", "Profile name:")
            except Exception:
                self._log("profile dialog FAILED:\n%s"
                          % traceback.format_exc())
                return
            if not accepted:
                return
            name = str(text).strip()
            if not name:
                QtWidgets.QMessageBox.warning(
                    self, "New Profile", "Name must not be empty.")
                return
            if name == skills_registry.PROFILE_ALL:
                QtWidgets.QMessageBox.warning(
                    self, "New Profile",
                    "%r is reserved." % skills_registry.PROFILE_ALL)
                return
            try:
                exists = name in skills_registry.get_all_profiles()
            except Exception:
                exists = False
            if exists:
                answer = QtWidgets.QMessageBox.question(
                    self, "New Profile",
                    "%r exists. Overwrite with current checkboxes?" % name)
                if answer != QtWidgets.QMessageBox.Yes:
                    return
            try:
                checked = [n for n in skills_registry.SKILL_REGISTRY
                           if skills_registry.is_skill_enabled(n)]
                ok, msg = skills_registry.save_profile(name, checked)
            except Exception:
                self._log("profile save FAILED:\n%s" % traceback.format_exc())
                return
            self._refresh_profile_combo()
            try:
                idx = self.profile_combo.findText(name)
                if idx >= 0:
                    self.profile_combo.setCurrentIndex(idx)
            except Exception:
                pass
            self._log("[PROFILE] %s" % msg)
            if not ok:
                QtWidgets.QMessageBox.warning(self, "New Profile", msg)

        def on_profile_delete(self):
            if not _SKILLS_AVAILABLE:
                return
            name = str(self.profile_combo.currentText())
            if name == skills_registry.PROFILE_ALL:
                QtWidgets.QMessageBox.warning(
                    self, "Delete Profile",
                    "%r is built-in and cannot be deleted."
                    % skills_registry.PROFILE_ALL)
                return
            answer = QtWidgets.QMessageBox.question(
                self, "Delete Profile",
                "Delete preset %r?" % name)
            if answer != QtWidgets.QMessageBox.Yes:
                return
            try:
                ok, msg = skills_registry.delete_profile(name)
            except Exception:
                self._log("profile delete FAILED:\n%s"
                          % traceback.format_exc())
                return
            self._refresh_skill_tree()
            self._log("[PROFILE] %s" % msg)
            if not ok:
                QtWidgets.QMessageBox.warning(self, "Delete Profile", msg)

        def on_select_all(self):
            if not _SKILLS_AVAILABLE:
                return
            try:
                ok, msg = skills_registry.set_all_enabled(True)
            except Exception:
                self._log("select all FAILED:\n%s" % traceback.format_exc())
                return
            self._refresh_skill_tree()
            self._log("[PROFILE] %s" % msg)

        def on_deselect_all(self):
            if not _SKILLS_AVAILABLE:
                return
            try:
                ok, msg = skills_registry.set_all_enabled(False)
            except Exception:
                self._log("deselect all FAILED:\n%s" % traceback.format_exc())
                return
            self._refresh_skill_tree()
            self._log("[PROFILE] %s" % msg)

        # -- skill management (custom_skills/ + hot-reload) ---------------
        def _select_skill(self, name):
            """Select the tree item for *name*. Returns True if found."""
            try:
                for ci in range(self.skill_tree.topLevelItemCount()):
                    cat = self.skill_tree.topLevelItem(ci)
                    for ki in range(cat.childCount()):
                        child = cat.child(ki)
                        if str(child.data(0, QtCore.Qt.UserRole)) == name:
                            self.skill_tree.setCurrentItem(child)
                            try:
                                self.skill_tree.scrollToItem(child)
                            except Exception:
                                pass
                            return True
            except Exception:
                pass
            return False

        def _hot_reload(self, reselect=None):
            """Reload custom skills, refresh the tree, log the outcome."""
            if not _SKILLS_AVAILABLE:
                self._log("reload: skills_registry unavailable.")
                return
            try:
                before = set(skills_registry.SKILL_REGISTRY)
            except Exception:
                before = set()
            try:
                report = skills_registry.reload_skills()
            except Exception:
                self._log("reload FAILED:\n%s" % traceback.format_exc())
                return
            try:
                after = set(skills_registry.SKILL_REGISTRY)
            except Exception:
                after = set()
            added = sorted(after - before)
            removed = sorted(before - after)
            self._refresh_skill_tree()
            if reselect and reselect in after:
                self._select_skill(reselect)
            elif self._current_skill in after:
                self._select_skill(self._current_skill)
            parts = []
            if added:
                parts.append("+%d added (%s)" % (len(added), ", ".join(added)))
            if removed:
                parts.append("-%d removed (%s)"
                             % (len(removed), ", ".join(removed)))
            loaded = [l for l in report.get("loaded", [])
                      if l not in added and "(no skills" not in l]
            if loaded:
                parts.append("updated: %s" % ", ".join(loaded))
            self._log("skills reloaded from %s: %s; total=%d"
                      % (report.get("dir"), "; ".join(parts) or "no changes",
                         report.get("total", len(after))))
            for fname, err in sorted(report.get("errors", {}).items()):
                self._log("skill ERROR %s:\n%s" % (fname, str(err)[:800]))
            self._log("MCP dispatch now serves %d skill(s) "
                      "(dynamic lookup, no restart needed)." % len(after))

        def on_add_skill(self):
            if not _SKILLS_AVAILABLE:
                self._log("add skill: skills_registry unavailable.")
                return
            try:
                dlg = AddSkillDialog(self)
            except Exception:
                self._log("add skill dialog FAILED:\n%s"
                          % traceback.format_exc())
                return
            if dlg.exec_() != QtWidgets.QDialog.Accepted:
                return
            self._log("skill saved: %s" % (dlg.saved_path or dlg.skill_name))
            self._hot_reload(reselect=dlg.skill_name)

        def on_edit_skill(self):
            if not _SKILLS_AVAILABLE or not self._current_skill:
                return
            try:
                path = skills_registry.skill_source_path(self._current_skill)
            except Exception:
                path = None
            if not path:
                QtWidgets.QMessageBox.information(
                    self, "Edit Skill",
                    "%s is a built-in skill (lives in skills_registry.py) "
                    "and cannot be edited here. Use Add New Skill to create "
                    "your own variant." % self._current_skill)
                return
            try:
                dlg = AddSkillDialog(self, edit_path=path)
            except Exception:
                self._log("edit skill dialog FAILED:\n%s"
                          % traceback.format_exc())
                return
            if dlg.exec_() != QtWidgets.QDialog.Accepted:
                return
            self._log("skill updated: %s" % path)
            self._hot_reload(reselect=self._current_skill)

        def on_import_skill(self):
            if not _SKILLS_AVAILABLE:
                self._log("import skill: skills_registry unavailable.")
                return
            src, _ = QtWidgets.QFileDialog.getOpenFileName(
                self, "Import Skill (.py)", "",
                "Python Skills (*.py)")
            if not src:
                return
            try:
                with open(src, "r", encoding="utf-8") as handle:
                    code = handle.read()
            except Exception as exc:
                self._log("import FAILED (cannot read %s): %s" % (src, exc))
                QtWidgets.QMessageBox.warning(
                    self, "Import Skill", "Cannot read file:\n%s" % exc)
                return
            ok, err = skills_registry.validate_skill_source(code)
            if not ok:
                self._log("import REJECTED %s: %s"
                          % (os.path.basename(src), err))
                QtWidgets.QMessageBox.warning(
                    self, "Import Skill",
                    "Syntax check failed, nothing was copied:\n%s" % err)
                return
            try:
                skills_dir = skills_registry.custom_skills_dir()
                if not os.path.isdir(skills_dir):
                    os.makedirs(skills_dir)
            except Exception as exc:
                self._log("import FAILED (skills folder): %s" % exc)
                return
            dest = os.path.join(skills_dir, os.path.basename(src))
            if os.path.exists(dest):
                answer = QtWidgets.QMessageBox.question(
                    self, "Import Skill",
                    "%s exists in custom_skills/. Overwrite?"
                    % os.path.basename(dest))
                if answer != QtWidgets.QMessageBox.Yes:
                    return
            try:
                shutil.copy(src, dest)
            except Exception as exc:
                self._log("import FAILED (copy): %s" % exc)
                QtWidgets.QMessageBox.warning(
                    self, "Import Skill", "Copy failed:\n%s" % exc)
                return
            self._log("skill imported: %s" % dest)
            self._hot_reload()

        def on_open_skills_folder(self):
            if not _SKILLS_AVAILABLE:
                self._log("skills folder: skills_registry unavailable.")
                return
            try:
                skills_dir = skills_registry.custom_skills_dir()
                if not os.path.isdir(skills_dir):
                    os.makedirs(skills_dir)
            except Exception as exc:
                self._log("skills folder FAILED: %s" % exc)
                return
            try:
                os.startfile(skills_dir)  # Windows-only host (3ds Max)
                self._log("opened skills folder: %s" % skills_dir)
            except Exception as exc:
                self._log("open folder FAILED %s: %s" % (skills_dir, exc))
                QtWidgets.QMessageBox.warning(
                    self, "Open Skills Folder",
                    "Cannot open Explorer at:\n%s\n%s" % (skills_dir, exc))

        def on_reload_skills(self):
            self._hot_reload(reselect=self._current_skill)

        def closeEvent(self, event):
            try:
                if self.server.running:
                    self.server.stop()
            except Exception:
                pass
            try:
                self._pump.stop()
            except Exception:
                pass
            super(MaxMcpWindow, self).closeEvent(event)

    def show_mcp_ui():
        """Show (or raise) the singleton panel parented to Max when possible."""
        global _MCP_UI_INSTANCE
        try:
            if _MCP_UI_INSTANCE is not None:
                try:
                    _MCP_UI_INSTANCE.show()
                    _MCP_UI_INSTANCE.raise_()
                    _MCP_UI_INSTANCE.activateWindow()
                    return _MCP_UI_INSTANCE
                except Exception:
                    _MCP_UI_INSTANCE = None
            parent = _max_main_window()
            _MCP_UI_INSTANCE = MaxMcpWindow(parent)
            _MCP_UI_INSTANCE.show()
            return _MCP_UI_INSTANCE
        except Exception:
            traceback.print_exc()
            return None

    def close_mcp_ui():
        global _MCP_UI_INSTANCE
        if _MCP_UI_INSTANCE is not None:
            try:
                _MCP_UI_INSTANCE.close()
            except Exception:
                pass
            _MCP_UI_INSTANCE = None

    def toggle_mcp_ui():
        """Toggle the singleton panel (macro/toolbar entry point).

        Shows (or raises) the window when hidden, hides it when visible.
        Hiding preserves the instance and the running listener; closing
        via the window [X] still stops the server through closeEvent.
        Never opens a duplicate window.
        """
        global _MCP_UI_INSTANCE
        try:
            if _MCP_UI_INSTANCE is not None:
                try:
                    if _MCP_UI_INSTANCE.isVisible():
                        _MCP_UI_INSTANCE.hide()
                        return None
                    _MCP_UI_INSTANCE.show()
                    _MCP_UI_INSTANCE.raise_()
                    _MCP_UI_INSTANCE.activateWindow()
                    return _MCP_UI_INSTANCE
                except Exception:
                    _MCP_UI_INSTANCE = None
            return show_mcp_ui()
        except Exception:
            traceback.print_exc()
            return None

else:  # pragma: no cover - no Qt (headless tests / bare interpreter)
    def show_mcp_ui():
        print("[max_mcp_ui] PySide2 unavailable; GUI cannot be shown.")
        return None

    def close_mcp_ui():
        return None

    def toggle_mcp_ui():
        print("[max_mcp_ui] PySide2 unavailable; GUI cannot be shown.")
        return None


# ---------------------------------------------------------------------------
# Headless self-test: python max_mcp_ui.py --test
# Exercises the registry + socket transport WITHOUT 3ds Max or Qt by
# starting the server on an ephemeral port and driving pump_once() manually.
# ---------------------------------------------------------------------------
def _self_test():
    import uuid

    print("== max_mcp_ui self-test ==")
    assert _SKILLS_AVAILABLE, "skills_registry failed: %s" % (_SKILLS_ERROR,)
    # Registry starts empty by design (no built-ins); the test registers
    # one temp skill per well-known category and removes them afterwards.
    _temps = [
        ("_selftest_geom", "Geometry & Modifiers"),
        ("_selftest_mat", "Materials & Cameras"),
        ("_selftest_scene", "Scene Management"),
    ]
    for _tname, _tcat in _temps:
        try:
            if _tname in skills_registry.SKILL_REGISTRY:
                del skills_registry.SKILL_REGISTRY[_tname]
            skills_registry._ENABLED.discard(_tname)
        except Exception:
            pass
    for _tname, _tcat in _temps:
        def _tfunc(echo="hi", _n=_tname):
            return {"ok": True, "echo": echo, "name": _n}
        _tfunc.__name__ = _tname
        skills_registry.register_skill(
            name=_tname, category=_tcat,
            description="Self-test skill (%s)." % _tcat,
            params=[{"name": "echo", "type": "str", "default": "hi",
                     "label": "Echo"}])(_tfunc)
    skills_registry.set_all_enabled(True)
    names = sorted(skills_registry.SKILL_REGISTRY)
    print("test skills registered: %s" % names)
    for _tname, _tcat in _temps:
        assert _tname in names, _tname
    # Empty categories stay visible for manual sorting.
    grouped = skills_registry.skills_by_category()
    for _tname, _tcat in _temps:
        assert _tcat in grouped, _tcat
    # Customs present in custom_skills/ stay loaded alongside the temps;
    # "All Skills" therefore advertises baseline + 3.
    _n_all = len(skills_registry.SKILL_REGISTRY)

    logs = []
    server = McpBridgeServer(log_fn=logs.append, client_fn=lambda n: None)
    ok, msg = server.start("127.0.0.1", 0)  # ephemeral port
    assert ok, msg
    port = server._server_socket.getsockname()[1]
    print("server on 127.0.0.1:%d" % port)

    def send(obj, timeout=10.0):
        payload = (json.dumps(obj) + "\n").encode("utf-8")
        with socket.create_connection(("127.0.0.1", port),
                                      timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(payload)
            data = b""
            # Pump from a helper thread: the test's main thread IS the pump.
            pump_stop = threading.Event()

            def _pump():
                while not pump_stop.is_set():
                    try:
                        server.pump_once()
                    except Exception:
                        pass
                    time.sleep(0.02)

            helper = threading.Thread(target=_pump)
            helper.daemon = True
            helper.start()
            try:
                while not data.endswith(b"\n"):
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    data += chunk
            finally:
                pump_stop.set()
                helper.join(timeout=2.0)
        assert data.strip(), "empty response"
        return json.loads(data.decode("utf-8"))

    # 1. skill dispatch (dry-run outside Max).
    resp = send({"id": uuid.uuid4().hex, "skill": "_selftest_scene",
                 "params": {"echo": "hello"}})
    assert resp.get("status") == "ok", resp
    assert resp["result"].get("ok") is True, resp
    assert resp["result"].get("echo") == "hello", resp
    print("PASS skill dispatch: _selftest_scene")

    # 2. tool alias + arguments alias.
    resp = send({"id": uuid.uuid4().hex, "tool": "_selftest_geom",
                 "arguments": {"echo": "alias"}})
    assert resp.get("status") == "ok", resp
    assert resp["result"].get("echo") == "alias", resp
    print("PASS tool/arguments aliases")

    # 3. legacy code path.
    resp = send({"id": uuid.uuid4().hex,
                 "code": "print(\"hello-ui\")\n__mcp_result__ = 42"})
    assert resp.get("status") == "ok" and resp.get("result") == 42, resp
    assert "hello-ui" in (resp.get("output") or ""), resp
    print("PASS legacy code exec")

    # 4. unknown skill -> structured error (not a hang).
    resp = send({"id": uuid.uuid4().hex, "skill": "nope"})
    assert resp.get("status") == "error", resp
    print("PASS unknown-skill error")

    # 5. malformed request -> structured error.
    resp = send({"id": uuid.uuid4().hex})
    assert resp.get("status") == "error", resp
    print("PASS malformed-request error")

    # 6. profiles: defaults exist, apply filters the advertised catalog.
    # The on-disk shell list may reference retired names, so the test
    # overwrites it with the temp skills first (file is backed up).
    prof_path = skills_registry.profiles_path()
    prof_backup = None
    if os.path.exists(prof_path):
        with open(prof_path, "r", encoding="utf-8") as _f:
            prof_backup = _f.read()
    try:
        assert skills_registry.get_active_profile() == "All Skills"
        assert skills_registry.get_all_profiles() == [
            "All Skills", "Floor Plan Shell", "Minimal / None"], \
            skills_registry.get_all_profiles()
        ok, msg = skills_registry.save_profile(
            "Floor Plan Shell", ["_selftest_geom", "_selftest_scene"])
        assert ok, msg
        v0 = skills_registry.tools_version()
        rep = skills_registry.apply_profile("Floor Plan Shell")
        assert rep["ok"] and rep["total"] == 2, rep
        assert sorted(rep["enabled"]) == [
            "_selftest_geom", "_selftest_scene"], rep
        assert rep["version"] == v0 + 1, rep
        print("PASS profile apply: Floor Plan Shell (2 active)")

        # 7. tools/list advertises only enabled skills.
        resp = send({"id": uuid.uuid4().hex, "tools": "list"})
        assert resp.get("status") == "ok", resp
        assert resp["result"]["version"] == v0 + 1, resp
        assert resp["result"]["active_profile"] == "Floor Plan Shell", resp
        names = sorted(s["name"] for s in resp["result"]["skills"])
        assert names == ["_selftest_geom", "_selftest_scene"], names
        print("PASS tools/list: enabled-only catalog")

        # 8. remote dispatch refuses disabled skills; local override works.
        ok, msg = skills_registry.save_profile(
            "Test Solo", ["_selftest_geom"])
        assert ok, msg
        rep = skills_registry.apply_profile("Test Solo")
        assert rep["ok"] and rep["total"] == 1, rep
        v1 = rep["version"]
        resp = send({"id": uuid.uuid4().hex,
                     "skill": "_selftest_scene", "params": {}})
        assert resp.get("status") == "error", resp
        assert "disabled" in (resp.get("error") or ""), resp
        print("PASS disabled skill refused over the wire")
        direct = skills_registry.execute_skill(
            "_selftest_scene", {}, enforce_enabled=False)
        assert direct.get("ok") is True, direct
        print("PASS explicit local override executes")

        # 9. await_change: stale version answers at once; live version parks
        # until the next catalog change, then wakes with the new catalog.
        resp = send({"id": uuid.uuid4().hex, "tools": "await_change",
                     "version": v1 - 1})
        assert resp.get("status") == "ok", resp
        assert resp["result"]["version"] == v1, resp
        print("PASS await_change: stale version answers immediately")
        box = {}
        watch = threading.Thread(
            target=lambda: box.update(resp=send(
                {"id": uuid.uuid4().hex, "tools": "await_change",
                 "version": v1})))
        watch.daemon = True
        watch.start()
        deadline = time.time() + 5.0
        while not server._watchers and time.time() < deadline:
            time.sleep(0.02)
        assert server._watchers, "watcher never parked"
        rep = skills_registry.apply_profile("All Skills")
        assert rep["ok"] and rep["total"] == _n_all, rep
        watch.join(timeout=10.0)
        assert not watch.is_alive(), "watcher never woke"
        resp = box["resp"]
        assert resp.get("status") == "ok", resp
        assert resp.get("update") == "changed", resp
        assert resp["result"]["version"] == v1 + 1, resp
        assert len(resp["result"]["skills"]) == _n_all, resp
        print("PASS await_change: parked watch wakes on profile switch")

        # 10. profile CRUD guards.
        ok, msg = skills_registry.delete_profile("All Skills")
        assert not ok, msg
        print("PASS All Skills deletion refused")
        ok, msg = skills_registry.save_profile("All Skills", [])
        assert not ok, msg
        print("PASS All Skills overwrite refused")
        ok, msg = skills_registry.save_profile(
            "Test Preset", ["_selftest_geom", "nope"])
        assert not ok and "nope" in msg, msg
        print("PASS unknown skill names rejected on save")
        ok, msg = skills_registry.save_profile(
            "Test Solo", ["_selftest_mat"])
        assert ok, msg
        rep = skills_registry.apply_profile("Test Solo")
        assert rep["ok"] and rep["enabled"] == ["_selftest_mat"], rep
        ok, msg = skills_registry.delete_profile("Test Preset")
        assert not ok, msg  # never saved (rejected above)
        ok, msg = skills_registry.delete_profile("Test Solo")
        assert ok, msg
        assert skills_registry.get_active_profile() == "All Skills", \
            skills_registry.get_active_profile()
        print("PASS profile save/apply/delete round-trip")
    finally:
        try:
            if prof_backup is None:
                if os.path.exists(prof_path):
                    os.remove(prof_path)
            else:
                with open(prof_path, "w", encoding="utf-8") as _f:
                    _f.write(prof_backup)
        except Exception:
            pass
        for _tname, _tcat in _temps:
            try:
                if _tname in skills_registry.SKILL_REGISTRY:
                    del skills_registry.SKILL_REGISTRY[_tname]
            except Exception:
                pass
            try:
                skills_registry._ENABLED.discard(_tname)
            except Exception:
                pass
        skills_registry._PROFILES = None
        skills_registry.apply_profile("All Skills")
        skills_registry._TOOLS_VERSION[0] = 0

    assert server.active_clients == 0, server.active_clients
    assert server.total_requests == 9, server.total_requests
    server.stop()
    assert server.queue_depth == 0
    print("log lines: %d" % len(logs))
    print("ALL SELF-TESTS PASSED.")
    return 0


if __name__ == "__main__":
    if "--test" in sys.argv:
        raise SystemExit(_self_test())
    if _QT_AVAILABLE:
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        win = show_mcp_ui()
        if win is not None and win.parent() is None:
            raise SystemExit(app.exec_())
    else:
        print("PySide2 not available; run with --test for headless check, "
              "or Run-Script this file inside 3ds Max 2022.")
