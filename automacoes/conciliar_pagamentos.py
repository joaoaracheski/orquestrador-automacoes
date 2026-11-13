"""Automação de exemplo, agendada: "concilia" os pagamentos do dia (dados fictícios).

Exclusiva (limite 1): nunca roda duas vezes ao mesmo tempo, como acontece com
rotinas que gravam no mesmo lugar.
"""

import random
import time


def main():
    """Espera de 2 a 4 s e mostra quantos pagamentos "conciliou"."""

    time.sleep(random.uniform(2, 4))
    print(f"{random.randint(20, 80)} pagamentos conciliados.")


if __name__ == "__main__":
    main()
