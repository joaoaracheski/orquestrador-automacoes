"""Escalador: sobe workers extras quando a fila enche e derruba quando sobra worker parado.

Dois modos: "processo" (worker como processo local, bom para testar) e "docker"
(worker como container, via API do Docker). O master não vê diferença entre eles.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import banco

PASTA_PROJETO = Path(__file__).parent

# Estado do escalador durante a vida do master (só ele mexe aqui, pelo loop)
ESTADO = {"extras": {}, "contador": 0, "ultima_subida": 0.0, "ocioso_desde": None}


def avaliar(caminho, escala, agora=None):
    """Decide se sobe um extra, derruba um ocioso ou não faz nada. Devolve o que fez.

    Chamada a cada volta do loop do master. Sobe no máximo um por vez, com espera entre
    subidas, para não criar vários de uma vez enquanto o primeiro ainda está ligando.
    """

    if escala["modo"] == "desligado":
        return None

    agora = agora or time.time()
    limpar_encerrados(escala)
    fila = banco.contar_fila(caminho)
    extras = ESTADO["extras"]

    if fila == 0:
        ESTADO["ocioso_desde"] = ESTADO["ocioso_desde"] or agora
    else:
        ESTADO["ocioso_desde"] = None

    pode_subir = agora - ESTADO["ultima_subida"] >= escala["espera_entre_subidas_s"]

    if (
        fila > escala["subir_acima_de"]
        and len(extras) < escala["max_extras"]
        and pode_subir
    ):
        nome = subir_extra(escala)
        ESTADO["ultima_subida"] = agora
        return f"fila com {fila} jobs: subi o worker {nome}"

    parado_ha = agora - (ESTADO["ocioso_desde"] or agora)

    if extras and parado_ha >= escala["derrubar_ocioso_apos_s"]:
        ociosos = banco.listar_ociosos(caminho, list(extras))
        if ociosos:
            derrubar_extra(ociosos[0], escala)
            banco.marcar_desligado(caminho, ociosos[0])
            ESTADO["ocioso_desde"] = agora
            return f"fila vazia: derrubei o worker {ociosos[0]}"

    return None


def subir_extra(escala):
    """Liga um worker extra no modo configurado e devolve o nome dele. Chamada por avaliar."""

    ESTADO["contador"] += 1
    nome = f"extra-{ESTADO['contador']}"
    etiquetas = ",".join(escala["etiquetas_extra"])

    if escala["modo"] == "docker":
        ESTADO["extras"][nome] = subir_container(nome, etiquetas, escala)
    else:
        ESTADO["extras"][nome] = subir_processo(nome, etiquetas, escala)

    return nome


def subir_processo(nome, etiquetas, escala):
    """Inicia worker.py como processo local. Chamada por subir_extra."""

    comando = [
        sys.executable,
        str(PASTA_PROJETO / "worker.py"),
        "--master",
        escala["url_master"],
        "--nome",
        nome,
        "--etiquetas",
        etiquetas,
        "--origem",
        "processo",
    ]

    # O token vai pelo ambiente (herdado do master), nunca pela linha de comando
    return subprocess.Popen(comando, cwd=PASTA_PROJETO, env=os.environ.copy())


def subir_container(nome, etiquetas, escala):
    """Cria um container do worker na mesma rede do master. Chamada por subir_extra."""

    import docker

    cliente = docker.from_env()

    return cliente.containers.run(
        escala["imagem"],
        command=[
            "python",
            "worker.py",
            "--master",
            escala["url_master"],
            "--nome",
            nome,
            "--etiquetas",
            etiquetas,
            "--origem",
            "docker",
        ],
        name=f"orquestrador-{nome}",
        network=escala["rede"],
        environment={"ORQ_TOKEN": os.environ["ORQ_TOKEN"]},
        labels={"orquestrador": "extra"},
        detach=True,
        remove=True,
    )


def derrubar_extra(nome, escala):
    """Desliga um worker extra (só é chamada para worker sem job). Chamada por avaliar."""

    alvo = ESTADO["extras"].pop(nome)

    if escala["modo"] == "docker":
        alvo.stop(timeout=5)
    else:
        alvo.terminate()


def limpar_encerrados(escala):
    """Esquece extras que já terminaram sozinhos (processo saiu, container sumiu).

    Chamada por avaliar, para a contagem de extras não ficar presa.
    """

    for nome, alvo in list(ESTADO["extras"].items()):
        if escala["modo"] == "docker":
            try:
                alvo.reload()
                encerrado = alvo.status in ("exited", "dead")
            except Exception:
                encerrado = True
        else:
            encerrado = alvo.poll() is not None

        if encerrado:
            ESTADO["extras"].pop(nome)


def derrubar_todos(escala):
    """Desliga todos os extras. Chamada ao encerrar o master (atexit)."""

    for nome in list(ESTADO["extras"]):
        try:
            derrubar_extra(nome, escala)
        except Exception:
            pass
