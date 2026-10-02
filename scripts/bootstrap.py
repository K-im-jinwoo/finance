"""Local runtime bootstrap. The vendor snapshot never writes to the original."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor/stock-assistant/src")]
sys.dont_write_bytecode = True

