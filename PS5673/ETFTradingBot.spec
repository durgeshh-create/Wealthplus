# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=[],
    datas=[('frontend/templates', 'frontend/templates'), ('frontend/static', 'frontend/static')],
    hiddenimports=['backend.auth.manager', 'backend.core.config', 'backend.core.constants', 'backend.data.historical', 'backend.data.realtime', 'backend.indicators.calculator', 'backend.orders.manager', 'backend.portfolio.tracker', 'backend.strategy.executor', 'backend.strategy.signal_generator', 'backend.utils.logger', 'frontend.app', 'frontend.routes', 'scripts.setup_auth', 'kiteconnect', 'pyotp', 'flask_cors'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['torch', 'scipy', 'numba', 'sklearn', 'matplotlib', 'cv2', 'PIL', 'tkinter', 'wx', 'PyQt5', 'PyQt6', 'llvmlite', 'IPython', 'jupyter', 'psycopg2', 'sqlalchemy'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ETFTradingBot',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
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
    upx=True,
    upx_exclude=[],
    name='ETFTradingBot',
)
