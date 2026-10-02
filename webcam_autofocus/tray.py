"""Tray-App (GTK 4 / libadwaita): Icon in der Panel-Leiste zum Ein-/Ausschalten, dazu ein Fenster
mit Regler, Schärfe-Verlauf und Log.

Das Icon meldet sich direkt per D-Bus als StatusNotifierItem an (kein GTK-3-AppIndicator nötig):
  Linksklick               Autofokus ein/aus
  Mittelklick              Fenster zeigen
  Rechtsklick              Menü (Ein/Aus · Fenster öffnen · Beenden), per dbusmenu
"""
import argparse
import dataclasses
import math
import os
import sys
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .cli import add_config_args  # noqa: E402
from .engine import AutoFocus, Config  # noqa: E402

APP_ID = "io.github.WebcamAutofocus"
ICON_ON, ICON_OFF, ICON_ERR = "on", "off", "error"       # Zustand des Panel-Icons (Linse grün / rot / orange)
LENS = {ICON_ON: (0.20, 0.78, 0.35), ICON_OFF: (0.90, 0.22, 0.22), ICON_ERR: (0.95, 0.60, 0.10)}
GRAPH_SECONDS = 30
GREEN, ORANGE, RED = (0.20, 0.70, 0.35), (0.95, 0.60, 0.10), (0.85, 0.25, 0.25)

SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="u" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="Activate"><arg type="i" name="x" direction="in"/><arg type="i" name="y" direction="in"/></method>
    <method name="SecondaryActivate"><arg type="i" name="x" direction="in"/><arg type="i" name="y" direction="in"/></method>
    <method name="ContextMenu"><arg type="i" name="x" direction="in"/><arg type="i" name="y" direction="in"/></method>
    <method name="Scroll"><arg type="i" name="delta" direction="in"/><arg type="s" name="orientation" direction="in"/></method>
    <signal name="NewIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg type="s" name="status"/></signal>
  </interface>
</node>
"""
WATCHER = "org.kde.StatusNotifierWatcher"


def panel_dark():
    """COSMIC-Theme dunkel? (Pixmaps kann das Panel nicht umfärben, deshalb passen wir das Gehäuse selbst an.)"""
    try:
        return (Path.home() / ".config/cosmic/com.system76.CosmicTheme.Mode/v1/is_dark").read_text().strip() == "true"
    except OSError:
        return False


def render_icon(state, size, dark=False):
    """camera-web-symbolic (Form aus dem Cosmic-Icon-Theme) mit farbiger Linse als SNI-Pixmap
    (ARGB32, Big-Endian, nicht vormultipliziert)."""
    import cairo
    import numpy as np
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(surf)
    cr.scale(size / 16, size / 16)
    body = (0.93, 0.93, 0.93) if dark else (0.137, 0.137, 0.137)
    cr.set_source_rgb(*body)
    cr.arc(8, 7, 6, 0, 2 * math.pi), cr.fill()                 # Kopf
    cr.move_to(4, 12), cr.curve_to(2, 12, 2, 14, 2, 14)        # Fuß
    cr.line_to(2, 15), cr.line_to(14, 15), cr.line_to(14, 14)
    cr.curve_to(14, 14, 14, 12, 12, 12), cr.close_path(), cr.fill()
    cr.set_source_rgb(*LENS[state])                            # Linse (im Original ein Loch)
    cr.arc(8, 7, 2.9, 0, 2 * math.pi), cr.fill()
    cr.set_source_rgba(1, 1, 1, .6)                            # Glanzpunkt
    cr.arc(7.1, 6.1, 0.8, 0, 2 * math.pi), cr.fill()
    surf.flush()
    a = np.frombuffer(surf.get_data(), np.uint8).reshape(size, size, 4).astype(np.float32)   # B G R A
    alpha = a[..., 3:4]
    rgb = np.where(alpha > 0, a[..., :3] * 255 / np.maximum(alpha, 1), 0).clip(0, 255)
    argb = np.concatenate([alpha, rgb[..., ::-1]], axis=2).astype(np.uint8)
    return (size, size, list(argb.tobytes()))

MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePaths" type="as" access="read"/>
    <method name="GetLayout">
      <arg type="i" name="parentId" direction="in"/><arg type="i" name="recursionDepth" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="u" name="revision" direction="out"/><arg type="(ia{sv}av)" name="layout" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg type="ai" name="ids" direction="in"/><arg type="as" name="propertyNames" direction="in"/>
      <arg type="a(ia{sv})" name="properties" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg type="i" name="id" direction="in"/><arg type="s" name="name" direction="in"/><arg type="v" name="value" direction="out"/>
    </method>
    <method name="Event">
      <arg type="i" name="id" direction="in"/><arg type="s" name="eventId" direction="in"/>
      <arg type="v" name="data" direction="in"/><arg type="u" name="timestamp" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg type="a(isvu)" name="events" direction="in"/><arg type="ai" name="idErrors" direction="out"/>
    </method>
    <method name="AboutToShow"><arg type="i" name="id" direction="in"/><arg type="b" name="needUpdate" direction="out"/></method>
    <method name="AboutToShowGroup">
      <arg type="ai" name="ids" direction="in"/><arg type="ai" name="updatesNeeded" direction="out"/><arg type="ai" name="idErrors" direction="out"/>
    </method>
    <signal name="LayoutUpdated"><arg type="u" name="revision"/><arg type="i" name="parent"/></signal>
    <signal name="ItemsPropertiesUpdated"><arg type="a(ia{sv})" name="updatedProps"/><arg type="a(ias)" name="removedProps"/></signal>
  </interface>
</node>
"""
MENU_PATH = "/MenuBar"


class TrayIcon:
    """Minimaler StatusNotifierItem mit dbusmenu (Rechtsklick-Menü)."""
    PATH, IFACE = "/StatusNotifierItem", "org.kde.StatusNotifierItem"

    def __init__(self, on_activate, on_secondary, on_quit):
        self.on_activate, self.on_secondary, self.on_quit = on_activate, on_secondary, on_quit
        self.icon, self.tip = ICON_OFF, ""
        self.toggle_label, self.revision = "Autofokus einschalten", 1
        self._icons, self.dark = {}, panel_dark()
        self.conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        node = Gio.DBusNodeInfo.new_for_xml(SNI_XML)
        self.conn.register_object(self.PATH, node.interfaces[0], self._call, self._get, lambda *a: False)
        menu = Gio.DBusNodeInfo.new_for_xml(MENU_XML)
        self.conn.register_object(MENU_PATH, menu.interfaces[0], self._menu_call, self._menu_get, lambda *a: False)
        self.name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        Gio.bus_own_name_on_connection(self.conn, self.name, Gio.BusNameOwnerFlags.NONE, None, None)
        # Meldet sich (neu) an, sobald der Watcher des Panels da ist — auch nach einem Panel-Neustart.
        Gio.bus_watch_name_on_connection(self.conn, WATCHER, Gio.BusNameWatcherFlags.NONE,
                                         lambda *a: self._register(), None)

    def _register(self):
        self.conn.call(WATCHER, "/StatusNotifierWatcher", WATCHER, "RegisterStatusNotifierItem",
                       GLib.Variant("(s)", (self.name,)), None, Gio.DBusCallFlags.NONE, -1, None, None)

    def _pixmaps(self):
        key = (self.icon, self.dark)
        if key not in self._icons:
            self._icons[key] = [render_icon(self.icon, n, self.dark) for n in (22, 32, 48, 64)]
        return self._icons[key]

    def _get(self, conn, sender, path, iface, prop):
        v = GLib.Variant
        return {
            "Category": v("s", "ApplicationStatus"), "Id": v("s", "webcam-autofocus"),
            "Title": v("s", "Webcam Autofokus"), "Status": v("s", "Active"), "WindowId": v("u", 0),
            "IconName": v("s", ""), "IconPixmap": v("a(iiay)", self._pixmaps()),
            "OverlayIconName": v("s", ""), "AttentionIconName": v("s", ""),
            "ToolTip": v("(sa(iiay)ss)", ("", [], "Webcam Autofokus", self.tip)),
            "ItemIsMenu": v("b", False), "Menu": v("o", MENU_PATH),
        }.get(prop)

    def _call(self, conn, sender, path, iface, method, params, invocation):
        if method == "Activate":
            GLib.idle_add(self.on_activate)
        elif method in ("SecondaryActivate", "ContextMenu"):
            GLib.idle_add(self.on_secondary)
        invocation.return_value(None)

    # ---------- dbusmenu ----------
    def _items(self):
        """(id, Eigenschaften) der Menüeinträge; id 0 ist die Wurzel."""
        v = GLib.Variant
        def item(label):
            return {"label": v("s", label), "type": v("s", "standard"), "enabled": v("b", True), "visible": v("b", True)}

        return [(2, item(self.toggle_label)), (1, item("Fenster öffnen")),
                (3, {"type": v("s", "separator"), "enabled": v("b", True), "visible": v("b", True)}),
                (4, item("Beenden"))]

    def _menu_get(self, conn, sender, path, iface, prop):
        v = GLib.Variant
        return {"Version": v("u", 3), "TextDirection": v("s", "ltr"), "Status": v("s", "normal"),
                "IconThemePaths": v("as", [])}.get(prop)

    def _menu_event(self, item, event):
        if event != "clicked":
            return
        action = {1: self.on_secondary, 2: self.on_activate, 4: self.on_quit}.get(item)
        if action:
            GLib.idle_add(action)

    def _menu_call(self, conn, sender, path, iface, method, params, invocation):
        v = GLib.Variant
        if method == "GetLayout":
            children = [v("(ia{sv}av)", (i, props, [])) for i, props in self._items()]    # av boxt selbst: nicht doppelt verpacken
            invocation.return_value(v("(u(ia{sv}av))", (self.revision, (0, {"children-display": v("s", "submenu")}, children))))
        elif method == "GetGroupProperties":
            ids = params.unpack()[0]
            invocation.return_value(v("(a(ia{sv}))", ([(i, p) for i, p in self._items() if not ids or i in ids],)))
        elif method == "GetProperty":
            i, name = params.unpack()
            invocation.return_value(v("(v)", (dict(self._items()).get(i, {}).get(name, v("s", "")),)))
        elif method == "Event":
            self._menu_event(params.unpack()[0], params.unpack()[1])
            invocation.return_value(None)
        elif method == "EventGroup":
            for i, event, _, _ in params.unpack()[0]:
                self._menu_event(i, event)
            invocation.return_value(v("(ai)", ([],)))
        elif method == "AboutToShow":
            invocation.return_value(v("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(v("(aiai)", ([], [])))
        else:
            invocation.return_value(None)

    def update(self, icon, tip, toggle_label=None):
        if toggle_label and toggle_label != self.toggle_label:
            self.toggle_label = toggle_label
            self.revision += 1
            self.conn.emit_signal(None, MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated",
                                  GLib.Variant("(ui)", (self.revision, 0)))
        dark = panel_dark()
        if icon != self.icon or dark != self.dark:
            self.icon, self.dark = icon, dark
            self.conn.emit_signal(None, self.PATH, self.IFACE, "NewIcon", None)
        if tip != self.tip:
            self.tip = tip
            self.conn.emit_signal(None, self.PATH, self.IFACE, "NewToolTip", None)


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app, engine):
        super().__init__(application=app, title="Webcam Autofokus", default_width=460)
        self.engine, self.app = engine, app
        self._disp = float(engine.a.fmin)       # angezeigte Knopfposition (gleitet zum Fokuswert)
        self._syncing = False

        header = Adw.HeaderBar()
        self.switch = Gtk.Switch(valign=Gtk.Align.CENTER, tooltip_text="Autofokus ein/aus")
        self.switch.connect("notify::active", self._on_switch)
        header.pack_start(self.switch)
        self.preview_btn = Gtk.ToggleButton(label="Vorschau", tooltip_text="Zeigt das Bild, das in Zoom ankommt")
        self.preview_btn.connect("toggled", self._on_preview)
        header.pack_end(self.preview_btn)

        self.banner = Adw.Banner(title="", revealed=False)
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True)
        self.picture.set_size_request(-1, 250)
        hint = Gtk.Label(label="Autofokus einschalten, um die Vorschau zu sehen", css_classes=["dim-label"])
        self.pv_stack = Gtk.Stack()
        self.pv_stack.add_named(self.picture, "pic")
        self.pv_stack.add_named(hint, "hint")
        self.overlay_chk = Gtk.CheckButton(label="Gesichtsrahmen zeigen", halign=Gtk.Align.START, margin_start=2)
        self.overlay_chk.connect("toggled", lambda c: setattr(self.engine, "preview_overlay", c.get_active()))
        pv_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_start=12, margin_end=12, margin_top=12)
        pv_box.append(self.pv_stack)
        pv_box.append(self.overlay_chk)
        self.revealer = Gtk.Revealer(child=pv_box, reveal_child=False)
        self._pv_t = 0.0
        self.slider = Gtk.DrawingArea(content_height=110, margin_start=12, margin_end=12, margin_top=12)
        self.slider.set_draw_func(self._draw_slider)
        self.graph = Gtk.DrawingArea(content_height=140, margin_start=12, margin_end=12, margin_top=6)
        self.graph.set_draw_func(self._draw_graph)
        self.status = Gtk.Label(xalign=0, margin_start=16, margin_end=16, margin_top=6)
        self.log = Gtk.Label(xalign=0, yalign=0, vexpand=True, margin_start=16, margin_end=16, margin_top=6,
                             margin_bottom=12, css_classes=["dim-label", "monospace"])

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.every = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 1, 30, 1)
        self.every.set_value(engine.a.eval_every)
        self.every.set_digits(0)
        self.every.set_draw_value(True)
        self.every.set_value_pos(Gtk.PositionType.RIGHT)
        for m in (1, 5, 10, 20, 30):
            self.every.add_mark(m, Gtk.PositionType.BOTTOM, str(m))
        self.every.set_tooltip_text("Nur jedes x-te Bild auswerten (1 = jedes). Höher = weniger CPU, trägere Reaktion.")
        self.every.connect("value-changed", lambda s: setattr(engine.a, "eval_every", int(s.get_value())))
        ev_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_start=12, margin_end=12, margin_top=12)
        self.dyn = Gtk.CheckButton(label="dynamisch (nach CPU-Last)", active=engine.a.dynamic)
        self.dyn.set_tooltip_text("Der Regler wird automatisch verschoben: hohe CPU-Last des Programms → seltener auswerten, wenig Last → öfter.")
        self.dyn.connect("toggled", lambda b: setattr(engine.a, "dynamic", b.get_active()))
        head = Gtk.Box(spacing=12)
        head.append(Gtk.Label(label="Auswertung: nur jedes x-te Bild", halign=Gtk.Align.START, hexpand=True))
        head.append(self.dyn)
        ev_box.append(head)
        ev_box.append(self.every)
        for w in (self.banner, self.revealer, ev_box, self.slider, self.graph, self.status, self.log):
            box.append(w)
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(box)
        self.set_content(view)
        self.connect("close-request", lambda w: (w.set_visible(False), True)[1])   # nur verstecken

    def _on_switch(self, switch, _pspec):
        if not self._syncing:
            self.app.set_enabled(switch.get_active())

    def _on_preview(self, btn):
        self.revealer.set_reveal_child(btn.get_active())
        self.engine.preview_on = btn.get_active()

    def sync(self):
        """Vom Timer aufgerufen: Schalter, Banner und Texte an den Engine-Zustand angleichen."""
        e = self.engine
        self._syncing = True
        self.switch.set_active(e.active)
        self._syncing = False
        self.banner.set_title(e.error or "")
        self.banner.set_revealed(bool(e.error))
        if not self.is_visible():
            e.preview_on = False                               # Fenster zu: kein Vorschaubild berechnen
            return
        e.preview_on = self.preview_btn.get_active()
        if e.a.dynamic and int(self.every.get_value()) != e.a.eval_every:
            self.every.set_value(e.a.eval_every)             # Regler folgt der automatischen Anpassung
        if e.preview_on:
            if e.active and e.preview and e.preview_t != self._pv_t:
                self._pv_t = e.preview_t
                w, h, data = e.preview
                self.picture.set_paintable(Gdk.MemoryTexture.new(
                    w, h, Gdk.MemoryFormat.B8G8R8, GLib.Bytes.new(data), w * 3))
            self.pv_stack.set_visible_child_name("pic" if e.active and e.preview else "hint")
        self._disp += (e.focus - self._disp) * 0.35
        face = f"{e.box[2]} px" if e.box else "—"
        sharp = f"{e.ema:.0f}" if e.ema is not None else "—"
        self.status.set_text(f"{e.mode}  ·  Schärfe {sharp}  ·  Gesicht {face}")
        self.log.set_text("\n".join(list(e.log)[-6:]))
        self.slider.queue_draw()
        self.graph.queue_draw()

    def _accent(self):
        e = self.engine
        if e.error:
            return RED
        return ORANGE if e.mode == "suche Fokus" else GREEN

    def _draw_slider(self, area, cr, w, h):
        e = self.engine
        fg = area.get_color()
        pad, y = 28, 46
        lo, hi = e.a.fmin, e.a.fmax
        frac = min(max((self._disp - lo) / (hi - lo), 0.0), 1.0)
        x = pad + frac * (w - 2 * pad)
        r, g, b = self._accent() if e.active else (fg.red, fg.green, fg.blue)
        cr.set_line_cap(1)                                    # rund
        cr.set_line_width(7)
        cr.set_source_rgba(fg.red, fg.green, fg.blue, .22)
        cr.move_to(pad, y), cr.line_to(w - pad, y), cr.stroke()
        cr.set_source_rgba(r, g, b, 1 if e.active else .4)
        cr.move_to(pad, y), cr.line_to(x, y), cr.stroke()
        cr.arc(x, y, 12, 0, 2 * math.pi), cr.fill()
        cr.set_source_rgba(1, 1, 1, 1)
        cr.arc(x, y, 4, 0, 2 * math.pi), cr.fill()
        cr.set_font_size(11)
        cr.set_source_rgba(fg.red, fg.green, fg.blue, .6)
        cr.move_to(pad - 4, y - 20), cr.show_text(str(lo))
        cr.move_to(w - pad - 18, y - 20), cr.show_text(str(hi))
        text = str(e.focus)
        cr.set_font_size(22)
        tw = cr.text_extents(text).x_advance
        changed = time.time() - e.changed_t < 3
        cr.set_source_rgba(*((r, g, b) if changed else (fg.red, fg.green, fg.blue)), 1)
        cr.move_to(min(max(x - tw / 2, pad - 4), w - pad + 4 - tw), y + 42)
        cr.show_text(text)
        if changed:
            cr.set_font_size(11)
            cr.set_source_rgba(r, g, b, 1)
            cr.move_to(pad - 4, h - 4), cr.show_text("◀ Fokus geändert")

    def _draw_graph(self, area, cr, w, h):
        e = self.engine
        fg = area.get_color()
        try:
            hist = list(e.history)
        except RuntimeError:
            return
        now = time.time()
        hist = [p for p in hist if now - p[0] <= GRAPH_SECONDS]
        top, bottom = 22, h - 6
        cr.set_source_rgba(fg.red, fg.green, fg.blue, .08)
        cr.rectangle(0, top, w, bottom - top), cr.fill()
        cr.set_font_size(11)
        r, g, b = self._accent()
        cr.set_source_rgba(r, g, b, 1)
        cr.move_to(4, 14), cr.show_text("■ Schärfe")
        cr.set_source_rgba(fg.red, fg.green, fg.blue, .8)
        cr.move_to(80, 14), cr.show_text("— Fokus")
        if len(hist) < 2:
            return
        xs = [w * (1 - (now - p[0]) / GRAPH_SECONDS) for p in hist]
        smax = max(p[2] for p in hist) or 1.0
        span = e.a.fmax - e.a.fmin
        sharp = [bottom - (p[2] / smax) * (bottom - top - 4) for p in hist]
        focus = [bottom - ((p[1] - e.a.fmin) / span) * (bottom - top - 4) for p in hist]
        cr.set_source_rgba(r, g, b, .25)                       # Schärfe als Fläche + Linie
        cr.move_to(xs[0], bottom)
        for x, y in zip(xs, sharp):
            cr.line_to(x, y)
        cr.line_to(xs[-1], bottom), cr.close_path(), cr.fill()
        cr.set_source_rgba(r, g, b, 1), cr.set_line_width(1.5)
        for i, (x, y) in enumerate(zip(xs, sharp)):
            (cr.move_to if i == 0 else cr.line_to)(x, y)
        cr.stroke()
        cr.set_source_rgba(fg.red, fg.green, fg.blue, .8), cr.set_line_width(1.5)
        for i, (x, y) in enumerate(zip(xs, focus)):
            (cr.move_to if i == 0 else cr.line_to)(x, y)
        cr.stroke()


class App(Adw.Application):
    def __init__(self, cfg, autostart=False, hidden=False):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.autostart, self._first_activate, self.hidden = autostart, True, hidden
        self.engine = AutoFocus(cfg)
        self.window = None
        self.tray = None
        self._last_error = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        self.hold()                                            # läuft weiter, wenn das Fenster zu ist
        self.tray = TrayIcon(on_activate=self.toggle, on_secondary=self.show_window, on_quit=self.quit)
        GLib.timeout_add(100, self._tick)
        if self.autostart:
            self.engine.start()

    def do_activate(self):
        first, self._first_activate = self._first_activate, False
        if first and self.hidden:                              # --hidden (z. B. Autostart): nur das Icon
            return
        self.show_window()                                     # Start aus dem Menü / erneuter Aufruf: Fenster zeigen

    def show_window(self):
        if self.window is None:
            self.window = MainWindow(self, self.engine)
        self.window.present()

    def toggle(self):
        self.set_enabled(not self.engine.active)

    def set_enabled(self, on):
        if on and not self.engine.active:
            self.engine.start()
        elif not on and self.engine.active:
            threading.Thread(target=self.engine.stop, daemon=True).start()   # Stop wartet auf den Thread

    def _tick(self):
        e = self.engine
        if e.error:
            icon, tip = ICON_ERR, e.error
        elif e.active:
            icon, tip = ICON_ON, f"Fokus {e.focus} · {e.mode}"
        else:
            icon, tip = ICON_OFF, "Autofokus aus"
        self.tray.update(icon, tip, "Autofokus ausschalten" if e.active else "Autofokus einschalten")
        if e.error and e.error != self._last_error:
            note = Gio.Notification.new("Webcam Autofokus")
            note.set_body(e.error)
            self.send_notification("error", note)
        self._last_error = e.error
        if self.window:
            self.window.sync()
        return True

    def do_shutdown(self):
        self.engine.stop()                                     # Kamera freigeben, Autofokus zurückschalten
        Adw.Application.do_shutdown(self)


def main():
    p = argparse.ArgumentParser(description="Webcam-Autofokus als Tray-App")
    add_config_args(p)
    p.add_argument("--start", action="store_true", help="Autofokus sofort einschalten")
    p.add_argument("--hidden", action="store_true", help="beim Start kein Fenster zeigen, nur das Panel-Icon")
    args = p.parse_args()
    cfg = Config(**{f.name: getattr(args, f.name) for f in dataclasses.fields(Config)})
    app = App(cfg, autostart=args.start, hidden=args.hidden)
    sys.exit(app.run([sys.argv[0]]))


if __name__ == "__main__":
    main()
