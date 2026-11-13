"""Automação de exemplo que precisa de tela: lançar dados num sistema desktop antigo.

Só vai para worker com a etiqueta "tela" (uma máquina Windows de verdade, fora do Docker).
"""

import sys
import time


def main():
    """Simula o preenchimento da tela do sistema por 4 s."""

    registro = sys.argv[1] if len(sys.argv) > 1 else "lote do dia"
    print(f"Abrindo o sistema legado e lançando {registro}...")
    time.sleep(4)
    print("Lançamento concluído.")


if __name__ == "__main__":
    main()
