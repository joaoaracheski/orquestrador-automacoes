"""Worker do orquestrador: se registra no master, pede jobs, executa e devolve o resultado.

Roda igual em container, VM ou máquina física. Uso:
ORQ_TOKEN=<segredo> python worker.py --master http://localhost:8000 --nome pc-1 --etiquetas tela
"""

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# As automações ficam ao lado do worker; o master só manda o nome
PASTA_AUTOMACOES = Path(__file__).parent / "automacoes"

# Nome de automação vem do master pela rede: só minúsculas, números e _ (vira nome de arquivo)
NOME_AUTOMACAO = re.compile(r"^[a-z0-9_]{1,60}$")

# Espera entre pedidos quando a fila está vazia, para não martelar o master
ESPERA_SEM_JOB_S = 1.5

# Tempo máximo de um job; passou disso, o worker mata o script e devolve erro
TEMPO_MAXIMO_S = 600

# Quanto da saída do script vai para o master (o fim é o que interessa no erro)
TAMANHO_SAIDA = 2000


def main():
    """Registra o worker, liga o sinal de vida e fica pedindo e executando jobs."""

    args = ler_argumentos()
    worker = {
        "master": args.master.rstrip("/"),
        "token": os.environ.get("ORQ_TOKEN", ""),
        "nome": args.nome,
        "etiquetas": [e for e in args.etiquetas.split(",") if e],
        "origem": args.origem,
        "id": None,
        "sinal_s": 2,
    }

    if not worker["token"]:
        raise SystemExit("Defina a variável ORQ_TOKEN antes de iniciar o worker.")

    registrar(worker)
    threading.Thread(target=enviar_sinais, args=(worker,), daemon=True).start()

    while True:
        job = pedir_job(worker)

        if not job:
            time.sleep(ESPERA_SEM_JOB_S)
            continue

        codigo, saida = executar(job)
        avisar(worker, f"{job['automacao']} #{job['id']} terminou com código {codigo}")
        chamar(
            worker,
            f"/jobs/{job['id']}/concluir",
            {"worker_id": worker["id"], "codigo": codigo, "saida": saida},
        )


def ler_argumentos():
    """Opções do worker. Chamada por main."""

    parser = argparse.ArgumentParser(description="Worker do orquestrador.")
    parser.add_argument("--master", required=True, help="endereço do master")
    parser.add_argument("--nome", required=True, help="nome que aparece no painel")
    parser.add_argument(
        "--etiquetas", default="", help="o que este worker sabe fazer: tela,navegador"
    )
    parser.add_argument(
        "--origem", default="maquina", choices=["processo", "docker", "maquina"]
    )

    return parser.parse_args()


def registrar(worker):
    """Se cadastra no master, tentando de novo até ele responder (ele pode estar subindo).

    Chamada por main e por enviar_sinais (quando o master não reconhece mais o worker).
    """

    while True:
        try:
            resposta = chamar(
                worker,
                "/workers",
                {
                    "nome": worker["nome"],
                    "etiquetas": worker["etiquetas"],
                    "origem": worker["origem"],
                },
            )
            worker["id"], worker["sinal_s"] = resposta["id"], resposta["sinal_s"]
            avisar(worker, f"registrado com id {worker['id']}")
            return
        except (urllib.error.URLError, OSError) as erro:
            avisar(worker, f"master fora do ar ({erro}); tentando de novo em 3 s")
            time.sleep(3)


def enviar_sinais(worker):
    """Manda sinal de vida a cada `sinal_s` segundos; isso renova o prazo do job atual.

    Chamada em thread por main. Se o master não conhece o worker (404), registra de novo.
    """

    while True:
        time.sleep(worker["sinal_s"])

        try:
            chamar(worker, f"/workers/{worker['id']}/sinal", {})
        except urllib.error.HTTPError as erro:
            if erro.code == 404:
                registrar(worker)
        except (urllib.error.URLError, OSError):
            pass  # master fora do ar: o próximo sinal tenta de novo


def pedir_job(worker):
    """Pede o próximo job ao master; devolve o job ou None. Chamada por main."""

    try:
        return chamar(worker, f"/workers/{worker['id']}/proximo", {})["job"]
    except (urllib.error.URLError, OSError):
        return None


def executar(job):
    """Roda a automação como processo separado e devolve (código de saída, fim da saída).

    Chamada por main. Processo separado: se o script travar ou cair, o worker continua.
    """

    nome = job["automacao"]
    script = PASTA_AUTOMACOES / f"{nome}.py"

    if not NOME_AUTOMACAO.match(nome) or not script.is_file():
        return 2, f"automação {nome!r} não existe neste worker"

    # Lista de argumentos, sem shell: o parâmetro nunca é interpretado como comando
    comando = [sys.executable, str(script)]
    if job.get("parametro"):
        comando.append(job["parametro"])

    try:
        processo = subprocess.run(
            comando,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TEMPO_MAXIMO_S,
        )
    except subprocess.TimeoutExpired:
        return 124, f"passou de {TEMPO_MAXIMO_S} s e foi interrompida"

    saida = (processo.stdout + processo.stderr)[-TAMANHO_SAIDA:]

    return processo.returncode, saida


def chamar(worker, caminho, dados):
    """POST em JSON para o master com o token; devolve a resposta já convertida.

    Chamada por todas as funções que falam com o master. Levanta HTTPError/URLError.
    """

    requisicao = urllib.request.Request(
        worker["master"] + caminho,
        data=json.dumps(dados).encode(),
        headers={"Content-Type": "application/json", "X-Token": worker["token"]},
        method="POST",
    )

    with urllib.request.urlopen(requisicao, timeout=10) as resposta:
        return json.loads(resposta.read())


def avisar(worker, mensagem):
    """Mensagem curta no terminal do worker (o histórico oficial fica no master)."""

    print(f"[{time.strftime('%H:%M:%S')}] {worker['nome']}: {mensagem}", flush=True)


if __name__ == "__main__":
    main()
