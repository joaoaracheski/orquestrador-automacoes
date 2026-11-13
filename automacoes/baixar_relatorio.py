"""Automação de exemplo: "baixa" o relatório de um cliente fictício num portal.

Simula a demora de um robô de navegador e, de vez em quando, uma falha do portal.
"""

import random
import sys
import time

# Chance de o "portal" falhar, para o painel mostrar erros reais de vez em quando
CHANCE_FALHA = 0.1


def main():
    """Espera de 3 a 7 s e sai com 0 (baixou) ou 1 (portal fora do ar)."""

    cliente = sys.argv[1] if len(sys.argv) > 1 else "sem cliente"
    print(f"Entrando no portal para o cliente {cliente}...")
    time.sleep(random.uniform(3, 7))

    if random.random() < CHANCE_FALHA:
        print("O portal não respondeu.")
        sys.exit(1)

    print(f"Relatório de {cliente} baixado.")


if __name__ == "__main__":
    main()
