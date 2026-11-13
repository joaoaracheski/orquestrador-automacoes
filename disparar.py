"""Dispara jobs no master pela API. Ex.: python disparar.py baixar_relatorio --clientes 20"""

import argparse
import json
import os
import urllib.request

# Clientes fictícios usados como parâmetro dos jobs de demonstração
CLIENTES = [
    "Alfa Logística", "Beta Contábil", "Cia. Gama", "Delta Imóveis", "Épsilon Saúde",
    "Fator Seguros", "Grupo Horizonte", "Ipê Advocacia", "Jota Transportes", "Kappa Foods",
    "Lumen Educação", "Mira Varejo", "Nobre Engenharia", "Ômega Têxtil", "Prisma Tech",
    "Quartzo Mineração", "Rota Sul", "Sigma Finanças", "Tetra Agro", "Unir Cooperativa",
]  # fmt: skip


def main():
    """Lê as opções e manda o disparo para o master. Mostra os ids criados."""

    args = ler_argumentos()
    parametros = CLIENTES[: args.clientes] if args.clientes else []
    requisicao = urllib.request.Request(
        args.master.rstrip("/") + "/jobs",
        data=json.dumps(
            {"automacao": args.automacao, "parametros": parametros}
        ).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Token": os.environ.get("ORQ_TOKEN", ""),
        },
        method="POST",
    )

    with urllib.request.urlopen(requisicao, timeout=10) as resposta:
        ids = json.loads(resposta.read())["ids"]

    print(f"{len(ids)} job(s) de {args.automacao} na fila.")


def ler_argumentos():
    """Opções do disparo. Chamada por main."""

    parser = argparse.ArgumentParser(description="Dispara jobs no orquestrador.")
    parser.add_argument("automacao", help="nome cadastrado no config.json")
    parser.add_argument(
        "--clientes", type=int, default=0, help="um job por cliente fictício (até 20)"
    )
    parser.add_argument("--master", default="http://127.0.0.1:8000")

    return parser.parse_args()


if __name__ == "__main__":
    main()
