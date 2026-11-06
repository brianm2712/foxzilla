"""The Tk window. Imported lazily, so the rest works with no display at all."""
from __future__ import annotations

import json
import os
import posixpath
import queue
import threading
import time

from . import config
from .backends import (Backend, Entry, TransferError, clean_typed_path,
                       make_backend, safe_name)
from .config import load_secrets, load_sites, needs_password, save_sites
from .queue import Batch, Job, Queue, marker_path, write_marker
from .scan import (CLEAR, CONFLICT, IDENTICAL, SAME_SIZE, UNSAFE,
                   plan_transfer, summarise)
from .scan import WANTED as _WANTED
from .util import APP, VERSION, bar, human, when

def _headless_tk():
    """
    Stand-ins for Tk, for machines with no display libraries.

    The backends, the queue and the scan live in their own modules and never
    import this one, so the hosted version needs none of this. It remains so
    the classes below can still be defined on a machine with no Tk. They
    are only ever used as base classes; every real Tk call happens inside a
    method, which never runs without a window. Asking for the window without
    Tk still fails loudly, in main().
    """
    import types
    tk_stub = types.ModuleType("tkinter")
    tk_stub.Toplevel = tk_stub.Frame = tk_stub.Text = tk_stub.Menu = object
    tk_stub.TclError = Exception
    ttk_stub = types.ModuleType("tkinter.ttk")
    ttk_stub.Frame = object
    return tk_stub, ttk_stub, None, None


try:
    import tkinter as tk
    from tkinter import messagebox, simpledialog, ttk
    HAVE_TK = True
except ImportError:                              # headless: backends only
    tk, ttk, messagebox, simpledialog = _headless_tk()
    HAVE_TK = False


# Foxers palette - charcoal and concrete with an amber accent, taken from
# foxers-demo.html so the tool matches the rest of the kit.
THEME = {
    "bg":       "#1C1E21",   # charcoal, the window ground
    "panel":    "#2E3238",   # graphite, raised surfaces
    "field":    "#24272B",   # input and list backgrounds
    "text":     "#F4F4F2",   # off-white
    "muted":    "#8A9099",
    "line":     "#3A3F45",
    "accent":   "#FFB020",   # amber
    "accent_d": "#9C6500",   # pressed / borders
    "ok":       "#2E7D4F",
    "err":      "#B3261E",
    "sel":      "#3A3226",   # amber-tinted selection
}


def apply_theme(root):
    """
    Paint the whole window in the Foxers palette.

    Tk's default widgets ignore most styling, so this leans on ttk's 'clam'
    theme (the one theme that honours every colour we set) and configures the
    classic tk widgets - menus, the log Text - by hand.
    """
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    t = THEME
    root.configure(bg=t["bg"])
    style.configure(".", background=t["bg"], foreground=t["text"],
                    fieldbackground=t["field"], bordercolor=t["line"],
                    lightcolor=t["panel"], darkcolor=t["bg"],
                    focuscolor=t["accent"], insertcolor=t["text"])
    style.configure("TFrame", background=t["bg"])
    style.configure("TLabel", background=t["bg"], foreground=t["text"])
    style.configure("Hint.TLabel", background=t["bg"], foreground=t["muted"])
    style.configure("TCheckbutton", background=t["bg"], foreground=t["text"])
    style.map("TCheckbutton",
              background=[("active", t["bg"])],
              indicatorcolor=[("selected", t["accent"])])
    style.configure("TButton", background=t["panel"], foreground=t["text"],
                    bordercolor=t["line"], focusthickness=1, padding=(8, 3))
    style.map("TButton",
              background=[("active", t["line"]), ("pressed", t["accent_d"])],
              foreground=[("disabled", t["muted"])])
    style.configure("Accent.TButton", background=t["accent"],
                    foreground=t["bg"], bordercolor=t["accent_d"])
    style.map("Accent.TButton",
              background=[("active", "#FFC14D"), ("pressed", t["accent_d"])],
              foreground=[("active", t["bg"])])
    style.configure("TEntry", fieldbackground=t["field"], foreground=t["text"],
                    bordercolor=t["line"], insertcolor=t["text"])
    style.configure("TCombobox", fieldbackground=t["field"], background=t["panel"],
                    foreground=t["text"], arrowcolor=t["accent"])
    style.map("TCombobox", fieldbackground=[("readonly", t["field"])],
              foreground=[("readonly", t["text"])])
    root.option_add("*TCombobox*Listbox.background", t["field"])
    root.option_add("*TCombobox*Listbox.foreground", t["text"])
    root.option_add("*TCombobox*Listbox.selectBackground", t["accent"])
    root.option_add("*TCombobox*Listbox.selectForeground", t["bg"])
    style.configure("Treeview", background=t["field"], fieldbackground=t["field"],
                    foreground=t["text"], bordercolor=t["line"], rowheight=22)
    style.configure("Treeview.Heading", background=t["panel"],
                    foreground=t["muted"], relief="flat", padding=(6, 4))
    style.map("Treeview.Heading", background=[("active", t["line"])],
              foreground=[("active", t["accent"])])
    style.map("Treeview", background=[("selected", t["sel"])],
              foreground=[("selected", t["accent"])])
    style.configure("TNotebook", background=t["bg"], bordercolor=t["line"])
    style.configure("TNotebook.Tab", background=t["panel"], foreground=t["muted"],
                    padding=(14, 5), bordercolor=t["line"])
    style.map("TNotebook.Tab",
              background=[("selected", t["bg"])],
              foreground=[("selected", t["accent"])])
    style.configure("TPanedwindow", background=t["bg"])
    style.configure("Sash", background=t["line"], sashthickness=5)
    style.configure("Vertical.TScrollbar", background=t["panel"],
                    troughcolor=t["bg"], bordercolor=t["bg"],
                    arrowcolor=t["muted"])
    style.configure("TSeparator", background=t["line"])
    style.configure("Status.TLabel", background=t["panel"],
                    foreground=t["muted"], padding=(6, 3))
    return style


class Pane(ttk.Frame):
    """One side of the window: a site picker, a path bar and a file list."""

    def __init__(self, master, app, title, initial=None):
        super().__init__(master, padding=(4, 4))
        self.app = app
        self.backend: Backend | None = None
        self.site: dict = {}
        self.password = ""
        self.path = "/"
        self.entries: list[Entry] = []

        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Label(bar, text=title, width=7).pack(side="left")
        self.site_var = tk.StringVar()
        self.site_box = ttk.Combobox(bar, textvariable=self.site_var,
                                     state="readonly", width=18)
        self.site_box.pack(side="left", padx=(0, 4))
        self.site_box.bind("<<ComboboxSelected>>", lambda e: self.connect())
        ttk.Button(bar, text="Connect", width=8,
                   command=self.connect).pack(side="left")

        nav = ttk.Frame(self)
        nav.pack(fill="x", pady=(4, 4))
        ttk.Button(nav, text="↑", width=3, command=self.go_up).pack(side="left")
        ttk.Button(nav, text="⌂", width=3, command=self.go_home).pack(side="left")
        ttk.Button(nav, text="↻", width=3, command=self.refresh).pack(side="left")
        self.path_var = tk.StringVar()
        pe = ttk.Entry(nav, textvariable=self.path_var)
        pe.pack(side="left", fill="x", expand=True, padx=4)
        pe.bind("<Return>", lambda e: self.go_typed())

        cols = ("size", "modified")
        self.tree = ttk.Treeview(self, columns=cols, selectmode="extended")
        self.tree.heading("#0", text="Name",
                          command=lambda: self.sort_by("name"))
        self.tree.heading("size", text="Size",
                          command=lambda: self.sort_by("size"))
        self.tree.heading("modified", text="Modified",
                          command=lambda: self.sort_by("mtime"))
        self.tree.column("#0", width=240, stretch=True)
        self.tree.column("size", width=70, anchor="e", stretch=False)
        self.tree.column("modified", width=125, anchor="w", stretch=False)
        vs = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")

        self.tree.bind("<Double-1>", self.on_open)
        self.tree.bind("<Return>", self.on_open)
        self.tree.bind("<Delete>", lambda e: self.delete_selected())
        self.tree.bind("<F2>", lambda e: self.rename_selected())
        self.tree.bind("<Button-3>", self.on_menu)

        self.menu = tk.Menu(self, tearoff=0, bg=THEME["panel"],
                            fg=THEME["text"], activebackground=THEME["accent"],
                            activeforeground=THEME["bg"], borderwidth=0)
        self.menu.add_command(label="Transfer →", command=lambda: app.transfer(self))
        self.menu.add_separator()
        self.menu.add_command(label="New folder…", command=self.new_folder)
        self.menu.add_command(label="Rename…  (F2)", command=self.rename_selected)
        self.menu.add_command(label="Delete   (Del)", command=self.delete_selected)
        self.menu.add_separator()
        self.menu.add_command(label="Refresh", command=self.refresh)

        self.sort_field = "name"
        self.status = ttk.Label(self, text="not connected", anchor="w")
        self.status.pack(side="bottom", fill="x", before=self.tree)

        self.reload_sites(initial)

    # -- sites ------------------------------------------------------------

    def reload_sites(self, select=None):
        names = [s["name"] for s in self.app.sites]
        self.site_box["values"] = names
        if select and select in names:
            self.site_var.set(select)
        elif names and not self.site_var.get():
            self.site_var.set(names[0])

    def connect(self):
        name = self.site_var.get()
        site = next((s for s in self.app.sites if s["name"] == name), None)
        if not site:
            return
        password = ""
        if needs_password(site):
            password = load_secrets().get(name, "")
            if not password:
                password = simpledialog.askstring(
                    "Password", f"Password for {name}:", show="•",
                    parent=self.app.root) or ""
        self.set_status(f"connecting to {name}…")
        old = self.backend

        def work():
            backend = make_backend(site, password)
            backend.connect()
            return backend

        def ok(backend):
            if old:
                threading.Thread(target=old.close, daemon=True).start()
            self.backend = backend
            # Kept so the queue can open its own connection to the same site.
            self.site, self.password = dict(site), password
            self.app.log(f"connected: {backend.label}")
            self.chdir(backend.home())
            missing = backend.is_local and backend.start_missing()
            if missing:
                self.app.log(f"[{name}] start folder not found: {missing}"
                             " - opened home instead")
                messagebox.showwarning(
                    "Folder not found",
                    f"{name}'s folder isn't there:\n\n{missing}\n\n"
                    "If it's on an external drive, mount the drive and "
                    "Connect again. Showing your home folder for now.",
                    parent=self.app.root)

        self.app.run_async(work, ok, self.fail)

    # -- navigation -------------------------------------------------------

    def chdir(self, path):
        if not self.backend:
            self.set_status("pick a site and hit Connect")
            return
        path = path or "/"
        self.set_status(f"reading {path}…")

        def work():
            return self.backend.listdir(path)

        def ok(entries):
            self.path = path
            self.path_var.set(path)
            self.entries = entries
            self.render()

        self.app.run_async(work, ok, self.fail)

    def go_typed(self):
        path = clean_typed_path(self.path_var.get())
        if self.backend and self.backend.is_local:
            path = os.path.expanduser(path)
        self.chdir(path)

    def go_up(self):
        if self.backend:
            self.chdir(self.backend.parent(self.path))

    def go_home(self):
        if self.backend:
            self.chdir(self.backend.home())

    def refresh(self):
        self.chdir(self.path)

    def on_open(self, _event=None):
        for e in self.selection():
            if e.is_dir or e.is_link:
                self.chdir(self.backend.join(self.path, e.name))
            return

    def on_menu(self, event):
        row = self.tree.identify_row(event.y)
        if row and row not in self.tree.selection():
            self.tree.selection_set(row)
        self.app.focus_pane = self
        self.menu.tk_popup(event.x_root, event.y_root)

    # -- rendering --------------------------------------------------------

    def sort_by(self, field):
        self.sort_field = field
        self.render()

    def render(self):
        self.tree.delete(*self.tree.get_children())
        if self.sort_field == "name":
            rows = sorted(self.entries, key=lambda e: e.sort_key)
        else:
            rows = sorted(self.entries,
                          key=lambda e: (not e.is_dir,
                                         -getattr(e, self.sort_field, 0)))
        for e in rows:
            icon = "\U0001f4c1 " if e.is_dir else ("\U0001f517 " if e.is_link else "\U0001f4c4 ")
            self.tree.insert("", "end", iid=e.name, text=icon + e.name,
                             values=(human(e.size), when(e.mtime)))
        ndirs = sum(1 for e in self.entries if e.is_dir)
        total = sum(e.size for e in self.entries if not e.is_dir)
        self.set_status(
            f"{len(self.entries) - ndirs} files, {ndirs} folders"
            + (f" — {human(total)}" if total else "")
        )

    def selection(self) -> list[Entry]:
        by_name = {e.name: e for e in self.entries}
        return [by_name[n] for n in self.tree.selection() if n in by_name]

    def set_status(self, text):
        self.status.configure(text=text)

    def fail(self, exc):
        self.set_status(f"error: {exc}")
        self.app.log(f"[{self.site_var.get()}] {exc}")

    # -- file operations --------------------------------------------------

    def new_folder(self):
        if not self.backend:
            return
        name = simpledialog.askstring("New folder", "Name:", parent=self.app.root)
        if not name:
            return
        target = self.backend.join(self.path, name)
        self.app.run_async(lambda: self.backend.mkdir(target),
                           lambda _: self.refresh(), self.fail)

    def rename_selected(self):
        sel = self.selection()
        if not self.backend or not sel:
            return
        old = sel[0].name
        new = simpledialog.askstring("Rename", "New name:", initialvalue=old,
                                     parent=self.app.root)
        if not new or new == old:
            return
        src = self.backend.join(self.path, old)
        dst = self.backend.join(self.path, new)
        self.app.run_async(lambda: self.backend.rename(src, dst),
                           lambda _: self.refresh(), self.fail)

    def delete_selected(self):
        sel = self.selection()
        if not self.backend or not sel:
            return
        what = sel[0].name if len(sel) == 1 else f"{len(sel)} items"
        if not messagebox.askyesno("Delete", f"Delete {what} from "
                                             f"{self.backend.label}?\n"
                                             "Folders are removed with their contents.",
                                   parent=self.app.root):
            return

        def work():
            for e in sel:
                target = self.backend.join(self.path, e.name)
                self.backend.rmdir(target) if e.is_dir else self.backend.remove(target)

        self.app.run_async(work, lambda _: self.refresh(), self.fail)


class ScanDialog(tk.Toplevel):
    """
    What the destination already has, and what to do about it.

    Only shown when the scan finds something - a clean upload should not need
    a click. The choice defaults to skipping what is already there, because
    that is almost always what was meant and it is the only option that
    cannot lose anything.
    """

    def __init__(self, parent, plan, dst_label):
        super().__init__(parent)
        self.title("Already at the destination")
        self.transient(parent)
        self.configure(bg=THEME["bg"])
        self.choice = None
        counts = summarise(plan)

        head = ttk.Frame(self, padding=(14, 12, 14, 6))
        head.pack(fill="x")
        existing = counts[IDENTICAL] + counts[SAME_SIZE] + counts[CONFLICT]
        ttk.Label(head, font=("", 11, "bold"),
                  text=f"{existing} of {counts['files']} files are already on "
                       f"{dst_label}").pack(anchor="w")

        bits = []
        if counts[IDENTICAL]:
            bits.append(f"{counts[IDENTICAL]} identical (verified by hash)")
        if counts[SAME_SIZE]:
            bits.append(f"{counts[SAME_SIZE]} the same size "
                        "(no remote hash available, so not certain)")
        if counts[CONFLICT]:
            bits.append(f"{counts[CONFLICT]} same name but different content")
        if counts[CLEAR]:
            bits.append(f"{counts[CLEAR]} new — {human(counts['new_bytes'])}")
        if counts[UNSAFE]:
            bits.append(f"{counts[UNSAFE]} refused — the server listed a name that "
                        "would land outside the destination folder")
        ttk.Label(head, style="Hint.TLabel", justify="left",
                  text="\n".join(bits)).pack(anchor="w", pady=(4, 0))

        body = ttk.Frame(self, padding=(14, 0))
        body.pack(fill="both", expand=True)
        cols = ("file", "size", "status")
        tree = ttk.Treeview(body, columns=cols, show="headings", height=10)
        for c, w in zip(cols, (330, 80, 210)):
            tree.heading(c, text=c.capitalize())
            tree.column(c, width=w, anchor="e" if c == "size" else "w")
        for tag, colour in ((CLEAR, THEME["ok"]), (IDENTICAL, THEME["muted"]),
                            (SAME_SIZE, THEME["accent"]), (CONFLICT, THEME["err"]),
                            (UNSAFE, THEME["err"])):
            tree.tag_configure(tag, foreground=colour)
        label = {CLEAR: "new", IDENTICAL: "identical — skip",
                 SAME_SIZE: "same size, unverified", CONFLICT: "differs — would overwrite",
                 UNSAFE: "unsafe name — refused"}
        # Existing files first: they are the reason this dialog is open.
        for item in sorted((p for p in plan if not p.is_dir),
                           key=lambda p: (p.verdict == CLEAR, p.name.lower())):
            tree.insert("", "end", values=(item.name, human(item.size),
                                           label[item.verdict]),
                        tags=(item.verdict,))
        sb = ttk.Scrollbar(body, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        opts = ttk.Frame(self, padding=(14, 10))
        opts.pack(fill="x")
        self.mode = tk.StringVar(value="skip")
        for value, text in (
            ("skip", "Upload only what is new — skip everything already there"),
            ("replace", "Upload new files, and overwrite the ones that differ"),
            ("all", "Upload everything, overwriting whatever is there"),
        ):
            ttk.Radiobutton(opts, text=text, value=value,
                            variable=self.mode).pack(anchor="w", pady=1)

        btns = ttk.Frame(self, padding=(14, 0, 14, 12))
        btns.pack(fill="x")
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="Transfer", style="Accent.TButton",
                   command=self._go).pack(side="right", padx=6)

        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()
        self.wait_window(self)

    def _go(self):
        self.choice = self.mode.get()
        self.destroy()


class SiteDialog(tk.Toplevel):
    """Add or edit a saved site. Fields shown depend on the protocol."""

    FIELDS = {
        "local": [("path", "Start folder (blank = home)")],
        "sftp": [("host", "Host or ~/.ssh/config alias"), ("user", "User"),
                 ("port", "Port (22)"), ("identity", "Key file (optional)"),
                 ("auth", "Auth: key (default) or password"),
                 ("path", "Start folder (optional)")],
        "ftp": [("host", "Host"), ("user", "User"), ("port", "Port (21)")],
        "ftps": [("host", "Host"), ("user", "User"), ("port", "Port (21)"),
                 ("ca_file", "CA file for a self-signed server (optional)"),
                 ("insecure_tls", "Skip certificate check: yes/no (default no)")],
        "s3": [("bucket", "Bucket"), ("region", "Region"),
               ("endpoint", "Endpoint (blank = AWS)"),
               ("access_key", "Access key"), ("secret_key", "Secret key")],
        "dav": [("url", "Base URL"), ("user", "User")],
    }

    def __init__(self, parent, site=None):
        super().__init__(parent)
        self.title("Site")
        self.transient(parent)
        self.result = None
        self.site = dict(site or {"type": "sftp"})
        self.vars = {}

        top = ttk.Frame(self, padding=10)
        top.pack(fill="both", expand=True)
        ttk.Label(top, text="Name").grid(row=0, column=0, sticky="w", pady=2)
        self.name_var = tk.StringVar(value=self.site.get("name", ""))
        ttk.Entry(top, textvariable=self.name_var, width=32).grid(
            row=0, column=1, sticky="ew", pady=2)

        ttk.Label(top, text="Protocol").grid(row=1, column=0, sticky="w", pady=2)
        self.type_var = tk.StringVar(value=self.site.get("type", "sftp"))
        box = ttk.Combobox(top, textvariable=self.type_var, state="readonly",
                           values=list(self.FIELDS), width=30)
        box.grid(row=1, column=1, sticky="ew", pady=2)
        box.bind("<<ComboboxSelected>>", lambda e: self.build_fields())

        self.body = ttk.Frame(top)
        self.body.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        top.columnconfigure(1, weight=1)

        btns = ttk.Frame(top)
        btns.grid(row=3, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="Save", command=self.save).pack(side="right", padx=4)

        self.build_fields()
        self.grab_set()
        self.wait_window(self)

    def build_fields(self):
        for w in self.body.winfo_children():
            w.destroy()
        self.vars.clear()
        for i, (key, caption) in enumerate(self.FIELDS[self.type_var.get()]):
            ttk.Label(self.body, text=caption).grid(row=i, column=0,
                                                    sticky="w", pady=2)
            var = tk.StringVar(value=str(self.site.get(key, "")))
            show = "•" if key == "secret_key" else ""
            ttk.Entry(self.body, textvariable=var, width=32, show=show).grid(
                row=i, column=1, sticky="ew", pady=2, padx=(8, 0))
            self.vars[key] = var
        self.body.columnconfigure(1, weight=1)

    def save(self):
        name = self.name_var.get().strip()
        if not name:
            messagebox.showerror("Site", "Give the site a name.", parent=self)
            return
        out = {"name": name, "type": self.type_var.get()}
        for key, var in self.vars.items():
            val = var.get().strip()
            if val:
                out[key] = int(val) if key == "port" and val.isdigit() else val
        self.result = out
        self.destroy()


class App:
    def __init__(self, root):
        self.root = root
        # Applied here rather than in main() so anything embedding App - the
        # tests included - gets the same window instead of default grey.
        self.style = apply_theme(root)
        root.title(f"Foxzilla {VERSION}")
        self._set_window_icon(root)
        root.geometry("1100x680")
        self.sites = load_sites()
        self.focus_pane = None
        self._ui_queue: queue.Queue = queue.Queue()
        self._pending_refresh = None
        self._rows: dict[str, Job] = {}
        # The queue's own connection per site, separate from the panes', so
        # browsing never shares a connection with a running transfer.
        self._queue_backends: dict[str, Backend] = {}
        self._queue_backends_lock = threading.Lock()
        self.queue = Queue(self.on_job_change, workers=8)

        self._build_menu()

        outer = ttk.Frame(root)
        outer.pack(fill="both", expand=True)

        split = ttk.PanedWindow(outer, orient="horizontal")
        split.pack(fill="both", expand=True, padx=4, pady=4)
        self.left = Pane(split, self, "Local", initial="Local")
        self.right = Pane(split, self, "Remote",
                          initial=next((s["name"] for s in self.sites
                                        if s.get("type") != "local"), None))
        split.add(self.left, weight=1)
        split.add(self.right, weight=1)

        for pane in (self.left, self.right):
            pane.tree.bind("<FocusIn>",
                           lambda e, p=pane: setattr(self, "focus_pane", p))

        mid = ttk.Frame(outer)
        mid.pack(fill="x", padx=8, pady=(2, 0))
        ttk.Button(mid, text="→  Send", width=10, style="Accent.TButton",
                   command=lambda: self.transfer(self.left)).pack(side="left")
        ttk.Button(mid, text="←  Fetch", width=10, style="Accent.TButton",
                   command=lambda: self.transfer(self.right)).pack(side="left", padx=4)

        self.scan_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(mid, text="Scan destination first",
                        variable=self.scan_var).pack(side="left", padx=(16, 0))

        ttk.Label(mid, text="parallel").pack(side="left", padx=(16, 4))
        self.workers_var = tk.StringVar(value="3")
        wbox = ttk.Combobox(mid, textvariable=self.workers_var, state="readonly",
                            width=3, values=("1", "2", "3", "4", "6", "8"))
        wbox.pack(side="left")
        wbox.bind("<<ComboboxSelected>>",
                  lambda e: self.queue.set_limit(self.workers_var.get()))

        ttk.Button(mid, text="Clear finished",
                   command=self.clear_finished).pack(side="right")
        ttk.Button(mid, text="Cancel all",
                   command=self.queue.cancel_all).pack(side="right", padx=4)
        self.pause_btn = ttk.Button(mid, text="Pause", width=8,
                                    command=self.toggle_pause)
        self.pause_btn.pack(side="right", padx=4)

        tabs = ttk.Notebook(outer, height=200)
        tabs.pack(fill="both", expand=False, padx=4, pady=4)

        qf = ttk.Frame(tabs)
        qbar = ttk.Frame(qf)
        qbar.pack(side="top", fill="x", pady=(0, 2))
        ttk.Label(qbar, text="priority").pack(side="left", padx=(2, 6))
        for text, fn in (("⤒ Top", lambda: self.reorder("top")),
                         ("↑", lambda: self.reorder(-1)),
                         ("↓", lambda: self.reorder(1)),
                         ("⤓ Bottom", lambda: self.reorder("bottom"))):
            ttk.Button(qbar, text=text, width=8 if len(text) > 2 else 3,
                       command=fn).pack(side="left", padx=1)
        ttk.Button(qbar, text="Cancel", width=8,
                   command=self.cancel_selected).pack(side="left", padx=(10, 0))
        ttk.Label(qbar, text="  (or Alt+↑ / Alt+↓ on a row)",
                  style="Hint.TLabel").pack(side="left")

        cols = ("name", "from", "to", "size", "progress", "speed", "state")
        self.qtree = ttk.Treeview(qf, columns=cols, show="headings")
        widths = (170, 190, 190, 70, 130, 80, 110)
        for c, w in zip(cols, widths):
            self.qtree.heading(c, text=c.capitalize())
            self.qtree.column(c, width=w, anchor="e" if c in ("size", "speed") else "w")
        qs = ttk.Scrollbar(qf, orient="vertical", command=self.qtree.yview)
        self.qtree.configure(yscrollcommand=qs.set)
        self.qtree.pack(side="left", fill="both", expand=True)
        qs.pack(side="right", fill="y")
        self.qtree.bind("<Alt-Up>", lambda e: self.reorder(-1))
        self.qtree.bind("<Alt-Down>", lambda e: self.reorder(1))
        self.qtree.bind("<Delete>", lambda e: self.cancel_selected())
        for tag, colour in (("running", THEME["accent"]), ("done", THEME["ok"]),
                            ("failed", THEME["err"]), ("skipped", THEME["muted"]),
                            ("cancelled", THEME["muted"])):
            self.qtree.tag_configure(tag, foreground=colour)
        tabs.add(qf, text="Queue")

        lf = ttk.Frame(tabs)
        self.logbox = tk.Text(lf, height=8, wrap="none", state="disabled",
                              font=("monospace", 9), relief="flat",
                              bg=THEME["field"], fg=THEME["text"],
                              insertbackground=THEME["text"],
                              selectbackground=THEME["sel"])
        ls = ttk.Scrollbar(lf, orient="vertical", command=self.logbox.yview)
        self.logbox.configure(yscrollcommand=ls.set)
        self.logbox.pack(side="left", fill="both", expand=True)
        ls.pack(side="right", fill="y")
        tabs.add(lf, text="Log")

        self.status = ttk.Label(root, anchor="w", style="Status.TLabel",
                                text=f"{APP} {VERSION} — SFTP · FTP/FTPS · S3 · WebDAV")
        self.status.pack(fill="x", side="bottom")

        root.protocol("WM_DELETE_WINDOW", self.quit)
        self._pump()
        self.left.connect()

    @staticmethod
    def _set_window_icon(root):
        """
        Use the installed icon for the window and taskbar if it is there.

        Purely cosmetic, and entirely optional - running the script straight
        out of a checkout should not depend on having installed anything.
        """
        for path in (
            # beside the launcher script (client/), as before the package split
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "foxzilla.png"),
            os.path.expanduser("~/.local/share/foxzilla/foxzilla.png"),
            os.path.expanduser("~/.local/share/icons/hicolor/256x256/apps/foxzilla.png"),
        ):
            try:
                if os.path.exists(path):
                    root.iconphoto(True, tk.PhotoImage(file=path))
                    return
            except tk.TclError:
                continue

    # -- menu -------------------------------------------------------------

    def _build_menu(self):
        menubar = tk.Menu(self.root, bg=THEME["panel"], fg=THEME["text"],
                      activebackground=THEME["accent"],
                      activeforeground=THEME["bg"], borderwidth=0)
        sites = tk.Menu(menubar, tearoff=0, bg=THEME["panel"], fg=THEME["text"],
                        activebackground=THEME["accent"],
                        activeforeground=THEME["bg"], borderwidth=0)
        sites.add_command(label="Add site…", command=self.add_site)
        sites.add_command(label="Edit site…", command=self.edit_site)
        sites.add_command(label="Remove site", command=self.remove_site)
        sites.add_separator()
        sites.add_command(label=f"Open {config.SITES_FILE}", command=self.reveal_config)
        sites.add_separator()
        sites.add_command(label="Quit", command=self.quit)
        menubar.add_cascade(label="Sites", menu=sites)
        self.root.config(menu=menubar)

    def reveal_config(self):
        save_sites(self.sites)
        self.log(f"sites file: {config.SITES_FILE}")

    def add_site(self):
        dlg = SiteDialog(self.root)
        if dlg.result:
            self.sites.append(dlg.result)
            self._sites_changed(dlg.result["name"])

    def edit_site(self):
        pane = self.focus_pane or self.right
        name = pane.site_var.get()
        idx = next((i for i, s in enumerate(self.sites) if s["name"] == name), None)
        if idx is None:
            return
        dlg = SiteDialog(self.root, self.sites[idx])
        if dlg.result:
            self.sites[idx] = dlg.result
            self._sites_changed(dlg.result["name"])

    def remove_site(self):
        pane = self.focus_pane or self.right
        name = pane.site_var.get()
        if name and messagebox.askyesno("Remove site", f"Remove {name}?"):
            self.sites = [s for s in self.sites if s["name"] != name]
            self._sites_changed()

    def _sites_changed(self, select=None):
        save_sites(self.sites)
        self.left.reload_sites()
        self.right.reload_sites(select)
        self.log("sites saved")

    # -- async plumbing ---------------------------------------------------

    def _pump(self):
        """
        Drain callbacks posted by worker threads.

        Tk objects may only be touched from the thread running the main loop,
        and that includes `after` itself, so background work hands results
        back through a plain queue that this main-thread tick drains.
        """
        for _ in range(64):             # bounded, so the UI stays responsive
            try:
                fn, arg = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn(arg)
            except Exception as e:
                self.log(f"ui error: {e}")
        self.root.after(40, self._pump)

    def post(self, fn, arg=None):
        """Ask the UI thread to run fn(arg). Safe from any thread."""
        self._ui_queue.put((fn, arg))

    def run_async(self, work, on_ok, on_err):
        """Run `work` off the UI thread, then hand the result back on it."""
        def runner():
            try:
                result = work()
            except Exception as e:
                self.post(on_err, e)
            else:
                self.post(on_ok, result)
        threading.Thread(target=runner, daemon=True).start()

    def log(self, text):
        stamp = time.strftime("%H:%M:%S")
        self.logbox.configure(state="normal")
        self.logbox.insert("end", f"{stamp}  {text}\n")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")
        self.status.configure(text=text)

    # -- transfers --------------------------------------------------------

    def _queue_backend(self, pane: Pane) -> Backend:
        """
        The queue's connection to `pane`'s site, opened on first use.

        Call off the UI thread: it may connect. Local backends hold no
        connection, so the pane's own is shared.
        """
        backend = pane.backend
        if backend.is_local:
            return backend
        key = json.dumps([pane.site, pane.password], sort_keys=True, default=str)
        with self._queue_backends_lock:
            mine = self._queue_backends.get(key)
            if mine is None:
                mine = make_backend(pane.site, pane.password)
                mine.connect()
                self._queue_backends[key] = mine
        return mine

    def transfer(self, src_pane: Pane):
        dst_pane = self.right if src_pane is self.left else self.left
        if not src_pane.backend or not dst_pane.backend:
            messagebox.showinfo(APP, "Connect both panes first.")
            return
        sel = src_pane.selection()
        if not sel:
            messagebox.showinfo(APP, "Nothing selected.")
            return
        try:
            for e in sel:
                safe_name(e.name)
        except TransferError as e:
            messagebox.showerror(APP, f"Not transferring: {e}")
            return

        src_browse, dst_browse = src_pane.backend, dst_pane.backend
        dst_root = dst_pane.path
        signal = bool(getattr(dst_browse, "signal_complete", False))
        items = [(e.name, src_browse.join(src_pane.path, e.name),
                  posixpath.join(dst_root, e.name), e.is_dir, e.size) for e in sel]
        scan = self.scan_var.get()
        self.log(f"scanning {dst_browse.label} for what is already there…" if scan
                 else f"queueing {len(sel)} item(s) → {dst_browse.label}")

        def work():
            src = self._queue_backend(src_pane)
            dst = self._queue_backend(dst_pane)
            if not scan:
                return src, dst, None
            plans = []
            for name, sp, dp, isdir, _size in items:
                plans.append((name, plan_transfer(
                    src, sp, dst, dp, isdir,
                    note=lambda n, path: self.post(
                        lambda p=path, n=n: self.log(f"scanned {n} — {posixpath.basename(p)}")
                        if n and n % 25 == 0 else None))))
            return src, dst, plans

        def ready(result):
            src, dst, plans = result
            if plans is None:
                for name, sp, dp, isdir, size in items:
                    batch = self._new_batch(name, dst, dst_root) if signal else None
                    self.queue.add_batch(batch, [Job(src=src, src_path=sp, dst=dst,
                                                     dst_path=dp, is_dir=isdir, size=size,
                                                     dst_root=dst_root)])
                self.log(f"queued {len(items)} item(s) → {dst.label}")
                self._pending_refresh = dst_pane
                return
            plan = [p for _name, item_plan in plans for p in item_plan]
            counts = summarise(plan)
            if counts[UNSAFE]:
                self.log(f"refusing {counts[UNSAFE]} item(s) with unsafe names "
                         "from the server")
            if not counts["files"]:
                self.log("nothing to transfer")
                return
            already = counts[IDENTICAL] + counts[SAME_SIZE] + counts[CONFLICT]
            if not already and not counts[UNSAFE]:
                self.log(f"scan: all {counts['files']} file(s) are new "
                         f"({human(counts['bytes'])}) — transferring")
                self._queue_plan(plans, "all", src, dst, dst_pane, dst_root, signal)
                return
            dialog = ScanDialog(self.root, plan, dst.label)
            if dialog.choice:
                self._queue_plan(plans, dialog.choice, src, dst, dst_pane, dst_root, signal)
            else:
                self.log("transfer cancelled")

        self.run_async(work, ready,
                       lambda e: self.log(f"could not start the transfer: {e}"))

    WANTED = _WANTED          # defined in scan.py; kept here for callers

    def _queue_plan(self, plans, mode, src, dst, dst_pane, dst_root, signal):
        """
        Create the folders the chosen files need, then queue the files.

        `plans` is one (item name, plan) pair per selected item, so each item
        gets its own batch - and its own marker, written only when that item
        is complete.
        """
        wanted = self.WANTED[mode]
        plan = [p for _name, item_plan in plans for p in item_plan]
        files = [p for p in plan if not p.is_dir and p.verdict in wanted]
        dirs = [p for p in plan if p.is_dir]
        skipped = sum(1 for p in plan if not p.is_dir and p.verdict not in wanted)

        if not files:
            self.log(f"nothing to do — all {skipped} file(s) already there")
            return

        def make_dirs():
            for d in dirs:
                try:
                    d.dst_path and dst.mkdir(d.dst_path)
                except Exception:                    # noqa: BLE001
                    pass                             # already exists
            return None

        def queue_them(_):
            for name, item_plan in plans:
                batch = self._new_batch(name, dst, dst_root) if signal else None
                if batch is not None:
                    left_out = [p for p in item_plan if not p.is_dir
                                and p.verdict in (CONFLICT, UNSAFE) and p.verdict not in wanted]
                    if left_out:
                        batch.taint(f"{len(left_out)} file(s) differ at the destination "
                                    "or were refused, and were not sent")
                self.queue.add_batch(batch, [
                    Job(src=src, src_path=p.src_path, dst=dst, dst_path=p.dst_path,
                        size=p.size, dst_root=dst_root)
                    for p in item_plan if not p.is_dir and p.verdict in wanted])
            self.log(f"queued {len(files)} file(s) → {dst.label}"
                     + (f", skipped {skipped} already there" if skipped else ""))
            self._pending_refresh = dst_pane

        self.run_async(make_dirs, queue_them,
                       lambda e: self.log(f"could not prepare folders: {e}"))

    def on_job_change(self, job: Job):
        # Called from the transfer worker; bounce onto the UI thread.
        self.post(self._render_job, job)

    def _render_job(self, job: Job):
        state, tag = job.state, job.state
        if job.state == "running":
            state = "transferring"
        elif job.state == "failed":
            state = f"failed: {job.error[:40]}"
        elif job.state == "skipped":
            state = "skipped (already there)"
        elif job.state == "done" and job.is_dir:
            state = "expanded"

        values = (
            job.name,
            f"{job.src.label}:{posixpath.dirname(job.src_path) or '/'}",
            f"{job.dst.label}:{posixpath.dirname(job.dst_path) or '/'}",
            "" if job.is_dir else human(job.size),
            "" if job.is_dir else bar(job.done, job.size),
            f"{human(int(job.rate))}/s" if job.rate else "",
            state,
        )
        if job.row and self.qtree.exists(job.row):
            self.qtree.item(job.row, values=values, tags=(tag,))
        else:
            job.row = self.qtree.insert("", "end", values=values, tags=(tag,))
            self._rows[job.row] = job
            self.qtree.see(job.row)

        if job.state in ("done", "failed", "skipped", "cancelled"):
            if job.state == "failed":
                self.log(f"failed {job.name}: {job.error}")
            elif job.state == "skipped" and job.error:
                self.log(f"skipped {job.name} — {job.error}")
            if self.queue.idle():
                pane = self._pending_refresh
                if pane:
                    pane.refresh()
                    self._pending_refresh = None
                self.log("queue idle")

    # -- completion signal ------------------------------------------------

    def _new_batch(self, name, dst, directory):
        """A batch for one item sent to a drop folder; its marker is its reward."""
        return Batch(name, on_done=lambda b: self.post(self._batch_done, b),
                     dst=dst, marker=marker_path(directory, name))

    def _batch_done(self, batch):
        """
        Drop a marker beside a finished item, so the far side can act now.

        Only for an item whose every job finished and none failed or was
        cancelled - a marker means "this is complete and verified", and one
        written after a partial transfer would be worse than none at all.
        The watcher re-hashes everything regardless, so the marker saves time,
        it does not replace the check.
        """
        if not batch.ok:
            self.log(f"not marking {batch.name} complete — {batch.reason}")
            return
        self.run_async(
            lambda: write_marker(batch.dst, batch.marker),
            lambda _: self.log(f"signalled {batch.name} complete — "
                               "the watcher can pick it up immediately"),
            lambda e: self.log(f"could not signal completion of {batch.name}: {e}"))

    # -- priority ---------------------------------------------------------

    def _selected_jobs(self):
        return [self._rows[r] for r in self.qtree.selection() if r in self._rows]

    def reorder(self, where):
        """Shift the selected rows through the queue. Order is priority."""
        jobs = self._selected_jobs()
        if not jobs:
            return
        if where == "top":
            for j in reversed(jobs):
                self.queue.move_to(j, 0)
        elif where == "bottom":
            for j in jobs:
                self.queue.move_to(j, len(self.queue.jobs))
        else:
            # moving down: walk from the bottom so they cannot collide
            for j in (jobs if where < 0 else reversed(jobs)):
                self.queue.move(j, where)
        self._resync_queue_view()

    def _resync_queue_view(self):
        """Redraw queue rows in the queue's own order."""
        for pos, job in enumerate(self.queue.jobs):
            if job.row and self.qtree.exists(job.row):
                self.qtree.move(job.row, "", pos)

    def cancel_selected(self):
        for job in self._selected_jobs():
            self.queue.cancel(job)

    def toggle_pause(self):
        paused = not self.queue.paused
        self.queue.set_paused(paused)
        self.pause_btn.configure(text="Resume" if paused else "Pause")
        self.log("queue paused" if paused else "queue resumed")

    def clear_finished(self):
        for job in self.queue.remove_finished():
            if job.row and self.qtree.exists(job.row):
                self.qtree.delete(job.row)
            self._rows.pop(job.row, None)

    def quit(self):
        busy = len(self.queue.running) + len(self.queue.pending)
        if busy and not messagebox.askyesno(
                "Quit Foxzilla",
                f"{busy} transfer(s) are still running or queued.\n\n"
                "Cancel them and quit? Partly downloaded files are removed; "
                "partly uploaded ones stay on the server.",
                icon="warning", parent=self.root):
            return
        self.queue.shutdown(timeout=5)
        for backend in self._open_backends():
            try:
                backend.close()
            except Exception:
                pass
        self.root.destroy()

    def _open_backends(self):
        with self._queue_backends_lock:
            mine = list(self._queue_backends.values())
            self._queue_backends.clear()
        return [p.backend for p in (self.left, self.right) if p.backend] + mine
