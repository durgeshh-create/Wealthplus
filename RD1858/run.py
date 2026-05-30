import sys
import os
from pathlib import Path

def main():
    python_exe = sys.executable.replace("pythonw.exe", "python.exe")
    dashboard  = str(Path(__file__).parent / "dashboard.py")
    os.execv(python_exe, [python_exe, dashboard])

if __name__ == "__main__":
    main()
