"""Use the canonical finance engine; keep the vendor snapshot as evidence."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "engine/src")]
sys.dont_write_bytecode = True

