"""CustomTkinter look shared by every window: colours, fonts, the restyled ttk track tables, helpers.

The app uses CustomTkinter widgets (https://github.com/TomSchimansky/CustomTkinter) with its built-in
"blue" theme, following the system's light/dark mode. CustomTkinter has no table widget, so the track
lists are ttk.Treeviews styled here to match, and restyled whenever light/dark mode changes.
"""
from __future__ import annotations

import re
import tkinter as tk
from tkinter import ttk

import customtkinter as ctk

ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")

# Semantic colours as (light mode, dark mode)
GOOD = ("#1e8449", "#4cd07d")
BAD = ("#c0392b", "#ff6b6b")
WARN = ("#b9770e", "#f0b429")
INFO = ("#2471a3", "#5dade2")
ORANGE = ("#d35400", "#ff9f43")
MUTED = ("gray40", "gray62")
STOP = ("#c0392b", "#a83226")
STOP_HOVER = ("#a93226", "#8e2a20")

_trees: list[ttk.Treeview] = []


def font(size: int = 13, bold: bool = False) -> ctk.CTkFont:
    return ctk.CTkFont(size=size, weight="bold" if bold else "normal")


def mode_color(pair) -> str:
    """The light or dark half of a colour pair, for widgets that only take one colour (ttk)."""
    if isinstance(pair, str):
        return pair
    return pair[1] if ctk.get_appearance_mode() == "Dark" else pair[0]


def _theme(widget: str, key: str) -> str:
    return mode_color(ctk.ThemeManager.theme[widget][key])


def style_tables(_mode=None):
    """(Re)style every ttk.Treeview to match the CustomTkinter colours of the current mode."""
    if _mode is not None:            # called by CustomTkinter when light/dark mode changes
        try:
            root = tk._default_root
            if root is not None:
                root.after(50, style_tables)   # let CustomTkinter finish switching first
        except Exception:
            pass
        return
    style = ttk.Style()
    if style.theme_use() != "clam":
        style.theme_use("clam")       # the macOS/Windows native themes ignore custom colours
    bg = _theme("CTkFrame", "fg_color")
    head = _theme("CTkFrame", "top_fg_color")
    fg = _theme("CTkLabel", "text_color")
    accent = _theme("CTkButton", "fg_color")
    border = _theme("CTkFrame", "border_color")
    style.configure("Treeview", background=bg, fieldbackground=bg, foreground=fg, bordercolor=border,
                    borderwidth=0, rowheight=28, font=("", 12))
    style.map("Treeview", background=[("selected", accent)], foreground=[("selected", "white")])
    style.configure("Treeview.Heading", background=head, foreground=fg, relief="flat", borderwidth=0,
                    font=("", 12, "bold"), padding=(6, 4))
    style.map("Treeview.Heading", background=[("active", border)])
    style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])   # no extra frame/border
    for tree in list(_trees):
        try:
            for tag, pair in getattr(tree, "_tag_pairs", {}).items():
                tree.tag_configure(tag, foreground=mode_color(pair))
        except tk.TclError:
            _trees.remove(tree)


def table(master, columns, headings: dict, widths: dict, stretch=(), selectmode="extended",
          tags: dict | None = None) -> tuple[ctk.CTkFrame, ttk.Treeview]:
    """A rounded CTk frame holding a styled ttk.Treeview and a CTk scrollbar."""
    frame = ctk.CTkFrame(master)
    tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode=selectmode)
    for c in columns:
        tree.heading(c, text=headings[c], anchor="w")
        tree.column(c, width=widths[c], anchor="w", stretch=c in stretch)
    sb = ctk.CTkScrollbar(frame, command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
    sb.pack(side="right", fill="y", padx=(2, 4), pady=8)
    tree._tag_pairs = dict(tags or {})
    for tag, pair in tree._tag_pairs.items():
        tree.tag_configure(tag, foreground=mode_color(pair))
    _trees.append(tree)
    return frame, tree


def set_enabled(widget, on: bool):
    widget.configure(state="normal" if on else "disabled")


def is_enabled(widget) -> bool:
    return str(widget.cget("state")) != "disabled"


def secondary(master, text, command, **kw) -> ctk.CTkButton:
    """Outlined button for less important actions (Cancel, Skip, Open folder…)."""
    return ctk.CTkButton(master, text=text, command=command, fg_color="transparent", border_width=1,
                         text_color=("gray10", "#DCE4EE"), **kw)


def window(master, title: str, icon: tk.PhotoImage | None = None) -> ctk.CTkToplevel:
    """A hidden CTkToplevel: add widgets, then call reveal(win, ...)."""
    win = ctk.CTkToplevel(master)
    begin(win)
    win.title(title)
    fix_icon(win, icon)
    return win


def fix_icon(win, icon: tk.PhotoImage | None):
    """Use Soulrip's icon on a CTkToplevel (CustomTkinter swaps in its own ~200 ms after opening on Windows)."""
    if icon is not None:
        win.after(250, lambda: _set_icon(win, icon))


def _set_icon(win, icon):
    try:
        win.iconphoto(False, icon)
    except tk.TclError:
        pass


def grab(win, tries: int = 10):
    """grab_set() fails while a new window isn't on screen yet (common with CTkToplevel on macOS)."""
    try:
        win.grab_set()
    except tk.TclError:
        if tries:
            win.after(60, lambda: grab(win, tries - 1))


def init(root):
    """Call once after creating the CTk root."""
    style_tables()
    try:
        ctk.AppearanceModeTracker.add(style_tables, root)
    except Exception:
        pass


# --------------------------------------------------------------------------- flash-free windows
# CustomTkinter windows go on screen the moment they're created, and its widgets force redraws while
# they're being built. So every window is hidden (and transparent) first, built unseen, positioned,
# and only then shown — otherwise you see it half-built at the default spot before it jumps into place.

def begin(win):
    """Call right after creating a CTk / CTkToplevel, before adding widgets."""
    try:
        win.attributes("-alpha", 0.0)
    except tk.TclError:
        pass
    win.withdraw()


def reveal(win, center_on=None):
    """Position the finished window (centred on `center_on`, or on the screen) and show it in one go."""
    win.update_idletasks()                     # window is still hidden: this only computes its size
    # the real (already DPI-scaled) size: an explicit geometry like "1180x540", else what the widgets need
    m = re.match(r"(\d+)x(\d+)", tk.Wm.wm_geometry(win))
    w, h = (int(m.group(1)), int(m.group(2))) if m and m.group(1) != "1" else (win.winfo_reqwidth(),
                                                                                win.winfo_reqheight())
    if center_on is not None and center_on.winfo_ismapped():
        x = center_on.winfo_rootx() + (center_on.winfo_width() - w) // 2
        y = center_on.winfo_rooty() + max(0, (center_on.winfo_height() - h) // 3)
    else:
        x = (win.winfo_screenwidth() - w) // 2
        y = (win.winfo_screenheight() - h) // 3
    # position only, via plain Tk: CustomTkinter's geometry() would scale the numbers a second time
    tk.Wm.wm_geometry(win, f"+{max(0, x)}+{max(0, y)}")
    win.deiconify()
    win.update_idletasks()                     # draw it fully before making it visible
    try:
        win.attributes("-alpha", 1.0)
    except tk.TclError:
        pass
    win.lift()
    win.focus_force()
