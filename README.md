# ScanDesk

A simple desktop app for Linux for scanning, copying and printing documents.
Scan several pages, reorder and rotate them, and save them as one PDF. You can also print pages, print files, or use your scanner and printer together as a copier.

ScanDesk is a single Python/Qt file. It runs SANE's `scanimage` to scan and uses CUPS to print, so it works with any scanner and printer your system already supports.

## Features

- **Multi-page scanning:** scan page after page with **Enter**, then save everything as one PDF. Pages keep their real physical size, so a Letter scan becomes a Letter-sized PDF page.
- **Scan settings:** pick the scanner, color mode, resolution and scan area (Letter, A4, A5, 4×6 photo or the full bed). Your choices are remembered.
- **Page management:** drag to reorder, move, rotate, delete or clear pages, with a large preview of the selected page.
- **Import from files:** add images, multi-page TIFFs and PDFs as pages, from a file dialog or by dragging them onto the window.
- **Print pages:** print some or all pages. Each page is scaled to fit the paper, turned to match it and centered.
- **Print files:** send PDFs, images, text or PostScript files straight to a CUPS printer.
- **Two-sided printing:** a *Sides* setting (one-sided, long edge or short edge) applies to all printing.
- **Copy:** scan the page on the glass and print it right away. You can set the number of copies, choose black & white and pick the printer.
- **PDF quality:** a quality slider trades file size against image quality.

## Requirements

- Linux with **SANE** (`scanimage`) for scanning
- **CUPS** (`lp`) for printing files; printing pages and copying use Qt's CUPS support
- **poppler** (`pdftoppm`) for importing PDFs
- Python 3.9+ with **PySide6** and **Pillow**

Arch Linux:

```sh
sudo pacman -S sane cups poppler pyside6 python-pillow
```

Debian / Ubuntu:

```sh
sudo apt install sane-utils cups-client poppler-utils python3-pyside6.qtwidgets python3-pyside6.qtprintsupport python3-pil
```

Fedora:

```sh
sudo dnf install sane-backends cups-client poppler-utils python3-pyside6 python3-pillow
```

Some scanners need a vendor backend (for example `scangearmp2` for many Canon PIXMA models).
Run `scanimage -L` to check that SANE can see your scanner.

## Running

Straight from the source folder:

```sh
python3 scandesk.py
```

Or install it so you get a `scandesk` command:

```sh
pipx install .                 # or: pip install --user .
```

To add it to your application menu:

```sh
install -Dm644 scandesk.desktop ~/.local/share/applications/scandesk.desktop
```

## Keyboard shortcuts

| Key | Action |
| --- | --- |
| **Enter** | Scan a page |
| **Ctrl+Enter** | Copy (scan and print right away) |
| **Esc** | Cancel the scan in progress |
| Ctrl+O | Add pages from files (images / PDFs) |
| Ctrl+S | Save pages as PDF |
| Ctrl+P | Print pages |
| Ctrl+Shift+P | Print files |
| Ctrl+↑ / Ctrl+↓ | Move the selected page up / down |
| Ctrl+L / Ctrl+Shift+R | Rotate left / right |
| Delete | Delete the selected pages (when the page list has focus) |
| Ctrl+R | Search for scanners again |
| Ctrl+Q | Quit |

## Troubleshooting

- **"No scanner found":** run `scanimage -L` in a terminal. If nothing shows up there, the scanner needs a SANE backend, or your user needs scanner permissions (on some systems that means joining the `scanner` group).
- **Webcams show up as scanners:** SANE lists v4l devices as scanners. ScanDesk lists real scanners first.
- **Two-sided option missing in the print dialog:** use the *Sides* setting in the main window. In Qt's own dialog the option is hidden under **Options >> → Options → Duplex Printing**.
- **PDF import fails:** install poppler (`pdftoppm`). Password-protected PDFs aren't supported.

## Development

```sh
pip install --user -e '.[test]'    # or just: python3 -m pytest
python3 -m pytest
```

The tests cover the PDF writer and the `scanimage -A` option parser. They don't need a scanner, printer or display.

Settings are stored with `QSettings` in `~/.config/ScanDesk/ScanDesk.conf`.

## License

[MIT](LICENSE) © 2026 shadowshard
