"""PyInstaller-Einstieg fuer Zeitspur.exe (absolute Imports, da als __main__ ausgefuehrt)."""
import multiprocessing

from zeitspur.service_main import run

if __name__ == "__main__":
    multiprocessing.freeze_support()
    run()
