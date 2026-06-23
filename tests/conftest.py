"""pytest 配置：把 src/ 加入 sys.path，使 `from sec_adapter import ...` 可用。"""
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
