"""Qt theme for the unified recorder, using the original camera palette."""

COLORS = {
    "bg": "#0e1217",            # window background
    "surface": "#161b22",       # cards
    "surface_alt": "#1f2630",   # buttons, status pill
    "surface_hover": "#2a3340",
    "surface_press": "#344050",
    "border": "#2b3440",
    "input": "#0b0f14",         # entries, message log
    "text": "#e6eaf0",
    "muted": "#8b95a3",
    "disabled": "#566170",
    "accent": "#4c9aff",
    "accent_hover": "#6cb0ff",
    "accent_press": "#3a7fd9",
    "record": "#e5484d",
    "record_hover": "#ef5b60",
    "record_press": "#c7393e",
    "record_disabled": "#2f2326",
    "success": "#3fb950",
    "warning": "#e3a008",
    "video": "#06080b",         # letterbox behind camera previews
}

FONT_FAMILY = "Segoe UI"
FONTS = {
    "title": (FONT_FAMILY, 20, "bold"),
    "heading": (FONT_FAMILY, 10, "bold"),
    "small": (FONT_FAMILY, 9),
    "small_bold": (FONT_FAMILY, 9, "bold"),
}

QT_STYLE = f"""
QMainWindow, QWidget {{ background:{COLORS['bg']}; color:{COLORS['text']}; font-family:'Segoe UI'; font-size:13px; }}
QLabel#title {{ font-size:25px; font-weight:650; }}
QLabel#muted {{ color:{COLORS['muted']}; }}
QLabel#badge {{ background:{COLORS['surface_alt']}; border-radius:6px; padding:7px 12px; font-weight:600; }}
QLabel#metric {{ font-family:'Consolas'; font-size:23px; }}
QLabel#preview {{ background:{COLORS['video']}; border:1px solid {COLORS['border']}; }}
QGroupBox {{ background:{COLORS['surface']}; border:1px solid {COLORS['border']}; border-radius:7px; margin-top:17px; padding-top:12px; font-weight:600; }}
QGroupBox::title {{ subcontrol-origin:margin; left:12px; padding:0 5px; color:{COLORS['muted']}; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{ background:{COLORS['input']}; border:1px solid {COLORS['border']}; border-radius:5px; padding:6px; }}
QComboBox QAbstractItemView {{ background:{COLORS['surface']}; selection-background-color:{COLORS['accent']}; }}
QPushButton {{ background:{COLORS['surface_alt']}; border:1px solid {COLORS['border']}; border-radius:5px; padding:8px 11px; font-weight:600; }}
QPushButton:hover {{ background:{COLORS['surface_hover']}; }}
QPushButton:disabled {{ color:{COLORS['disabled']}; background:{COLORS['surface']}; }}
QPushButton#primary {{ background:{COLORS['accent']}; color:{COLORS['bg']}; }}
QPushButton#record {{ background:{COLORS['record']}; color:white; }}
QPushButton#record:disabled {{ background:{COLORS['record_disabled']}; color:{COLORS['disabled']}; }}
QCheckBox {{ spacing:7px; }}
QTabWidget::pane {{ border:1px solid {COLORS['border']}; border-radius:6px; }}
QTabBar::tab {{ background:{COLORS['surface']}; padding:10px 20px; color:{COLORS['muted']}; }}
QTabBar::tab:selected {{ background:{COLORS['surface_alt']}; color:{COLORS['text']}; }}
QPlainTextEdit {{ background:{COLORS['input']}; border:1px solid {COLORS['border']}; font-family:'Consolas'; font-size:11px; }}
QScrollArea {{ border:none; }}
QStatusBar {{ color:{COLORS['muted']}; }}
"""

# Applied only by UnifiedWindow; the ROI and standalone audio styles stay intact.
RECORDER_STYLE = QT_STYLE + """
QLabel { background: transparent; }
QGroupBox { margin-top: 12px; padding-top: 10px; border-color: #303b49; }
QGroupBox::title { color: #b9c5d4; }
QPushButton { padding: 7px 10px; min-height: 18px; }
QPushButton:focus, QToolButton:focus, QLineEdit:focus, QComboBox:focus,
QSpinBox:focus, QDoubleSpinBox:focus { border: 1px solid #70b3ff; }
QCheckBox:focus { outline: 1px solid #70b3ff; }
QPushButton#record { padding: 9px 18px; }
QPushButton#stop:enabled { background: #e6eaf0; color: #111923; }
QPushButton#disclosure { text-align: left; background: #1b2531; border: none; }
QLabel#sectionSummary { color: #acb9c9; padding: 2px 4px; }
QLabel#deviceStatus { color: #b9c5d4; padding: 5px; background: #161f2a; border-radius: 4px; }
QLabel#notice { padding: 7px 10px; background: #1b2531; border-radius: 5px; }
QLabel#notice[tone="warning"], QLabel#badge[tone="warning"] { color: #ffcf75; background: #30291c; }
QLabel#notice[tone="error"], QLabel#badge[tone="error"] { color: #ffb2b7; background: #362127; }
QLabel#badge[tone="success"] { color: #88e3ac; background: #173127; }
QLabel#badge[tone="recording"] { color: #ffccd0; background: #57272e; }
QLabel#triggerStatus { color: #a7b8cf; padding: 4px; }
QLabel#metric { font-size: 21px; }
QSplitter::handle { background: #243344; }
QSplitter::handle:hover { background: #4c9aff; }
QScrollBar:vertical { background: #111923; width: 9px; margin: 0; }
QScrollBar::handle:vertical { background: #3e4d61; min-height: 28px; border-radius: 4px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
QTabBar::tab:selected { border-bottom: 2px solid #70b3ff; }
"""
