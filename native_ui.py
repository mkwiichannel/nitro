"""Native (Qt) launcher UI for Mario Kart Nitro.

Replaces the WebView2 front-end for everyday use: no browser engine, no
msedgewebview2.exe process tree, no JS bridge round-trips. Every value
the old page needed is read straight from main.Api, in-process.

The Mii editor (three.js / WebGL) is the only thing that still needs a
browser engine, so the "Mii" tab just starts `MarioKartNitro.exe --mii`
as a separate process, on demand, and that process exits when its window
closes -- WebView2 is never running while you just launch the game.
"""
import json
import os
import subprocess
import sys
import threading

from PySide6.QtCore import QObject, QTimer, Qt, Signal
from PySide6.QtCore import QRectF
from PySide6.QtGui import (QColor, QDesktopServices, QFont, QIcon, QLinearGradient,
                           QPainter, QPainterPath, QPen, QPixmap)
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QPlainTextEdit,
    QProgressBar, QPushButton, QSizePolicy, QStackedWidget, QVBoxLayout,
    QWidget,
)

LANGUAGES = [
    ("en", "English"), ("de", "Deutsch"), ("fr", "Français"),
    ("es", "Español"), ("it", "Italiano"), ("pt", "Português"),
    ("nl", "Nederlands"), ("pl", "Polski"), ("tr", "Türkçe"),
    ("ru", "Русский"), ("ja", "日本語"), ("ko", "한국어"), ("zh", "中文"),
    ("ar", "العربية"), ("sv", "Svenska"), ("fi", "Suomi"),
]

# Mii-tab strings (the editor itself is the web page, these only cover
# the launcher-side button). English fallback for every language.
MII_TEXT = {
    "en": ("Mii Editor", "Create and edit your Nitro Miis. They sync into Dolphin when you press Play.", "Open Mii editor"),
    "de": ("Mii-Editor", "Erstelle und bearbeite deine Nitro-Miis. Sie werden beim Spielen mit Dolphin synchronisiert.", "Mii-Editor öffnen"),
    "fr": ("Éditeur de Mii", "Crée et modifie tes Mii Nitro. Ils sont synchronisés avec Dolphin au lancement.", "Ouvrir l'éditeur de Mii"),
    "es": ("Editor de Mii", "Crea y edita tus Mii de Nitro. Se sincronizan con Dolphin al pulsar Jugar.", "Abrir editor de Mii"),
}

DEFAULT_PALETTE = {
    "--bg": "#0a0512", "--bg-2": "#120a1f", "--panel": "#1a0e2b",
    "--panel-hover": "#241340", "--blue": "#ff8c00", "--blue-2": "#ffa513",
    "--cyan": "#c084fc", "--text": "#e8eefb", "--text-dim": "#8b9ab3",
    "--text-faint": "#56637c", "--green": "#ff8c00",
}


def _load_translations(resource_path):
    try:
        with open(resource_path("translations.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"en": {}}


def _is_hex_color(v):
    return isinstance(v, str) and v.startswith("#") and len(v) in (4, 7)


class _Bridge(QObject):
    """Thread -> UI hand-off. Everything slow (network, subprocess) runs
    on a plain thread; its result comes back through a queued signal."""
    call = Signal(object, object)          # (callback, result)
    launcher_update_error = Signal(str)
    quit_requested = Signal()
    launcher_update_available = Signal(str)


class BannerLabel(QLabel):
    def __init__(self):
        super().__init__()
        self._pm = None
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

    def set_path(self, path):
        pm = QPixmap(path) if path and os.path.isfile(path) else QPixmap()
        self._pm = pm if not pm.isNull() else None
        self._rescale()

    def _rescale(self):
        if self._pm is None:
            self.clear()
            self.setFixedHeight(0)
            return
        w = max(200, self.width())
        scaled = self._pm.scaledToWidth(w, Qt.SmoothTransformation)
        if scaled.height() > 260:  # centre-crop to a hero strip
            y = (scaled.height() - 260) // 2
            scaled = scaled.copy(0, y, scaled.width(), 260)
        self.setPixmap(scaled)
        self.setFixedHeight(scaled.height())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._rescale()


class HeroFrame(QFrame):
    """Home hero: the whole banner, with the buttons in front of it and a fade
    at the bottom (same look as the web UI)."""
    def __init__(self):
        super().__init__()
        self.setObjectName("hero")
        self._pm = None
        self._scaled = None
        self._scaled_for = None
        self.border = QColor("#241340")

    def set_path(self, path):
        pm = QPixmap(path) if path and os.path.isfile(path) else QPixmap()
        self._pm = pm if not pm.isNull() else None
        self._scaled_for = None
        self.update()

    def paintEvent(self, e):
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        clip = QPainterPath()
        clip.addRoundedRect(r, 18, 18)
        p.setClipPath(clip)
        p.fillRect(self.rect(), QColor("#07040d"))
        if self._pm is not None:
            size = self.size()
            if self._scaled_for != size:
                self._scaled = self._pm.scaled(size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self._scaled_for = size
            x = (size.width() - self._scaled.width()) // 2
            y = (size.height() - self._scaled.height()) // 2
            p.drawPixmap(x, y, self._scaled)
        h = self.height()
        g = QLinearGradient(0, h * 0.54, 0, h)
        g.setColorAt(0.0, QColor(10, 5, 18, 0))
        g.setColorAt(0.62, QColor(10, 5, 18, 184))
        g.setColorAt(1.0, QColor(10, 5, 18, 255))
        p.fillRect(self.rect(), g)
        p.setClipping(False)
        p.setPen(QPen(self.border, 1))
        p.drawRoundedRect(r, 18, 18)


class LauncherWindow(QMainWindow):
    def __init__(self, api, main_module):
        super().__init__()
        self.api = api
        self.m = main_module
        self.resource_path = main_module.resource_path
        self.translations = _load_translations(self.resource_path)
        self.lang = "en"
        self._playing = False
        self._launcher_popup_shown = False
        self._cfg_mtime = None
        self._mii_proc = None
        self._browse_buttons = []

        self.bridge = _Bridge()
        self.bridge.call.connect(lambda cb, res: cb(res))
        self.bridge.launcher_update_error.connect(self._on_launcher_update_error)
        self.bridge.quit_requested.connect(QApplication.instance().quit)
        self.bridge.launcher_update_available.connect(self._show_launcher_popup)
        api.on_launcher_update_error = self.bridge.launcher_update_error.emit
        api.on_request_quit = self.bridge.quit_requested.emit

        self.setWindowTitle("Mario Kart Nitro — Launcher")
        self.resize(1100, 760)
        self.setMinimumSize(860, 620)
        icon = self.resource_path("logo.png")
        if os.path.isfile(icon):
            self.setWindowIcon(QIcon(icon))

        self._build()
        state = api.get_state()
        self.apply_state(state)

        # Pick up remote theme changes the background updater writes to
        # config.json (cheap stat() once a second; no web page involved).
        self._watch = QTimer(self)
        self._watch.timeout.connect(self._poll_config)
        self._watch.start(1500)
        QTimer.singleShot(600, self._startup_update_check)

    # ------------------------------------------------------------ helpers
    def t(self, key):
        return (self.translations.get(self.lang, {}).get(key)
                or self.translations.get("en", {}).get(key) or key)

    def run_bg(self, fn, cb):
        def work():
            try:
                res = fn()
            except Exception as e:  # noqa: BLE001 - surfaced to the UI
                res = {"ok": False, "error": str(e)}
            self.bridge.call.emit(cb, res)
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ build UI
    def _build(self):
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # title bar
        bar = QFrame()
        bar.setObjectName("bar")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(24, 12, 24, 12)
        self.logo = QLabel()
        self.logo.setFixedSize(30, 30)
        self.brand = QLabel()
        self.brand.setObjectName("brand")
        self.ver = QLabel()
        self.ver.setObjectName("pill")
        bl.addWidget(self.logo)
        bl.addWidget(self.brand)
        bl.addWidget(self.ver)
        bl.addStretch(1)
        self.tab_buttons = {}
        for key in ("home", "mii", "credits", "settings"):
            b = QPushButton()
            b.setObjectName("tab")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=key: self.show_screen(k))
            self.tab_buttons[key] = b
            bl.addWidget(b)
        outer.addWidget(bar)

        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)
        self.screens = {}
        for key, builder in (("home", self._build_home), ("mii", self._build_mii),
                             ("credits", self._build_credits),
                             ("settings", self._build_settings),
                             ("setup", self._build_setup)):
            page = builder()
            self.screens[key] = page
            self.stack.addWidget(page)

        # launcher-update overlay is a modal dialog, built lazily
        self.show_screen("home")

    def _page(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(40, 28, 40, 28)
        lay.setSpacing(16)
        return w, lay

    def _build_home(self):
        w, lay = self._page()
        self.banner = HeroFrame()
        hl = QVBoxLayout(self.banner)
        hl.setContentsMargins(28, 0, 28, 22)
        hl.setSpacing(10)
        hl.addStretch(1)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        self.progress.hide()
        hl.addWidget(self.progress)
        self.notice = QLabel()
        self.notice.setWordWrap(True)
        self.notice.setObjectName("notice")
        self.notice.hide()
        hl.addWidget(self.notice)
        row = QHBoxLayout()
        self.play_btn = QPushButton()
        self.play_btn.setObjectName("primary")
        self.play_btn.setMinimumHeight(44)
        self.play_btn.setMinimumWidth(110)
        self.play_btn.setCursor(Qt.PointingHandCursor)
        self.play_btn.clicked.connect(self.do_play)
        self.hero_close = QPushButton()
        self.hero_close.setObjectName("ghost")
        self.hero_close.setMinimumHeight(44)
        self.hero_close.setCursor(Qt.PointingHandCursor)
        self.hero_close.clicked.connect(QApplication.instance().quit)
        row.addWidget(self.play_btn)
        row.addWidget(self.hero_close)
        row.addStretch(1)
        hl.addLayout(row)
        lay.addWidget(self.banner, 1)

        stats = QHBoxLayout()
        stats.setSpacing(14)
        self.stat_labels, self.stat_vals = {}, {}
        for key in ("Dolphin", "Iso", "ActiveMod", "Launcher"):
            box = QFrame()
            box.setObjectName("stat")
            bl = QVBoxLayout(box)
            lab = QLabel()
            lab.setObjectName("statLabel")
            val = QLabel()
            val.setObjectName("statVal")
            val.setWordWrap(False)
            val.setTextInteractionFlags(Qt.NoTextInteraction)
            bl.addWidget(lab)
            bl.addWidget(val)
            self.stat_labels[key], self.stat_vals[key] = lab, val
            stats.addWidget(box, 1)
        lay.addLayout(stats)

        cards = QHBoxLayout()
        cards.setSpacing(14)
        self.cards = []
        for target in ("play", "settings", "credits"):
            c = QPushButton()
            c.setObjectName("card")
            c.setCursor(Qt.PointingHandCursor)
            c.setMinimumHeight(120)
            cl = QVBoxLayout(c)
            ct = QLabel(); ct.setObjectName("cardTitle")
            cd = QLabel(); cd.setObjectName("cardDesc"); cd.setWordWrap(True)
            cd.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            for lab in (ct, cd):
                lab.setAttribute(Qt.WA_TransparentForMouseEvents)
            cl.addWidget(ct); cl.addWidget(cd, 1)
            c._t, c._d = ct, cd
            if target == "play":
                c.clicked.connect(self.do_play)
            else:
                c.clicked.connect(lambda _=False, k=target: self.show_screen(k))
            self.cards.append((target, c))
            cards.addWidget(c, 1)
        lay.addLayout(cards)
        return w

    def _build_mii(self):
        w, lay = self._page()
        self.mii_title = QLabel()
        self.mii_title.setObjectName("h2")
        self.mii_desc = QLabel()
        self.mii_desc.setWordWrap(True)
        self.mii_desc.setObjectName("dim")
        self.mii_btn = QPushButton()
        self.mii_btn.setObjectName("primary")
        self.mii_btn.setMinimumHeight(46)
        self.mii_btn.setMaximumWidth(280)
        self.mii_btn.setCursor(Qt.PointingHandCursor)
        self.mii_btn.clicked.connect(self.open_mii_editor)
        lay.addWidget(self.mii_title)
        lay.addWidget(self.mii_desc)
        lay.addWidget(self.mii_btn)
        lay.addStretch(1)
        return w

    def _sub_head(self, lay):
        row = QHBoxLayout()
        back = QPushButton()
        back.setObjectName("ghost")
        back.clicked.connect(lambda: self.show_screen("home"))
        title = QLabel()
        title.setObjectName("h2")
        tag = QLabel()
        tag.setObjectName("dim")
        row.addWidget(back)
        row.addWidget(title)
        row.addWidget(tag)
        row.addStretch(1)
        lay.addLayout(row)
        return back, title, tag

    def _path_row(self, kind, filt):
        row = QHBoxLayout()
        edit = QLineEdit()
        edit.setReadOnly(True)
        btn = QPushButton()
        btn.setObjectName("ghost")
        btn.setCursor(Qt.PointingHandCursor)

        def browse():
            if kind == "folder":
                p = QFileDialog.getExistingDirectory(self, "", edit.text())
            else:
                p, _ = QFileDialog.getOpenFileName(self, "", edit.text(), filt)
            if p:
                edit.setText(os.path.normpath(p))
        btn.clicked.connect(browse)
        row.addWidget(edit, 1)
        row.addWidget(btn)
        self._browse_buttons.append(btn)
        return row, edit

    def _build_settings(self):
        w, lay = self._page()
        self.set_back, self.set_title, self.set_tag = self._sub_head(lay)
        panel = QFrame()
        panel.setObjectName("panel")
        pl = QVBoxLayout(panel)
        pl.setSpacing(8)

        self.lbl_dolphin_path = QLabel()
        r, self.set_dolphin = self._path_row("file", "Dolphin (*.exe);;All files (*)")
        pl.addWidget(self.lbl_dolphin_path); pl.addLayout(r)
        self.lbl_iso_path = QLabel()
        r, self.set_iso = self._path_row("file", "Wii disc image (*.iso *.wbfs *.rvz);;All files (*)")
        pl.addWidget(self.lbl_iso_path); pl.addLayout(r)
        self.lbl_mod_dir = QLabel()
        r, self.set_moddir = self._path_row("folder", "")
        pl.addWidget(self.lbl_mod_dir); pl.addLayout(r)

        self.lbl_res = QLabel()
        self.set_res = QLineEdit()
        self.set_res.setMaximumWidth(220)
        pl.addWidget(self.lbl_res); pl.addWidget(self.set_res)
        self.lbl_lang = QLabel()
        self.set_lang = QComboBox()
        self.set_lang.setMaximumWidth(220)
        for code, name in LANGUAGES:
            self.set_lang.addItem(name, code)
        pl.addWidget(self.lbl_lang); pl.addWidget(self.set_lang)
        self.set_full = QCheckBox()
        self.set_auto = QCheckBox()
        pl.addWidget(self.set_full); pl.addWidget(self.set_auto)

        self.save_btn = QPushButton()
        self.save_btn.setObjectName("primary")
        self.save_btn.setCursor(Qt.PointingHandCursor)
        self.save_btn.setMaximumWidth(220)
        self.save_btn.clicked.connect(self.save_settings)
        pl.addWidget(self.save_btn)

        lay.addWidget(panel)
        lay.addStretch(1)
        return w

    def _toggle_text(self, fn):
        if self.diag_text.isVisible() and getattr(self, "_diag_fn", None) is fn:
            self.diag_text.hide()
            return
        self._diag_fn = fn
        self.diag_text.setPlainText("…")
        self.diag_text.show()
        self.run_bg(fn, lambda res: self.diag_text.setPlainText(str(res)))

    def _build_credits(self):
        w, lay = self._page()
        self.cr_back, self.cr_title, _tag = self._sub_head(lay)
        panel = QFrame(); panel.setObjectName("panel")
        pl = QVBoxLayout(panel)
        self.cr_blocks = []
        for _ in range(4):
            h = QLabel(); h.setObjectName("h4")
            p = QLabel(); p.setWordWrap(True); p.setObjectName("dim")
            pl.addWidget(h); pl.addWidget(p)
            self.cr_blocks.append((h, p))
        self.discord_btn = QPushButton()
        self.discord_btn.setObjectName("primary")
        self.discord_btn.setMaximumWidth(240)
        self.discord_btn.setCursor(Qt.PointingHandCursor)
        self.discord_btn.clicked.connect(lambda: self.api.open_discord())
        pl.addWidget(self.discord_btn)
        lay.addWidget(panel)
        lay.addStretch(1)
        return w

    def _build_setup(self):
        w, lay = self._page()
        self.su_back, self.su_title, _tag = self._sub_head(lay)
        self.su_intro = QLabel(); self.su_intro.setObjectName("dim"); self.su_intro.setWordWrap(True)
        lay.addWidget(self.su_intro)
        panel = QFrame(); panel.setObjectName("panel")
        pl = QVBoxLayout(panel)
        self.su_lbl_dolphin = QLabel()
        r, self.su_dolphin = self._path_row("file", "Dolphin (*.exe);;All files (*)")
        pl.addWidget(self.su_lbl_dolphin); pl.addLayout(r)
        self.su_lbl_iso = QLabel()
        r, self.su_iso = self._path_row("file", "Wii disc image (*.iso *.wbfs *.rvz);;All files (*)")
        pl.addWidget(self.su_lbl_iso); pl.addLayout(r)
        self.su_continue = QPushButton()
        self.su_continue.setObjectName("primary")
        self.su_continue.setMaximumWidth(220)
        self.su_continue.setCursor(Qt.PointingHandCursor)
        self.su_continue.clicked.connect(self.save_setup)
        pl.addWidget(self.su_continue)
        lay.addWidget(panel)
        lay.addStretch(1)
        return w

    # ------------------------------------------------------------ navigation
    def show_screen(self, key):
        self.stack.setCurrentWidget(self.screens[key])
        for k, b in self.tab_buttons.items():
            b.setChecked(k == key)

    # ------------------------------------------------------------ state
    def apply_state(self, s):
        self.state = s
        lang = s.get("language") or "en"
        if lang not in self.translations:
            lang = "en"
        self._apply_theme(s.get("theme_colors") or {})
        self.lang = lang
        app = QApplication.instance()
        app.setLayoutDirection(Qt.RightToLeft if lang == "ar" else Qt.LeftToRight)

        logo = s.get("theme_logo_path") or self.resource_path("logo.png")
        if not os.path.isfile(logo):
            logo = self.resource_path("logo.png")
        pm = QPixmap(logo)
        if not pm.isNull():
            self.logo.setPixmap(pm.scaled(30, 30, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        banner = s.get("theme_banner_path") or self.resource_path("banner.png")
        if not os.path.isfile(banner):
            banner = self.resource_path("banner.png")
        self.banner.set_path(banner)

        self.brand.setText("MARIO KART <span style='color:%s'>NITRO</span>" % self.pal["--cyan"])
        self.brand.setTextFormat(Qt.RichText)
        self.ver.setText("v" + str(s.get("version", "")))
        def _when(iso):
            try:
                import datetime
                return datetime.datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
            except Exception:
                return "-"
        self.ver.setToolTip("Modpack updated: %s\nLauncher updated: %s" % (
            _when(s.get("modpack_updated_at", "")), _when(s.get("launcher_updated_at", ""))))

        self._apply_texts()

        def short(p):
            return os.path.basename(p) if p else ""
        for key, val in (("Dolphin", s.get("dolphin_path")), ("Iso", s.get("iso_path"))):
            lab = self.stat_vals[key]
            lab.setText(short(val) if val else self.t("notSet"))
            lab.setToolTip(val or "")
            lab.setProperty("dim", not val)
        mod = s.get("active_mod")
        self.stat_vals["ActiveMod"].setText(mod if mod else self.t("none"))
        self.stat_vals["ActiveMod"].setProperty("dim", not mod)
        self.stat_vals["Launcher"].setText("v" + str(s.get("version", "")))
        self.stat_vals["Launcher"].setProperty("dim", False)
        for v in self.stat_vals.values():
            v.style().unpolish(v); v.style().polish(v)

        self.set_dolphin.setText(s.get("dolphin_path") or "")
        self.set_iso.setText(s.get("iso_path") or "")
        self.set_moddir.setText(s.get("mod_directory") or "")
        self.set_res.setText(s.get("resolution") or "")
        self.set_lang.setCurrentIndex(max(0, self.set_lang.findData(lang)))
        self.set_full.setChecked(bool(s.get("fullscreen")))
        self.set_auto.setChecked(bool(s.get("auto_update", True)))
        self.su_dolphin.setText(s.get("dolphin_path") or "")
        self.su_iso.setText(s.get("iso_path") or "")

    def _apply_texts(self):
        t = self.t
        names = {"home": t("navHome"), "mii": t("navMii"),
                 "credits": t("navCredits"), "settings": t("navSettings")}
        for k, b in self.tab_buttons.items():
            b.setText(names[k])
        self.hero_close.setText(t("exitBtn"))
        if not self._playing:
            self.play_btn.setText("▶  " + t("playLabel"))
        for key, name in (("Dolphin", "labelDolphin"), ("Iso", "labelIso"),
                          ("ActiveMod", "labelActiveMod"), ("Launcher", "labelLauncher")):
            self.stat_labels[key].setText(t(name).upper())
        titles = {"play": ("▶  " + t("cardPlayTitle"), t("cardPlayDesc")),
                  "settings": ("⚙  " + t("cardSettingsTitle"), t("cardSettingsDesc")),
                  "credits": ("ⓘ  " + t("cardCreditsTitle"), t("cardCreditsDesc"))}
        for target, c in self.cards:
            c._t.setText(titles[target][0]); c._d.setText(titles[target][1])

        mt = MII_TEXT.get(self.lang, MII_TEXT["en"])
        self.mii_title.setText(mt[0]); self.mii_desc.setText(mt[1]); self.mii_btn.setText(mt[2])

        self.set_back.setText(t("settingsBackBtn"))
        self.set_title.setText(t("settingsTitle"))
        self.set_tag.setText(t("settingsTag"))
        self.lbl_dolphin_path.setText(t("labelDolphinPath"))
        self.lbl_iso_path.setText(t("labelIsoPath"))
        self.lbl_mod_dir.setText(t("labelModDir"))
        self.lbl_res.setText(t("labelResolution"))
        self.lbl_lang.setText(t("labelLanguage"))
        self.set_full.setText(t("labelFullscreen"))
        self.set_auto.setText(t("labelAutoUpdate"))
        self.save_btn.setText(t("saveSettingsLabel"))
        for b in self._browse_buttons:
            b.setText(t("browseLabel"))

        self.cr_back.setText(t("creditsBackBtn"))
        self.cr_title.setText(t("creditsTitle"))
        blocks = [(t("creditsAppName"), t("creditsAppDesc").replace("1.0.0", str(self.state.get("version", "1.0.0")))),
                  (t("creditsLicenseTitle"), t("creditsLicenseText")),
                  (t("creditsCommunityTitle"), "discord.gg/wbU8vw8vJq"),
                  (t("creditsProfileTitle"), t("creditsProfileText"))]
        for (h, p), (ht, pt) in zip(self.cr_blocks, blocks):
            h.setText(ht); p.setText(pt)
        self.discord_btn.setText(t("openDiscordLabel"))

        self.su_back.setText(t("settingsBackBtn"))
        self.su_title.setText(t("setupTitle"))
        self.su_intro.setText(t("setupIntro"))
        self.su_lbl_dolphin.setText(t("labelSetupDolphin"))
        self.su_lbl_iso.setText(t("labelSetupIso"))
        self.su_continue.setText(t("setupContinueLabel"))

    # ------------------------------------------------------------ theme
    def _apply_theme(self, colors):
        pal = dict(DEFAULT_PALETTE)
        for k, v in (colors or {}).items():
            if k in pal and _is_hex_color(v):
                pal[k] = v
        self.pal = pal
        p = pal
        if hasattr(self, "banner"):
            self.banner.border = QColor(p['--panel-hover'])
        self.setStyleSheet(f"""
        QWidget {{ color:{p['--text']}; font-size:13px; }}
        #root, QStackedWidget, QStackedWidget > QWidget {{ background:{p['--bg']}; }}
        #bar {{ background:{p['--bg-2']}; border-bottom:1px solid {p['--panel-hover']}; }}
        #brand {{ font-size:17px; font-weight:700; letter-spacing:1px; }}
        #pill {{ color:{p['--text-dim']}; border:1px solid {p['--panel-hover']};
                 border-radius:10px; padding:2px 9px; font-size:11px; }}
        QPushButton#tab {{ background:transparent; border:0; border-radius:8px;
                           padding:8px 16px; color:{p['--text-dim']}; font-weight:500; }}
        QPushButton#tab:hover {{ color:{p['--text']}; }}
        QPushButton#tab:checked {{ background:{p['--panel-hover']}; color:{p['--text']};
                                    border:1px solid {p['--blue']}; }}
        QPushButton#primary {{ background:{p['--blue']}; color:#1a0800; border:0;
                               border-radius:11px; padding:12px 26px; font-weight:700; font-size:14px; }}
        QPushButton#primary:hover {{ background:{p['--blue-2']}; }}
        QPushButton#primary:disabled {{ background:{p['--panel-hover']}; color:{p['--text-dim']}; }}
        QPushButton#ghost {{ background:{p['--panel']}; border:1px solid {p['--panel-hover']};
                             border-radius:10px; padding:10px 18px; font-weight:600; }}
        QPushButton#ghost:hover {{ background:{p['--panel-hover']}; }}
        QPushButton#card {{ background:{p['--panel']}; border:1px solid {p['--panel-hover']};
                            border-radius:14px; padding:8px; text-align:left; }}
        QPushButton#card:hover {{ background:{p['--panel-hover']}; border-color:{p['--blue']}; }}
        #stat, #panel {{ background:{p['--panel']}; border:1px solid {p['--panel-hover']}; border-radius:14px; }}
        #panel QLabel, #stat QLabel {{ background:transparent; border:0; }}
        #cardTitle {{ font-size:15px; font-weight:700; background:transparent; }}
        #cardDesc {{ color:{p['--text-dim']}; background:transparent; }}
        #statLabel {{ color:{p['--text-faint']}; font-size:10.5px; letter-spacing:1px; }}
        #statVal {{ font-size:15px; font-weight:700; }}
        #statVal[dim="true"] {{ color:{p['--text-dim']}; font-weight:500; }}
        #h2 {{ font-size:22px; font-weight:700; }}
        #h4 {{ font-size:14px; font-weight:700; color:{p['--cyan']}; margin-top:8px; }}
        #dim {{ color:{p['--text-dim']}; }}
        #notice {{ color:{p['--text']}; background:{p['--panel']}; border:1px solid {p['--panel-hover']};
                   border-radius:10px; padding:10px 14px; }}
        QLineEdit, QComboBox, QPlainTextEdit {{ background:{p['--bg-2']}; border:1px solid {p['--panel-hover']};
                  border-radius:8px; padding:8px 10px; selection-background-color:{p['--blue']}; }}
        QPlainTextEdit#mono {{ font-family:Consolas,'DejaVu Sans Mono',monospace; font-size:11.5px; color:{p['--text-dim']}; }}
        QComboBox QAbstractItemView {{ background:{p['--panel']}; selection-background-color:{p['--panel-hover']}; }}
        QCheckBox {{ spacing:8px; padding:4px 0; }}
        QProgressBar {{ background:{p['--bg-2']}; border:0; border-radius:3px; }}
        QProgressBar::chunk {{ background:{p['--blue']}; border-radius:3px; }}
        QDialog {{ background:{p['--panel']}; }}
        """)

    def _poll_config(self):
        try:
            m = os.path.getmtime(self.m.CONFIG_PATH)
        except OSError:
            return
        if self._cfg_mtime is None:
            self._cfg_mtime = m
            return
        if m != self._cfg_mtime and not self._playing:
            self._cfg_mtime = m
            self.apply_state(self.api.get_state())

    # ------------------------------------------------------------ toast
    def toast(self, text, error=False):
        self.notice.setText(text)
        self.notice.setStyleSheet("color:#ff7b7b;" if error else "")
        self.notice.show()
        if not hasattr(self, "_toast_timer"):
            self._toast_timer = QTimer(self)
            self._toast_timer.setSingleShot(True)
            self._toast_timer.timeout.connect(self.notice.hide)
        self._toast_timer.start(9000)

    # ------------------------------------------------------------ settings
    def save_settings(self):
        payload = {
            "dolphin_path": self.set_dolphin.text(),
            "iso_path": self.set_iso.text(),
            "mod_directory": self.set_moddir.text(),
            "resolution": self.set_res.text(),
            "language": self.set_lang.currentData(),
            "fullscreen": self.set_full.isChecked(),
            "auto_update": self.set_auto.isChecked(),
        }
        self.api.save_settings(payload)
        self.apply_state(self.api.get_state())
        self._cfg_mtime = None
        self.toast(self.t("toastSettingsSaved"))

    def save_setup(self):
        self.api.save_settings({"dolphin_path": self.su_dolphin.text(),
                                "iso_path": self.su_iso.text()})
        self.apply_state(self.api.get_state())
        self._cfg_mtime = None
        self.show_screen("home")

    # ------------------------------------------------------------ play flow
    def _set_playing(self, busy, text=None):
        self._playing = busy
        self.play_btn.setEnabled(not busy)
        for target, c in self.cards:
            if target == "play":
                c.setEnabled(not busy)
        self.play_btn.setText(text if busy else "▶  " + self.t("playLabel"))
        if not busy:
            self.progress.hide()

    def do_play(self):
        if self._playing:
            return
        self._set_playing(True, self.t("checkingFiles"))
        self.run_bg(self.api.check_for_update, self._after_check)

    def _after_check(self, check):
        if isinstance(check, dict) and check.get("launcher_update_available") \
                and check.get("launcher_download_url"):
            self._show_launcher_popup(check["launcher_download_url"])
        if isinstance(check, dict) and check.get("update_available"):
            res = self.api.start_update(check["download_url"], check["latest_version"])
            if not (res and res.get("ok")):
                self.toast(self.t("toastFailedStart"), True)
                self._set_playing(False)
                return
            self._kind = self.t("downloadingMod" if check.get("is_first_install") else "downloadingUpdate")
            self.toast(self.t("toastDownloadingFirst" if check.get("is_first_install")
                              else "toastDownloadingUpdate") + " " + self.t("toastDontClose"))
            self.progress.setRange(0, 0)
            self.progress.show()
            self._poll = QTimer(self)
            self._poll.timeout.connect(self._poll_progress)
            self._poll.start(500)
            return
        if isinstance(check, dict) and check.get("error"):
            self.toast(f"{self.t('toastCouldntCheck')} ({check['error']})", True)
        self._launch()

    def _poll_progress(self):
        p = self.api.get_download_progress()
        st = p.get("status")
        if st == "downloading":
            done = p.get("downloaded_bytes") or 0
            total = p.get("total_bytes")
            if total:
                self.progress.setRange(0, 1000)
                self.progress.setValue(int(done * 1000 / total))
                self.play_btn.setText(f"⬇ {self._kind}… {int(done*100/total)}% "
                                      f"({done>>20}/{total>>20} MB)")
            else:
                self.progress.setRange(0, 0)
                self.play_btn.setText(f"⬇ {self._kind}… {done>>20} MB")
        elif st == "extracting":
            self.progress.setRange(0, 0)
            self.play_btn.setText(self.t("extracting"))
        elif st == "done":
            self._poll.stop()
            self.toast(f"{self.t('toastGotPrefix')}{p.get('version')}. {self.t('launching')}")
            self._launch()
        elif st == "error":
            self._poll.stop()
            self.toast(p.get("error") or self.t("toastFailedFetch"), True)
            self._set_playing(False)

    def _launch(self):
        self.progress.hide()
        self.play_btn.setText(self.t("launching"))
        self.run_bg(self.api.launch_game, self._after_launch)

    def _after_launch(self, res):
        self._set_playing(False)
        if res and res.get("ok"):
            self.toast(res.get("message") or self.t("toastLaunched"))
        else:
            self.toast((res or {}).get("error") or self.t("toastCouldNotLaunch"), True)

    # ------------------------------------------------------------ launcher self-update
    def _startup_update_check(self):
        def work():
            return self.api.check_for_update()

        def done(check):
            if isinstance(check, dict):
                self.apply_state(self.api.get_state())
                if check.get("launcher_update_available") and check.get("launcher_download_url"):
                    self._show_launcher_popup(check["launcher_download_url"])
        self.run_bg(work, done)

    def _show_launcher_popup(self, url):
        if self._launcher_popup_shown:
            return
        self._launcher_popup_shown = True
        dlg = QDialog(self)
        dlg.setWindowTitle("Mario Kart Nitro")
        dlg.setModal(True)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(28, 24, 28, 24)
        msg = QLabel(self.t("launcherUpdateMsg"))
        msg.setWordWrap(True)
        btn = QPushButton(self.t("launcherUpdateBtn"))
        btn.setObjectName("primary")
        lay.addWidget(msg)
        lay.addWidget(btn)
        self._popup_msg, self._popup_btn = msg, btn

        def go():
            btn.setEnabled(False)
            msg.setText(self.t("launcherUpdateInstalling"))
            res = self.api.start_launcher_update(url)
            if not (res and res.get("ok")):
                msg.setText((res or {}).get("error") or self.t("launcherUpdateFailed"))
        btn.clicked.connect(go)
        self._popup = dlg
        dlg.show()

    def _on_launcher_update_error(self, message):
        if getattr(self, "_popup", None) is not None:
            self._popup_msg.setText(self.t("launcherUpdateFailed") + ("\n" + message if message else ""))
            self._popup_btn.setEnabled(True)
        else:
            self.toast(self.t("launcherUpdateFailed"), True)

    # ------------------------------------------------------------ Mii editor
    def open_mii_editor(self):
        if self._mii_proc is not None and self._mii_proc.poll() is None:
            return  # already open
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--mii"]
        else:
            cmd = [sys.executable, os.path.abspath(self.m.__file__), "--mii"]
        env = dict(os.environ)
        # Fresh extraction for the child is fine, but never inherit the
        # parent's bootloader state (would make it reuse a _MEI folder
        # this process may delete on exit).
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        try:
            self._mii_proc = subprocess.Popen(cmd, env=env)
        except OSError as e:
            self.toast(str(e), True)


def run(api, main_module):
    """Entry point used by main.py. Returns the process exit code."""
    app = QApplication.instance() or QApplication(sys.argv)
    font = QFont()
    font.setFamilies(["Segoe UI", "Inter", "Noto Sans", "DejaVu Sans"])
    font.setPointSize(10)
    app.setFont(font)
    win = LauncherWindow(api, main_module)
    # Centre on the primary screen.
    geo = app.primaryScreen().availableGeometry()
    win.move(geo.center().x() - win.width() // 2, geo.center().y() - win.height() // 2)
    win.show()
    try:
        main_module._close_splash_screen()
    except Exception:
        pass
    return app.exec()
