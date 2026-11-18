#!/usr/bin/env python3
"""ScanDesk - scan, copy and print documents on Linux using SANE and CUPS."""
import datetime
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageSequence
from PySide6.QtCore import QPointF, QProcess, QRectF, QSettings, QSize, Qt, QUrl
from PySide6.QtGui import (
    QAction, QDesktopServices, QIcon, QImage, QKeySequence, QPageLayout, QPageSize, QPainter,
    QPixmap, QShortcut,
)
from PySide6.QtPrintSupport import QAbstractPrintDialog, QPrintDialog, QPrinter, QPrinterInfo
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow,
    QMessageBox, QProgressBar, QPushButton, QScrollArea, QSlider, QSpinBox,
    QSplitter, QVBoxLayout, QWidget,
)

APP = "ScanDesk"
__version__ = "0.1.0"
PATH_ROLE = Qt.UserRole
DPI_ROLE = Qt.UserRole + 1
PDF_IMPORT_DPI = 300
EMPTY_HINT = ("No pages yet.\n\nPlace a page on the glass and press Enter to scan,\n"
              "or drop images / PDFs here.")
IMPORT_FILTER = "Images and PDFs (*.pdf *.png *.jpg *.jpeg *.tif *.tiff *.bmp *.gif *.webp);;All files (*)"
PRINT_FILTER = ("Printable files (*.pdf *.ps *.txt *.png *.jpg *.jpeg *.tif *.tiff *.bmp *.gif *.webp)"
                ";;All files (*)")

# Paper sizes in mm (width, height); None = full scanner bed
AREAS = {
    "Full bed": None,
    "Letter (8.5 × 11 in)": (215.9, 279.4),
    "A4 (210 × 297 mm)": (210.0, 297.0),
    "A5 (148 × 210 mm)": (148.0, 210.0),
    "Photo 4 × 6 in": (101.6, 152.4),
}


# --------------------------------------------------------------------------- PDF
def write_pdf(pages, out_path, quality=85):
    """Write [(image_path, dpi), ...] to a PDF, one image per page at true physical size."""
    bodies = {}
    kids = []
    for i, (path, dpi) in enumerate(pages):
        with Image.open(path) as im:
            im.load()
            if im.mode not in ("RGB", "L"):
                im = im.convert("L" if im.mode in ("1", "LA", "I", "I;16") else "RGB")
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=quality, optimize=True)
            data = buf.getvalue()
            w, h = im.size
            cs = "/DeviceRGB" if im.mode == "RGB" else "/DeviceGray"
        dpi = dpi or 300
        pw, ph = w * 72.0 / dpi, h * 72.0 / dpi
        img_o, cont_o, page_o = 3 + 3 * i, 4 + 3 * i, 5 + 3 * i
        bodies[img_o] = (
            f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} /ColorSpace {cs} "
            f"/BitsPerComponent 8 /Filter /DCTDecode /Length {len(data)} >>\nstream\n"
        ).encode() + data + b"\nendstream"
        content = f"q {pw:.3f} 0 0 {ph:.3f} 0 0 cm /Im0 Do Q".encode()
        bodies[cont_o] = f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream"
        bodies[page_o] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {pw:.3f} {ph:.3f}] "
            f"/Resources << /XObject << /Im0 {img_o} 0 R >> >> /Contents {cont_o} 0 R >>"
        ).encode()
        kids.append(f"{page_o} 0 R")
    bodies[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    bodies[2] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>".encode()
    info_o = max(bodies) + 1
    stamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    bodies[info_o] = f"<< /Producer ({APP}) /CreationDate (D:{stamp}) >>".encode()

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for k in sorted(bodies):
        offsets[k] = len(out)
        out += f"{k} 0 obj\n".encode() + bodies[k] + b"\nendobj\n"
    xref = len(out)
    total = len(bodies) + 1
    out += f"xref\n0 {total}\n0000000000 65535 f \n".encode()
    for k in range(1, total):
        out += f"{offsets[k]:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {total} /Root 1 0 R /Info {info_o} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()

    tmp = str(out_path) + ".part"
    with open(tmp, "wb") as f:
        f.write(out)
    os.replace(tmp, out_path)


def parse_options(text):
    """Parse `scanimage -A` output into {option: (values|range, default)}."""
    opts = {}
    for line in text.splitlines():
        m = re.match(r"^\s+(--?[\w-]+)(?:\[=\(.*?\)\])? (.+?) \[(.*?)\]\s*$", line)
        if not m:
            continue
        name, spec, default = m.groups()
        spec = re.sub(r"\s*\(in steps of .*?\)$", "", spec.strip())
        spec = re.sub(r"(dpi|mm|%|pel)$", "", spec)
        spec = re.sub(r"^auto\|", "", spec)
        default = re.sub(r"(dpi|mm|%|pel)$", "", default.strip())
        rng = re.match(r"^([\d.]+)\.\.([\d.]+)", spec)
        if rng:
            opts[name] = (("range", float(rng.group(1)), float(rng.group(2))), default)
        else:
            opts[name] = ([v.strip() for v in spec.split("|") if v.strip() and v.strip() != "auto"], default)
    return opts


# --------------------------------------------------------------------------- UI
class Preview(QScrollArea):
    def __init__(self):
        super().__init__()
        self.label = QLabel(EMPTY_HINT)
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setStyleSheet("color: palette(mid); font-size: 15px;")
        self.setWidget(self.label)
        self.setWidgetResizable(True)
        self.setAlignment(Qt.AlignCenter)
        self._pix = None

    def show_image(self, path):
        self._pix = QPixmap(path) if path else None
        if not self._pix:
            self.label.setPixmap(QPixmap())
            self.label.setText(EMPTY_HINT)
        self._rescale()

    def _rescale(self):
        if self._pix and not self._pix.isNull():
            vp = self.viewport().size() - QSize(16, 16)
            self.label.setPixmap(self._pix.scaled(vp, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._rescale()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP)
        self.setWindowIcon(QIcon.fromTheme("scanner"))
        self.resize(1200, 800)
        self.settings = QSettings(APP, APP)
        self.workdir = Path(tempfile.mkdtemp(prefix="scandesk-"))
        self.counter = 0
        self.proc = None
        self.options = {}

        # ---- left: scanner controls
        self.device = QComboBox()
        self.device.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.device.setMinimumContentsLength(18)
        self.device.currentIndexChanged.connect(self.load_device_options)
        refresh = QPushButton(QIcon.fromTheme("view-refresh"), "")
        refresh.setToolTip("Search for scanners")
        refresh.clicked.connect(self.find_devices)
        devrow = QHBoxLayout()
        devrow.addWidget(self.device, 1)
        devrow.addWidget(refresh)

        self.mode = QComboBox()
        self.resolution = QComboBox()
        self.area = QComboBox()
        self.area.addItems(AREAS.keys())
        for combo in (self.mode, self.resolution, self.area):
            combo.currentTextChanged.connect(self.save_prefs)

        form = QFormLayout()
        form.addRow("Scanner", devrow)
        form.addRow("Color", self.mode)
        form.addRow("Resolution", self.resolution)
        form.addRow("Area", self.area)
        settings_box = QGroupBox("Scan settings")
        settings_box.setLayout(form)

        self.scan_btn = QPushButton(QIcon.fromTheme("scanner"), "  Scan page  (Enter)")
        self.scan_btn.setMinimumHeight(56)
        f = self.scan_btn.font()
        f.setPointSize(f.pointSize() + 3)
        f.setBold(True)
        self.scan_btn.setFont(f)
        self.scan_btn.clicked.connect(self.scan_or_cancel)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.hide()
        self.status = QLabel("")
        self.status.setWordWrap(True)

        # ---- copy (scan straight to printer)
        self.copy_printer = QComboBox()
        self.copy_printer.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.copy_printer.setMinimumContentsLength(14)
        self.refresh_printers()
        self.copy_printer.currentTextChanged.connect(
            lambda t: t and self.settings.setValue("copy_printer", t))
        self.copy_count = QSpinBox()
        self.copy_count.setRange(1, 99)
        self.copy_count.setValue(int(self.settings.value("copy_count", 1)))
        self.copy_count.valueChanged.connect(lambda v: self.settings.setValue("copy_count", v))
        self.copy_bw = QCheckBox("Black && white")
        self.copy_bw.setChecked(self.settings.value("copy_bw", "false") == "true")
        self.copy_bw.toggled.connect(lambda on: self.settings.setValue("copy_bw", "true" if on else "false"))
        crow = QHBoxLayout()
        crow.addWidget(self.copy_count)
        crow.addWidget(self.copy_bw, 1)
        self.copy_btn = QPushButton(QIcon.fromTheme("edit-copy"), "  Copy  (Ctrl+Enter)")
        self.copy_btn.setMinimumHeight(40)
        self.copy_btn.setToolTip("Scan the page on the glass and print it right away")
        self.copy_btn.clicked.connect(lambda: self.start_scan(copy=True))
        cform = QFormLayout()
        cform.addRow("Printer", self.copy_printer)
        cform.addRow("Copies", crow)
        cform.addRow(self.copy_btn)
        copy_box = QGroupBox("Copy")
        copy_box.setLayout(cform)
        self._copying = False

        left = QVBoxLayout()
        left.addWidget(settings_box)
        left.addSpacing(8)
        left.addWidget(self.scan_btn)
        left.addWidget(self.progress)
        left.addWidget(self.status)
        left.addSpacing(8)
        left.addWidget(copy_box)
        left.addStretch(1)
        add_file = QPushButton(QIcon.fromTheme("document-open"), "Add pages from files…")
        add_file.setToolTip("Import images or PDFs as pages (Ctrl+O). You can also drag files onto the window.")
        add_file.clicked.connect(self.add_from_files)
        left.addWidget(add_file)
        print_file = QPushButton(QIcon.fromTheme("document-print"), "Print files…")
        print_file.setToolTip("Print PDFs, images or text files directly (Ctrl+Shift+P)")
        print_file.clicked.connect(self.print_files)
        left.addWidget(print_file)
        leftw = QWidget()
        leftw.setLayout(left)
        leftw.setMinimumWidth(280)
        leftw.setMaximumWidth(380)

        # ---- center: preview
        self.preview = Preview()

        # ---- right: pages + save
        self.pages = QListWidget()
        self.pages.setViewMode(QListWidget.ListMode)
        self.pages.setIconSize(QSize(110, 140))
        self.pages.setSpacing(4)
        self.pages.setDragDropMode(QAbstractItemView.InternalMove)
        self.pages.setDefaultDropAction(Qt.MoveAction)
        self.pages.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.pages.currentItemChanged.connect(self.on_select)
        self.pages.model().rowsMoved.connect(self.renumber)

        def tool(icon, tip, slot):
            b = QPushButton(QIcon.fromTheme(icon), "")
            b.setToolTip(tip)
            b.clicked.connect(slot)
            return b

        tools = QHBoxLayout()
        tools.addWidget(tool("go-up", "Move page up", lambda: self.move(-1)))
        tools.addWidget(tool("go-down", "Move page down", lambda: self.move(1)))
        tools.addWidget(tool("object-rotate-left", "Rotate left", lambda: self.rotate(90)))
        tools.addWidget(tool("object-rotate-right", "Rotate right", lambda: self.rotate(-90)))
        tools.addWidget(tool("edit-delete", "Delete selected pages (Del)", self.delete_selected))
        tools.addWidget(tool("edit-clear-all", "Remove all pages", self.clear_all))

        self.quality = QSlider(Qt.Horizontal)
        self.quality.setRange(40, 95)
        self.quality.setValue(int(self.settings.value("quality", 80)))
        self.quality_lbl = QLabel()
        self.quality.valueChanged.connect(self._quality_label)
        self._quality_label(self.quality.value())
        qrow = QHBoxLayout()
        qrow.addWidget(QLabel("Quality"))
        qrow.addWidget(self.quality, 1)
        qrow.addWidget(self.quality_lbl)

        self.open_after = QCheckBox("Open PDF after saving")
        self.open_after.setChecked(self.settings.value("open_after", "true") == "true")
        self.clear_after = QCheckBox("Clear pages after saving")
        self.clear_after.setChecked(self.settings.value("clear_after", "true") == "true")

        self.save_btn = QPushButton(QIcon.fromTheme("document-save"), "  Save as PDF…  (Ctrl+S)")
        self.save_btn.setMinimumHeight(44)
        self.save_btn.clicked.connect(self.save_pdf)
        self.print_btn = QPushButton(QIcon.fromTheme("document-print"), "  Print pages…  (Ctrl+P)")
        self.print_btn.setMinimumHeight(36)
        self.print_btn.clicked.connect(self.print_pages)
        self.sides = QComboBox()
        for label, mode in (("One-sided", QPrinter.DuplexNone),
                            ("Two-sided (flip on long edge)", QPrinter.DuplexLongSide),
                            ("Two-sided (flip on short edge)", QPrinter.DuplexShortSide)):
            self.sides.addItem(label, int(mode.value))
        self.sides.setToolTip("Used by Print pages and Print files. Long edge = normal book-style "
                              "two-sided; short edge = for landscape pages.")
        idx = self.sides.findData(int(self.settings.value("duplex", 0)))
        self.sides.setCurrentIndex(max(idx, 0))
        self.sides.currentIndexChanged.connect(
            lambda _: self.settings.setValue("duplex", self.sides.currentData()))
        srow = QHBoxLayout()
        srow.addWidget(QLabel("Sides"))
        srow.addWidget(self.sides, 1)

        self.pages_title = QLabel()
        right = QVBoxLayout()
        right.addWidget(self.pages_title)
        right.addWidget(self.pages, 1)
        right.addLayout(tools)
        right.addLayout(qrow)
        right.addWidget(self.open_after)
        right.addWidget(self.clear_after)
        right.addWidget(self.save_btn)
        right.addSpacing(6)
        right.addLayout(srow)
        right.addWidget(self.print_btn)
        rightw = QWidget()
        rightw.setLayout(right)
        rightw.setMinimumWidth(260)
        rightw.setMaximumWidth(360)

        split = QSplitter()
        split.addWidget(leftw)
        split.addWidget(self.preview)
        split.addWidget(rightw)
        split.setStretchFactor(1, 1)
        split.setChildrenCollapsible(False)
        self.setCentralWidget(split)

        self.build_menus(refresh)
        QShortcut(QKeySequence.Delete, self.pages, self.delete_selected)
        self.setAcceptDrops(True)

        self.renumber()
        self.update_buttons()

        if not shutil.which("scanimage"):
            self.scan_btn.setEnabled(False)
            self.set_status("<b>scanimage not found.</b><br>Install SANE:<br>"
                            "<code>sudo pacman -S sane</code>", error=True)
        else:
            self.find_devices()

    # ---------------------------------------------------------------- menus
    def build_menus(self, refresh_btn):
        def act(menu, icon, text, slot, *keys):
            a = QAction(QIcon.fromTheme(icon), text, self)
            if keys:
                a.setShortcuts([QKeySequence(k) for k in keys])
            a.triggered.connect(slot)
            menu.addAction(a)
            return a

        m = self.menuBar().addMenu("&File")
        act(m, "document-open", "&Add pages from files…", self.add_from_files, QKeySequence.Open)
        self.save_act = act(m, "document-save", "&Save as PDF…", self.save_pdf, QKeySequence.Save)
        m.addSeparator()
        self.print_act = act(m, "document-print", "&Print pages…", self.print_pages, QKeySequence.Print)
        act(m, "document-print", "Print &files…", self.print_files, "Ctrl+Shift+P")
        m.addSeparator()
        act(m, "application-exit", "&Quit", self.close, QKeySequence.Quit)

        m = self.menuBar().addMenu("&Scan")
        # Enter (main keyboard) and Enter (keypad) both start a scan; Esc cancels.
        self.scan_act = act(m, "scanner", "&Scan page", lambda: self.start_scan(), "Return", "Enter")
        self.copy_act = act(m, "edit-copy", "C&opy (scan && print)", lambda: self.start_scan(copy=True),
                            "Ctrl+Return", "Ctrl+Enter")
        self.cancel_act = act(m, "process-stop", "&Cancel scan", self.cancel_scan, "Esc")
        m.addSeparator()
        act(m, "view-refresh", "Search for scanners", self.find_devices, "Ctrl+R")
        act(m, "printer", "Refresh printer list", self.refresh_printers)
        refresh_btn.setToolTip("Search for scanners (Ctrl+R)")

        m = self.menuBar().addMenu("&Pages")
        act(m, "go-up", "Move up", lambda: self.move(-1), "Ctrl+Up")
        act(m, "go-down", "Move down", lambda: self.move(1), "Ctrl+Down")
        act(m, "object-rotate-left", "Rotate left", lambda: self.rotate(90), "Ctrl+L")
        act(m, "object-rotate-right", "Rotate right", lambda: self.rotate(-90), "Ctrl+Shift+R")
        m.addSeparator()
        # Delete is bound on the page list itself (see __init__) so it can't eat keys elsewhere.
        act(m, "edit-delete", "Delete selected", self.delete_selected)
        act(m, "edit-clear-all", "Remove all", self.clear_all)

        m = self.menuBar().addMenu("&Help")
        act(m, "help-about", f"&About {APP}", self.about)

    def about(self):
        QMessageBox.about(self, f"About {APP}",
                          f"<h3>{APP} {__version__}</h3>"
                          "<p>Scan, copy and print documents on Linux.</p>"
                          "<p>Uses SANE (<code>scanimage</code>) for scanning and CUPS for printing.</p>"
                          "<p>Released under the MIT License.</p>")

    # ---------------------------------------------------------------- helpers
    def set_status(self, text, error=False):
        self.status.setText(text)
        self.status.setStyleSheet("color: #c0392b;" if error else "")

    def _quality_label(self, v):
        self.quality_lbl.setText(f"{v}")
        self.settings.setValue("quality", v)

    def save_prefs(self, *_):
        if self.mode.count():
            self.settings.setValue("mode", self.mode.currentText())
        if self.resolution.count():
            self.settings.setValue("resolution", self.resolution.currentText())
        self.settings.setValue("area", self.area.currentText())

    def run_async(self, args, on_done):
        p = QProcess(self)
        p.finished.connect(lambda code, _s: on_done(code, bytes(p.readAllStandardOutput()).decode(errors="replace"),
                                                   bytes(p.readAllStandardError()).decode(errors="replace")))
        p.start("scanimage", args)
        return p

    # ---------------------------------------------------------------- devices
    def find_devices(self):
        self.set_status("Searching for scanners…")
        self.scan_btn.setEnabled(False)
        self.device.clear()
        self.run_async(["-f", "%d\x1f%v %m%n"], self._devices_found)

    def _devices_found(self, code, out, err):
        devs = [l.split("\x1f", 1) for l in out.splitlines() if "\x1f" in l]
        # Webcams (v4l) show up as "scanners" too; list real scanners first.
        devs.sort(key=lambda d: d[0].startswith("v4l:"))
        self.device.blockSignals(True)
        for dev, name in devs:
            self.device.addItem(name.strip() or dev, dev)
            self.device.setItemData(self.device.count() - 1, dev, Qt.ToolTipRole)
        last = self.settings.value("device")
        idx = self.device.findData(last)
        self.device.setCurrentIndex(idx if idx >= 0 else 0)
        self.device.blockSignals(False)
        if not devs:
            self.set_status("No scanner found. Check that it's on and connected, then press ⟳.<br>"
                            "<small>Tip: run <code>scanimage -L</code> in a terminal to diagnose.</small>",
                            error=True)
            return
        self.load_device_options()

    def load_device_options(self):
        dev = self.device.currentData()
        if not dev:
            return
        self.settings.setValue("device", dev)
        self.set_status("Reading scanner capabilities…")
        self.run_async(["-d", dev, "-A"], self._options_loaded)

    def _options_loaded(self, code, out, err):
        self.options = parse_options(out)
        modes, mdef = self.options.get("--mode", (["Color", "Gray", "Lineart"], "Color"))
        res, rdef = self.options.get("--resolution", (["150", "300", "600"], "300"))
        if isinstance(res, tuple):  # range instead of list
            lo, hi = res[1], res[2]
            res = [str(r) for r in (75, 100, 150, 200, 300, 600, 1200) if lo <= r <= hi]
        for combo, values, pref, fallback in (
            (self.mode, modes, self.settings.value("mode"), mdef),
            (self.resolution, res, self.settings.value("resolution"), "300" if "300" in res else rdef),
        ):
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(values)
            combo.setCurrentText(pref if pref in values else fallback)
            combo.blockSignals(False)
        area = self.settings.value("area")
        if area in AREAS:
            self.area.setCurrentText(area)
        if code != 0 and not self.options:
            self.set_status(f"Couldn't read scanner options:<br><small>{err.strip()}</small>", error=True)
        else:
            self.set_status("Ready.")
        self.update_buttons()

    # ---------------------------------------------------------------- scanning
    def scan_or_cancel(self):
        """Scan button: starts a scan, or cancels the one in progress."""
        if self.proc:
            self.cancel_scan()
        else:
            self.start_scan()

    def cancel_scan(self):
        if self.proc:
            self.proc.kill()
            self.set_status("Scan cancelled.")

    def start_scan(self, copy=False):
        if self.proc:  # Enter while scanning does nothing (Esc cancels)
            return
        dev = self.device.currentData()
        if not dev or not self.scan_btn.isEnabled():
            return
        if copy and not self.copy_printer.currentText():
            QMessageBox.warning(self, APP, "No printer found for copying.\n"
                                "Check that it's on, then use Scan → Refresh printer list.")
            return
        self._copying = copy
        self.counter += 1
        out = self.workdir / f"page-{self.counter:04d}.png"
        dpi = int(re.sub(r"\D", "", self.resolution.currentText()) or 300)
        args = ["-d", dev, "--format=png", "-p", "-o", str(out)]
        if self.mode.currentText():
            args += ["--mode", self.mode.currentText()]
        args += ["--resolution", str(dpi)]
        size = AREAS.get(self.area.currentText())
        if size:
            for opt, val in (("-x", size[0]), ("-y", size[1])):
                rng = self.options.get(opt, (None,))[0]
                if isinstance(rng, tuple):
                    val = min(val, rng[2])
                args += [opt, f"{val:.1f}"]

        self.proc = QProcess(self)
        self.proc.readyReadStandardError.connect(self._scan_progress)
        self.proc.finished.connect(lambda c, s, o=out, d=dpi: self._scan_done(c, s, o, d))
        self._stderr = ""
        self.proc.start("scanimage", args)
        self.scan_btn.setText("  Cancel scan  (Esc)")
        self.scan_btn.setIcon(QIcon.fromTheme("process-stop"))
        self.progress.setValue(0)
        self.progress.show()
        self.set_status(("Copying" if copy else "Scanning") + "…  <small>(Esc to cancel)</small>")
        self.device.setEnabled(False)
        self.update_buttons()

    def _scan_progress(self):
        chunk = bytes(self.proc.readAllStandardError()).decode(errors="replace")
        self._stderr += chunk
        found = re.findall(r"Progress:\s*([\d.]+)%", chunk)
        if found:
            self.progress.setValue(int(float(found[-1])))

    def _scan_done(self, code, status, out, dpi):
        self.proc = None
        self.progress.hide()
        self.device.setEnabled(True)
        self.scan_btn.setText("  Scan page  (Enter)")
        self.scan_btn.setIcon(QIcon.fromTheme("scanner"))
        copying, self._copying = self._copying, False
        if copying and status == QProcess.NormalExit and code == 0 and out.exists() and out.stat().st_size:
            self.print_copy(out, dpi)
            out.unlink(missing_ok=True)
        elif status == QProcess.NormalExit and code == 0 and out.exists() and out.stat().st_size:
            self.add_page(out, dpi)
            self.set_status(f"Scanned page {self.pages.count()}. Place the next page and press Enter.")
        else:
            out.unlink(missing_ok=True)
            if status != QProcess.CrashExit:
                msg = "\n".join(l for l in self._stderr.splitlines() if "Progress" not in l).strip()
                self.set_status(f"Scan failed.<br><small>{msg or 'Unknown error'}</small>", error=True)
        self.update_buttons()

    # ---------------------------------------------------------------- pages
    def make_thumb(self, path):
        pix = QPixmap(str(path))
        return QIcon(pix.scaled(self.pages.iconSize(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def add_page(self, path, dpi):
        item = QListWidgetItem(self.make_thumb(path), "")
        item.setData(PATH_ROLE, str(path))
        item.setData(DPI_ROLE, dpi)
        item.setFlags(item.flags() & ~Qt.ItemIsDropEnabled)
        self.pages.addItem(item)
        self.pages.setCurrentItem(item)
        self.pages.scrollToItem(item)
        self.renumber()

    # ---------------------------------------------------------------- import
    def _new_page_path(self):
        self.counter += 1
        return self.workdir / f"page-{self.counter:04d}.png"

    def add_from_files(self):
        start = self.settings.value("open_dir") or str(Path.home())
        files, _ = QFileDialog.getOpenFileNames(self, "Add pages from files", start, IMPORT_FILTER)
        if files:
            self.settings.setValue("open_dir", str(Path(files[0]).parent))
            self.import_files(files)

    def import_files(self, files):
        added, errors = 0, []
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            for f in files:
                self.set_status(f"Importing {Path(f).name}…")
                QApplication.processEvents()
                try:
                    added += self._import_pdf(f) if f.lower().endswith(".pdf") else self._import_image(f)
                except Exception as e:
                    errors.append(f"{Path(f).name}: {e}")
        finally:
            QApplication.restoreOverrideCursor()
        self.set_status(f"Added {added} page(s) from {len(files) - len(errors)} file(s)." if added else "")
        if errors:
            QMessageBox.warning(self, APP, "Some files couldn't be imported:\n\n" + "\n".join(errors))
        self.update_buttons()

    def _import_image(self, f):
        n = 0
        with Image.open(f) as im:
            for frame in ImageSequence.Iterator(im):  # multi-page TIFF/GIF → several pages
                frame = frame.copy()
                if frame.mode in ("RGBA", "LA", "PA") or (frame.mode == "P" and "transparency" in frame.info):
                    rgba = frame.convert("RGBA")
                    frame = Image.new("RGB", rgba.size, "white")
                    frame.paste(rgba, mask=rgba.getchannel("A"))
                elif frame.mode not in ("RGB", "L"):
                    frame = frame.convert("RGB")
                dpi = im.info.get("dpi", (0, 0))[0]
                dpi = int(round(float(dpi))) if dpi else 0
                if dpi < 100:
                    # Photos/screenshots usually carry no real DPI (or 72). Size them so
                    # the long side is 11in, which prints/saves at a sensible page size.
                    dpi = max(72, round(max(frame.size) / 11))
                dest = self._new_page_path()
                frame.save(dest)
                self.add_page(dest, dpi)
                n += 1
        return n

    def _import_pdf(self, f):
        if not shutil.which("pdftoppm"):
            raise RuntimeError("pdftoppm not found. Install poppler: sudo pacman -S poppler")
        tmp = Path(tempfile.mkdtemp(dir=self.workdir))
        try:
            r = subprocess.run(["pdftoppm", "-r", str(PDF_IMPORT_DPI), "-png", f, str(tmp / "p")],
                               capture_output=True, text=True)
            if r.returncode:
                raise RuntimeError(r.stderr.strip() or "pdftoppm failed")
            outs = sorted(tmp.glob("p-*.png"), key=lambda p: int(p.stem.rsplit("-", 1)[1]))
            for o in outs:
                dest = self._new_page_path()
                o.rename(dest)
                self.add_page(dest, PDF_IMPORT_DPI)
            return len(outs)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @staticmethod
    def _local_files(event):
        md = event.mimeData()
        return [u.toLocalFile() for u in md.urls() if u.isLocalFile()] if md.hasUrls() else []

    def dragEnterEvent(self, e):
        if self._local_files(e):
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if self._local_files(e):
            e.acceptProposedAction()

    def dropEvent(self, e):
        files = [f for f in self._local_files(e) if Path(f).is_file()]
        if files:
            e.acceptProposedAction()
            self.import_files(files)

    def renumber(self, *_):
        for i in range(self.pages.count()):
            it = self.pages.item(i)
            it.setText(f"Page {i + 1}")
        n = self.pages.count()
        self.pages_title.setText(f"<b>Pages</b> ({n})  <small>— drag to reorder</small>")
        self.update_buttons()

    def on_select(self, cur, _prev):
        self.preview.show_image(cur.data(PATH_ROLE) if cur else None)

    def move(self, delta):
        row = self.pages.currentRow()
        new = row + delta
        if row < 0 or not (0 <= new < self.pages.count()):
            return
        item = self.pages.takeItem(row)
        self.pages.insertItem(new, item)
        self.pages.setCurrentRow(new)
        self.renumber()

    def rotate(self, angle):
        for item in self.pages.selectedItems() or [self.pages.currentItem()]:
            if not item:
                continue
            path = item.data(PATH_ROLE)
            with Image.open(path) as im:
                rotated = im.rotate(angle, expand=True)
            rotated.save(path)
            item.setIcon(self.make_thumb(path))
        cur = self.pages.currentItem()
        self.preview.show_image(cur.data(PATH_ROLE) if cur else None)

    def delete_selected(self):
        items = self.pages.selectedItems()
        if not items:
            return
        row = self.pages.row(items[0])
        for it in items:
            Path(it.data(PATH_ROLE)).unlink(missing_ok=True)
            self.pages.takeItem(self.pages.row(it))
        if self.pages.count():
            self.pages.setCurrentRow(min(row, self.pages.count() - 1))
        else:
            self.preview.show_image(None)
        self.renumber()

    def clear_all(self, confirm=True):
        if not self.pages.count():
            return
        if confirm and QMessageBox.question(
                self, APP, f"Remove all {self.pages.count()} pages?") != QMessageBox.Yes:
            return
        for i in range(self.pages.count()):
            Path(self.pages.item(i).data(PATH_ROLE)).unlink(missing_ok=True)
        self.pages.clear()
        self.preview.show_image(None)
        self.renumber()

    def update_buttons(self):
        has = self.pages.count() > 0
        scanning = self.proc is not None
        can_scan = bool(self.device.currentData()) and shutil.which("scanimage") is not None
        self.save_btn.setEnabled(has)
        self.print_btn.setEnabled(has)
        self.scan_btn.setEnabled(scanning or can_scan)
        can_copy = can_scan and not scanning and self.copy_printer.count() > 0
        self.copy_btn.setEnabled(can_copy)
        if hasattr(self, "scan_act"):
            self.copy_act.setEnabled(can_copy)
            self.save_act.setEnabled(has)
            self.print_act.setEnabled(has)
            self.scan_act.setEnabled(can_scan and not scanning)
            self.cancel_act.setEnabled(scanning)

    # ---------------------------------------------------------------- save
    def save_pdf(self):
        if not self.pages.count():
            return
        folder = self.settings.value("save_dir") or str(Path.home() / "Documents")
        name = datetime.datetime.now().strftime("scan-%Y-%m-%d-%H%M%S.pdf")
        path, _ = QFileDialog.getSaveFileName(self, "Save PDF", str(Path(folder) / name), "PDF (*.pdf)")
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        self.settings.setValue("save_dir", str(Path(path).parent))
        self.settings.setValue("open_after", "true" if self.open_after.isChecked() else "false")
        self.settings.setValue("clear_after", "true" if self.clear_after.isChecked() else "false")
        pages = [(self.pages.item(i).data(PATH_ROLE), self.pages.item(i).data(DPI_ROLE))
                 for i in range(self.pages.count())]
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            write_pdf(pages, path, self.quality.value())
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, APP, f"Couldn't save PDF:\n{e}")
            return
        QApplication.restoreOverrideCursor()
        size_kb = os.path.getsize(path) / 1024
        self.set_status(f"Saved {len(pages)} page(s) to<br><small>{path}</small><br>({size_kb:,.0f} KB)")
        if self.open_after.isChecked():
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        if self.clear_after.isChecked():
            self.clear_all(confirm=False)

    # ---------------------------------------------------------------- print
    def _new_printer(self, mode=QPrinter.HighResolution):
        printer = QPrinter(mode)
        last = self.settings.value("printer")
        if last and last in QPrinterInfo.availablePrinterNames():
            printer.setPrinterName(last)
        return printer

    def _remember_printer(self, printer):
        if printer.outputFormat() == QPrinter.NativeFormat and printer.printerName():
            self.settings.setValue("printer", printer.printerName())
        # If the user changed duplex inside the dialog, reflect it in the Sides box.
        mode = QPrinter.DuplexLongSide if printer.duplex() == QPrinter.DuplexAuto else printer.duplex()
        idx = self.sides.findData(int(mode.value))
        if idx >= 0:
            self.sides.setCurrentIndex(idx)

    def _apply_sides(self, printer):
        printer.setDuplex(QPrinter.DuplexMode(self.sides.currentData()))

    def refresh_printers(self):
        names = QPrinterInfo.availablePrinterNames()
        pref = (self.settings.value("copy_printer") or self.settings.value("printer")
                or QPrinterInfo.defaultPrinterName())
        self.copy_printer.blockSignals(True)
        self.copy_printer.clear()
        self.copy_printer.addItems(names)
        if pref in names:
            self.copy_printer.setCurrentText(pref)
        self.copy_printer.blockSignals(False)
        if hasattr(self, "copy_act"):
            self.update_buttons()

    def print_copy(self, path, dpi):
        """Print a freshly scanned page with the Copy settings, no dialog."""
        printer = QPrinter(QPrinter.HighResolution)
        printer.setPrinterName(self.copy_printer.currentText())
        printer.setCopyCount(self.copy_count.value())
        printer.setColorMode(QPrinter.GrayScale if self.copy_bw.isChecked() else QPrinter.Color)
        printer.setDocName(f"{APP} copy")
        if self._paint_pages(printer, [(path, dpi)]):
            n = self.copy_count.value()
            self.set_status(f"Sent {n} cop{'y' if n == 1 else 'ies'} to <b>{self.copy_printer.currentText()}</b>.")

    def print_pages(self):
        """Print the page list through Qt (scales each page to fit, auto-rotates, centers)."""
        n = self.pages.count()
        if not n:
            return
        printer = self._new_printer()
        self._apply_sides(printer)
        dlg = QPrintDialog(printer, self)
        dlg.setWindowTitle(f"Print {n} page(s)")
        dlg.setMinMax(1, n)
        dlg.setFromTo(1, n)
        dlg.setOption(QAbstractPrintDialog.PrintPageRange, True)
        dlg.setOption(QAbstractPrintDialog.PrintCurrentPage, True)
        dlg.setOption(QAbstractPrintDialog.PrintSelection, len(self.pages.selectedItems()) > 1)
        if not dlg.exec():
            return
        self._remember_printer(printer)

        rng = printer.printRange()
        if rng == QPrinter.Selection:
            rows = sorted(self.pages.row(it) for it in self.pages.selectedItems())
        elif rng == QPrinter.CurrentPage:
            rows = [max(self.pages.currentRow(), 0)]
        elif rng == QPrinter.PageRange and printer.fromPage():
            rows = list(range(printer.fromPage() - 1, min(printer.toPage(), n)))
        else:
            rows = list(range(n))
        if not rows:
            return
        pages = [(self.pages.item(r).data(PATH_ROLE), self.pages.item(r).data(DPI_ROLE)) for r in rows]
        if self._paint_pages(printer, pages):
            where = printer.outputFileName() or printer.printerName() or "printer"
            self.set_status(f"Sent {len(rows)} page(s) to <b>{where}</b>.")

    def _paint_pages(self, printer, pages):
        """Render [(image_path, dpi), ...] to printer. Returns True on success."""
        painter = QPainter()
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            if not painter.begin(printer):
                raise RuntimeError("Couldn't start printing (is the printer available?)")
            painter.setRenderHint(QPainter.SmoothPixmapTransform)
            res = printer.resolution()
            area_w, area_h = printer.width(), printer.height()  # printable area, device px
            for k, (path, dpi) in enumerate(pages):
                if k and not printer.newPage():
                    raise RuntimeError("The printer rejected a new page.")
                img = QImage(str(path))
                if img.isNull():
                    continue
                dpi = dpi or 300
                w, h = img.width() * res / dpi, img.height() * res / dpi  # true physical size
                turn = (w > h) != (area_w > area_h) and abs(w - h) > 1  # landscape scan → portrait paper
                bw, bh = (h, w) if turn else (w, h)
                scale = min(1.0, area_w / bw, area_h / bh)  # shrink to fit, never enlarge
                w, h = w * scale, h * scale
                painter.save()
                painter.translate(QPointF(area_w / 2, area_h / 2))
                if turn:
                    painter.rotate(90)
                painter.drawImage(QRectF(-w / 2, -h / 2, w, h), img)
                painter.restore()
            painter.end()
        except Exception as e:
            if painter.isActive():
                painter.end()
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, APP, f"Printing failed:\n{e}")
            return False
        QApplication.restoreOverrideCursor()
        return True

    def print_files(self):
        """Send files (PDF, images, text, PostScript) straight to a CUPS printer with `lp`."""
        if not shutil.which("lp"):
            QMessageBox.warning(self, APP, "The `lp` command wasn't found.\nInstall CUPS: sudo pacman -S cups")
            return
        start = self.settings.value("print_dir") or self.settings.value("open_dir") or str(Path.home())
        files, _ = QFileDialog.getOpenFileNames(self, "Choose files to print", start, PRINT_FILTER)
        if not files:
            return
        self.settings.setValue("print_dir", str(Path(files[0]).parent))

        printer = self._new_printer(QPrinter.ScreenResolution)
        self._apply_sides(printer)
        dlg = QPrintDialog(printer, self)
        names = ", ".join(Path(f).name for f in files[:3]) + (f" +{len(files) - 3} more" if len(files) > 3 else "")
        dlg.setWindowTitle(f"Print {names}")
        dlg.setOption(QAbstractPrintDialog.PrintToFile, False)
        dlg.setOption(QAbstractPrintDialog.PrintPageRange, True)
        dlg.setOption(QAbstractPrintDialog.PrintCurrentPage, False)
        dlg.setOption(QAbstractPrintDialog.PrintSelection, False)
        dlg.setMinMax(1, 9999)
        if not dlg.exec():
            return
        if printer.outputFormat() != QPrinter.NativeFormat or not printer.printerName():
            QMessageBox.warning(self, APP, "Please choose a real printer to print files.")
            return
        self._remember_printer(printer)

        args = ["-d", printer.printerName(), "-n", str(max(1, printer.copyCount()))]
        opts = []
        if printer.copyCount() > 1:
            opts.append(f"collate={'true' if printer.collateCopies() else 'false'}")
        if printer.printRange() == QPrinter.PageRange and printer.fromPage():
            opts.append(f"page-ranges={printer.fromPage()}-{printer.toPage()}")
        sides = {QPrinter.DuplexLongSide: "two-sided-long-edge", QPrinter.DuplexAuto: "two-sided-long-edge",
                 QPrinter.DuplexShortSide: "two-sided-short-edge"}.get(printer.duplex(), "one-sided")
        opts.append(f"sides={sides}")
        if printer.colorMode() == QPrinter.GrayScale:
            opts.append("print-color-mode=monochrome")
        layout = printer.pageLayout()
        if layout.orientation() == QPageLayout.Landscape:
            opts.append("landscape")
        if layout.pageSize().id() != QPageSize.Custom:
            opts.append(f"media={layout.pageSize().key()}")
        opts.append("fit-to-page")
        for o in opts:
            args += ["-o", o]
        args += ["-t", names, "--", *files]

        r = subprocess.run(["lp", *args], capture_output=True, text=True)
        if r.returncode:
            QMessageBox.critical(self, APP, f"Printing failed:\n{(r.stderr or r.stdout).strip()}")
            return
        job = re.search(r"request id is (\S+)", r.stdout)
        self.set_status(f"Sent {len(files)} file(s) to <b>{printer.printerName()}</b>"
                        + (f"<br><small>Job {job.group(1)}</small>" if job else "."))

    def closeEvent(self, e):
        if self.pages.count() and QMessageBox.question(
                self, APP, f"You have {self.pages.count()} unsaved page(s). Quit anyway?") != QMessageBox.Yes:
            e.ignore()
            return
        if self.proc:
            self.proc.kill()
            self.proc.waitForFinished(2000)
        shutil.rmtree(self.workdir, ignore_errors=True)
        e.accept()


def main():
    if "--version" in sys.argv[1:]:
        print(f"{APP} {__version__}")
        return
    app = QApplication(sys.argv)
    app.setApplicationName(APP)
    app.setApplicationVersion(__version__)
    app.setDesktopFileName("scandesk")
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
