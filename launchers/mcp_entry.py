"""PyInstaller-Einstieg fuer ZeitspurMCP.exe (absolute Imports, da als __main__ ausgefuehrt)."""
import multiprocessing

from zeitspur.mcp_server import run

if __name__ == "__main__":
    multiprocessing.freeze_support()
    run()
