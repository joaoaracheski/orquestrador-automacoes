"""Master do orquestrador: API (workers e painel) e o loop que agenda, recupera e escala.

Uso: ORQ_TOKEN=<segredo> python master.py
"""

import atexit
import hmac
import json
import logging
import os
import re
import threading
import time
from datetime import date
from functools import partial
from pathlib import Path

import uvicorn
from fastapi import Body, Depends, FastAPI, Header, HTTPException

import banco
import escalador

# Todos os caminhos relativos à pasta do projeto (o master pode ser iniciado de qualquer lugar)
PASTA_PROJETO = Path(__file__).parent
CONFIG = json.loads((PASTA_PROJETO / "config.json").read_text(encoding="utf-8"))

# Banco fora da pasta do código: no Docker vira um volume e sobrevive a um rebuild
BANCO = Path(os.environ.get("ORQ_BANCO", PASTA_PROJETO / "dados" / "orquestrador.db"))

# Segredo que os workers mandam no cabeçalho X-Token; sem ele o master nem sobe
TOKEN = os.environ.get("ORQ_TOKEN", "")

# Nomes e etiquetas vêm de fora (workers): só letras, números, _ e -, até 40 caracteres
NOME_VALIDO = re.compile(r"^[A-Za-z0-9_-]{1,40}$")

# Nome de automação vira nome de arquivo no worker: só minúsculas, números e _
NOME_AUTOMACAO = re.compile(r"^[a-z0-9_]{1,60}$")

# Horário de agendamento diário, HH:MM em 24 h
HORARIO_VALIDO = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# Cadastro de exemplo carregado só na primeira vez (banco vazio)
EXEMPLOS = PASTA_PROJETO / "exemplos.json"

# Origens que um worker pode declarar
ORIGENS = {"processo", "docker", "maquina"}

# Máximo de jobs por disparo manual, para um erro de digitação não lotar a fila
MAX_POR_DISPARO = 200

# Último erro de cada etapa do loop: só registra de novo se a mensagem mudar
ULTIMO_ERRO = {}

app = FastAPI(title="Orquestrador de automações")
log = logging.getLogger("master")


def main():
    """Prepara banco e log, inicia o loop em segundo plano e sobe a API.

    Efeito colateral: cria dados/ e logs/, e pode subir workers extras.
    """

    if not TOKEN:
        raise SystemExit("Defina a variável ORQ_TOKEN antes de iniciar o master.")

    configurar_log()
    BANCO.parent.mkdir(parents=True, exist_ok=True)
    banco.criar_tabelas(BANCO)
    exemplos = json.loads(EXEMPLOS.read_text(encoding="utf-8"))
    if banco.semear_exemplos(BANCO, exemplos):
        log.info("Banco vazio: cadastrei as automações de exemplo.")

    escala = montar_escala()
    atexit.register(escalador.derrubar_todos, escala)
    threading.Thread(target=rodar_loop, args=(escala,), daemon=True).start()

    log.info("Master no ar (escala: %s).", escala["modo"])
    uvicorn.run(app, host=CONFIG["host"], port=CONFIG["porta"], log_level="warning")


def configurar_log():
    """Log no terminal e em logs/AAAA-MM-DD.log. Chamada por main."""

    pasta = PASTA_PROJETO / "logs"
    pasta.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(pasta / f"{date.today()}.log", encoding="utf-8"),
        ],
    )


def montar_escala():
    """Junta a seção "escala" do config com o que vier do ambiente.

    ORQ_MODO_ESCALA troca o modo sem editar o arquivo (o docker-compose usa "docker").
    Chamada por main.
    """

    escala = dict(CONFIG["escala"])
    escala["modo"] = os.environ.get("ORQ_MODO_ESCALA", escala["modo"])
    chave_url = (
        "url_master_docker" if escala["modo"] == "docker" else "url_master_local"
    )
    escala["url_master"] = escala[chave_url]

    return escala


def rodar_loop(escala):
    """Loop do master: cuida dos agendamentos, dos workers, da recuperação e da escala.

    Chamada em thread por main. Cada etapa tem o próprio try: uma falha não para as outras.
    """

    etapas = [
        ("vencidos", partial(banco.desativar_vencidos, BANCO)),
        ("calcular", partial(banco.calcular_novos, BANCO)),
        ("agendar", partial(banco.enfileirar_agendados, BANCO)),
        (
            "workers",
            partial(banco.marcar_workers_mortos, BANCO, CONFIG["worker_morto_s"]),
        ),
        (
            "recuperar",
            partial(banco.recuperar_vencidos, BANCO, CONFIG["max_tentativas"]),
        ),
        ("escalar", partial(escalador.avaliar, BANCO, escala)),
    ]

    while True:
        for nome, etapa in etapas:
            try:
                registrar_resultado(nome, etapa())
                ULTIMO_ERRO.pop(nome, None)
            except Exception as erro:
                registrar_erro(nome, erro)

        time.sleep(CONFIG["intervalo_loop_s"])


def registrar_resultado(etapa, resultado):
    """Escreve no log só o que mudou algo (nada de log a cada segundo). Chamada pelo loop."""

    if etapa == "vencidos" and resultado:
        log.info("Agendamentos fora da validade desativados: %s", resultado)
    elif etapa == "workers" and resultado:
        log.warning("Sem sinal, marcados como mortos: %s", ", ".join(resultado))
    elif etapa == "recuperar" and any(resultado):
        log.warning("Jobs devolvidos à fila: %s | desistidos: %s", *resultado)
    elif etapa == "agendar" and resultado:
        log.info("Na hora de rodar: %s", ", ".join(resultado))
    elif etapa == "escalar" and resultado:
        log.info("Escala: %s", resultado)


def registrar_erro(etapa, erro):
    """Registra o erro de uma etapa só quando ele muda, para não lotar o log. Chamada pelo loop."""

    mensagem = f"{type(erro).__name__}: {erro}"

    if ULTIMO_ERRO.get(etapa) != mensagem:
        ULTIMO_ERRO[etapa] = mensagem
        log.error("Etapa %s falhou: %s", etapa, mensagem)


def exigir_token(x_token: str = Header(default="")):
    """Recusa chamadas sem o token certo. Usada como dependência nas rotas dos workers.

    compare_digest: compara em tempo constante (não dá pista do token pela demora).
    """

    if not hmac.compare_digest(x_token.encode(), TOKEN.encode()):
        raise HTTPException(status_code=401, detail="token inválido")


@app.post("/workers", dependencies=[Depends(exigir_token)])
def rota_registrar(dados: dict = Body(...)):
    """Cadastra um worker que acabou de subir e diz de quanto em quanto tempo mandar sinal."""

    nome = str(dados.get("nome", ""))
    etiquetas = [str(e) for e in dados.get("etiquetas", [])]
    origem = str(dados.get("origem", ""))

    if not NOME_VALIDO.match(nome) or origem not in ORIGENS:
        raise HTTPException(status_code=400, detail="nome ou origem inválidos")
    if not all(NOME_VALIDO.match(e) for e in etiquetas):
        raise HTTPException(status_code=400, detail="etiqueta inválida")

    worker_id = banco.registrar_worker(BANCO, nome, etiquetas, origem)
    log.info("Worker %s entrou (%s, etiquetas: %s).", nome, origem, etiquetas or "-")

    return {"id": worker_id, "sinal_s": CONFIG["sinal_s"]}


@app.post("/workers/{worker_id}/sinal", dependencies=[Depends(exigir_token)])
def rota_sinal(worker_id: int):
    """Sinal de vida; 404 manda o worker se registrar de novo (ex.: banco recriado)."""

    if not banco.receber_sinal(BANCO, worker_id, CONFIG["prazo_job_s"]):
        raise HTTPException(status_code=404, detail="worker desconhecido")

    return {"ok": True}


@app.post("/workers/{worker_id}/proximo", dependencies=[Depends(exigir_token)])
def rota_proximo(worker_id: int):
    """Entrega o próximo job que esse worker pode fazer, ou {"job": None}."""

    return {"job": banco.pegar_job(BANCO, worker_id, CONFIG["prazo_job_s"])}


@app.post("/jobs/{job_id}/concluir", dependencies=[Depends(exigir_token)])
def rota_concluir(job_id: int, dados: dict = Body(...)):
    """Recebe o resultado do job (código de saída e o fim da saída do script)."""

    aceito = banco.concluir_job(
        BANCO,
        job_id,
        int(dados["worker_id"]),
        int(dados["codigo"]),
        str(dados.get("saida", "")),
    )

    return {"aceito": aceito}


@app.post("/jobs", dependencies=[Depends(exigir_token)])
def rota_disparar(dados: dict = Body(...)):
    """Executa agora uma automação cadastrada, um job por parâmetro (ex.: um por cliente)."""

    automacao = banco.buscar_automacao(BANCO, str(dados.get("automacao", "")))
    parametros = [str(p) for p in dados.get("parametros", [])] or [None]

    if not automacao:
        raise HTTPException(status_code=400, detail="automação não cadastrada")
    if not automacao["ativa"]:
        raise HTTPException(status_code=400, detail="automação desativada")
    if len(parametros) > MAX_POR_DISPARO:
        raise HTTPException(
            status_code=400, detail=f"no máximo {MAX_POR_DISPARO} por vez"
        )

    ids = banco.enfileirar(BANCO, automacao, parametros)
    log.info("Disparo manual: %s job(s) de %s.", len(ids), automacao["nome"])

    return {"ids": ids}


@app.get("/automacoes")
def rota_automacoes():
    """Automações cadastradas com os agendamentos (só leitura, para o painel)."""

    return banco.listar_automacoes(BANCO)


@app.post("/automacoes", dependencies=[Depends(exigir_token)])
def rota_salvar_automacao(dados: dict = Body(...)):
    """Cadastra ou atualiza uma automação."""

    nome = str(dados.get("nome", ""))
    etiquetas = [str(e) for e in dados.get("etiquetas", [])]
    simultaneos = int(dados.get("simultaneos", 1))

    if not NOME_AUTOMACAO.match(nome):
        raise HTTPException(status_code=400, detail="nome: só minúsculas, números e _")
    if not all(NOME_VALIDO.match(e) for e in etiquetas) or not 1 <= simultaneos <= 50:
        raise HTTPException(
            status_code=400, detail="etiquetas ou simultâneos inválidos"
        )

    banco.salvar_automacao(
        BANCO,
        {
            "nome": nome,
            "descricao": str(dados.get("descricao", ""))[:200],
            "etiquetas": etiquetas,
            "simultaneos": simultaneos,
            "ativa": bool(dados.get("ativa", True)),
        },
    )
    log.info("Automação %s salva.", nome)

    return {"ok": True}


@app.post("/automacoes/{nome}/agendamentos", dependencies=[Depends(exigir_token)])
def rota_agendar(nome: str, dados: dict = Body(...)):
    """Cria um agendamento: a cada N minutos, ou num horário nos dias escolhidos."""

    if not banco.buscar_automacao(BANCO, nome):
        raise HTTPException(status_code=404, detail="automação não cadastrada")

    agendamento = validar_agendamento(dados)
    agendamento["automacao"] = nome
    agendamento_id = banco.salvar_agendamento(BANCO, agendamento)
    log.info("Agendamento %s criado para %s.", agendamento_id, nome)

    return {"id": agendamento_id}


def validar_agendamento(dados):
    """Confere e normaliza os campos do agendamento; 400 se algo estiver errado.

    Chamada por rota_agendar.
    """

    tipo = dados.get("tipo")
    dias = [int(d) for d in dados.get("dias", range(7))]
    valido_ate = dados.get("valido_ate") or None

    try:
        if valido_ate:
            date.fromisoformat(valido_ate)
    except ValueError:
        raise HTTPException(status_code=400, detail="validade: use AAAA-MM-DD")

    if not dias or not all(0 <= d <= 6 for d in dias):
        raise HTTPException(status_code=400, detail="dias: de 0 (seg) a 6 (dom)")

    agendamento = {"tipo": tipo, "dias": sorted(set(dias)), "valido_ate": valido_ate}
    agendamento["parametro"] = (
        str(dados["parametro"]) if dados.get("parametro") else None
    )

    if tipo == banco.INTERVALO and 1 <= int(dados.get("a_cada_min", 0)) <= 10080:
        agendamento["a_cada_min"] = int(dados["a_cada_min"])
    elif tipo == banco.DIARIO and HORARIO_VALIDO.match(str(dados.get("horario", ""))):
        agendamento["horario"] = dados["horario"]
    else:
        raise HTTPException(
            status_code=400,
            detail="use tipo 'intervalo' com a_cada_min, ou 'diario' com horario HH:MM",
        )

    return agendamento


@app.get("/estado")
def rota_estado():
    """Dados do painel (só leitura): contagens, workers e últimos jobs."""

    estado = banco.montar_estado(BANCO)
    estado["extras"] = sorted(escalador.ESTADO["extras"])

    return estado


if __name__ == "__main__":
    main()
