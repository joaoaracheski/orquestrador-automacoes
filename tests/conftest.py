"""Deixa os testes importarem os módulos da raiz do projeto (banco, escalador...)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
