# -*- mode: python ; coding: utf-8 -*-
# The `cmdr-mcp` server as a one-folder build, which an MCP client starts as dist/CommanderMcp/cmdr-mcp.exe.
# Build: uv sync --group build && uv run --group build pyinstaller --noconfirm --clean CommanderMcp.spec
# A console program (stdio is the protocol), and without the desktop app: `cmdr app` is not a tool, so the
# window, its web engine and the Buy screen's modules stay out.


a = Analysis(
    ['src/commander/mcp/__main__.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['commander.app', 'webview', 'clr', 'clr_loader', 'pythonnet', 'PyQt6', 'qtpy', 'tkinter'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='cmdr-mcp',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='CommanderMcp',
)
