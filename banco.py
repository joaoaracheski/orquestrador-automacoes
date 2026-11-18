"""Banco do orquestrador (SQLite): automações cadastradas, agendamentos, workers e fila.

Todas as regras moram aqui, para o master e os testes usarem as mesmas.
"""

import json
import sqlite3
import time
from contextlib import closing
from datetime import date, datetime, timedelta

# Status de um job ao longo da vida
FILA, EXECUTANDO, SUCESSO, ERRO = "fila", "executando", "sucesso", "erro"

# Tipos de agendamento: a cada N minutos, ou num horário fixo nos dias escolhidos
INTERVALO, DIARIO = "intervalo", "diario"

# Dias da semana como o Python conta (segunda = 0), com os nomes que aparecem no painel
DIAS = ["seg", "ter", "qua", "qui", "sex", "sab", "dom"]

# Quantos jobs recentes o painel mostra
JOBS_NO_PAINEL = 12

TABELAS = """
CREATE TABLE IF NOT EXISTS automacoes (
    nome TEXT PRIMARY KEY,
    descricao TEXT NOT NULL DEFAULT '',
    etiquetas TEXT NOT NULL DEFAULT '[]',
    simultaneos INTEGER NOT NULL DEFAULT 1,
    ativa INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS agendamentos (
    id INTEGER PRIMARY KEY,
    automacao TEXT NOT NULL REFERENCES automacoes (nome),
    tipo TEXT NOT NULL,
    a_cada_min INTEGER,
    horario TEXT,
    dias TEXT NOT NULL DEFAULT '[0,1,2,3,4,5,6]',
    valido_ate TEXT,
    parametro TEXT,
    ativo INTEGER NOT NULL DEFAULT 1,
    proxima REAL
);
CREATE TABLE IF NOT EXISTS workers (
    id INTEGER PRIMARY KEY,
    nome TEXT NOT NULL,
    etiquetas TEXT NOT NULL,
    origem TEXT NOT NULL,
    status TEXT NOT NULL,
    ultimo_sinal REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    automacao TEXT NOT NULL,
    parametro TEXT,
    origem TEXT NOT NULL DEFAULT 'manual',
    etiquetas TEXT NOT NULL,
    limite INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL,
    worker_id INTEGER,
    tentativas INTEGER NOT NULL DEFAULT 0,
    prazo REAL,
    criado REAL NOT NULL,
    inicio REAL,
    fim REAL,
    codigo INTEGER,
    saida TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs (status);
"""


def conectar(caminho):
    """Abre uma conexão nova (uma por operação: o FastAPI atende em várias threads).

    Sempre usada com closing(): o `with` da conexão sozinho não fecha o arquivo.
    WAL deixa o painel ler enquanto o loop do master grava.
    """

    conexao = sqlite3.connect(caminho, timeout=10, isolation_level=None)
    conexao.row_factory = sqlite3.Row
    conexao.execute("PRAGMA journal_mode=WAL")

    return conexao


def criar_tabelas(caminho):
    """Cria as tabelas se ainda não existirem. Chamada pelo master ao subir e pelos testes."""

    with closing(conectar(caminho)) as conexao:
        conexao.executescript(TABELAS)


# ---------------------------------------------------------------- automações


def salvar_automacao(caminho, dados):
    """Cadastra ou atualiza uma automação (nome, descrição, etiquetas, simultâneos, ativa).

    Chamada pela rota POST /automacoes e pela carga inicial de exemplo.
    """

    with closing(conectar(caminho)) as conexao:
        conexao.execute(
            "INSERT INTO automacoes (nome, descricao, etiquetas, simultaneos, ativa)"
            " VALUES (?, ?, ?, ?, ?) ON CONFLICT(nome) DO UPDATE SET"
            " descricao = excluded.descricao, etiquetas = excluded.etiquetas,"
            " simultaneos = excluded.simultaneos, ativa = excluded.ativa",
            (
                dados["nome"],
                dados.get("descricao", ""),
                json.dumps(sorted(dados.get("etiquetas", []))),
                int(dados.get("simultaneos", 1)),
                int(dados.get("ativa", True)),
            ),
        )


def buscar_automacao(caminho, nome):
    """Devolve a automação cadastrada (dict) ou None. Chamada nos disparos."""

    with closing(conectar(caminho)) as conexao:
        linha = conexao.execute(
            "SELECT * FROM automacoes WHERE nome = ?", (nome,)
        ).fetchone()

    return dict(linha) if linha else None


def salvar_agendamento(caminho, dados):
    """Cria um agendamento e devolve o id. A próxima execução é calculada pelo loop.

    Chamada pela rota POST /automacoes/{nome}/agendamentos e pela carga inicial.
    """

    with closing(conectar(caminho)) as conexao:
        cursor = conexao.execute(
            "INSERT INTO agendamentos"
            " (automacao, tipo, a_cada_min, horario, dias, valido_ate, parametro)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                dados["automacao"],
                dados["tipo"],
                dados.get("a_cada_min"),
                dados.get("horario"),
                json.dumps(dados.get("dias", list(range(7)))),
                dados.get("valido_ate"),
                dados.get("parametro"),
            ),
        )

    return cursor.lastrowid


def semear_exemplos(caminho, exemplos):
    """Carrega automações e agendamentos de exemplo se o banco ainda não tem nenhuma.

    Chamada pelo master ao subir. Banco com cadastro existente não é tocado.
    """

    with closing(conectar(caminho)) as conexao:
        if conexao.execute("SELECT COUNT(*) FROM automacoes").fetchone()[0]:
            return 0

    for automacao in exemplos["automacoes"]:
        salvar_automacao(caminho, automacao)
    for agendamento in exemplos["agendamentos"]:
        salvar_agendamento(caminho, agendamento)

    return len(exemplos["automacoes"])


def listar_automacoes(caminho):
    """Automações com os agendamentos de cada uma, para o painel. Chamada por GET /automacoes."""

    with closing(conectar(caminho)) as conexao:
        automacoes = [
            dict(a) for a in conexao.execute("SELECT * FROM automacoes ORDER BY nome")
        ]
        agendamentos = [
            dict(a) for a in conexao.execute("SELECT * FROM agendamentos ORDER BY id")
        ]

    for automacao in automacoes:
        automacao["agendamentos"] = [
            a for a in agendamentos if a["automacao"] == automacao["nome"]
        ]

    return automacoes


# ------------------------------------------------------------- agendamentos


def calcular_proxima(agendamento, agora):
    """Devolve o horário (timestamp) da próxima execução depois de `agora`.

    Intervalo: agora + N minutos. Diário: o próximo dia permitido no horário marcado
    (hoje mesmo, se ainda não passou). Chamada pelo loop e pelos testes.
    """

    if agendamento["tipo"] == INTERVALO:
        return agora + int(agendamento["a_cada_min"]) * 60

    hora, minuto = (int(p) for p in agendamento["horario"].split(":"))
    dias = json.loads(agendamento["dias"])
    base = datetime.fromtimestamp(agora).replace(
        hour=hora, minute=minuto, second=0, microsecond=0
    )

    for avanco in range(8):
        candidato = base + timedelta(days=avanco)
        if candidato.weekday() in dias and candidato.timestamp() > agora:
            return candidato.timestamp()

    return None


def desativar_vencidos(caminho, hoje=None):
    """Desativa os agendamentos cuja validade já passou. Devolve quantos.

    Chamada pelo loop do master (etapa 1).
    """

    hoje = (hoje or date.today()).isoformat()

    with closing(conectar(caminho)) as conexao:
        cursor = conexao.execute(
            "UPDATE agendamentos SET ativo = 0, proxima = NULL"
            " WHERE ativo = 1 AND valido_ate IS NOT NULL AND valido_ate < ?",
            (hoje,),
        )

    return cursor.rowcount


def calcular_novos(caminho, agora=None):
    """Calcula a próxima execução dos agendamentos ativos que ainda não têm uma.

    Chamada pelo loop do master (etapa 2). Devolve quantos calculou.
    """

    agora = agora or time.time()

    with closing(conectar(caminho)) as conexao:
        novos = conexao.execute(
            "SELECT * FROM agendamentos WHERE ativo = 1 AND proxima IS NULL"
        ).fetchall()
        for agendamento in novos:
            conexao.execute(
                "UPDATE agendamentos SET proxima = ? WHERE id = ?",
                (calcular_proxima(agendamento, agora), agendamento["id"]),
            )

    return len(novos)


def enfileirar_agendados(caminho, agora=None):
    """Põe na fila o que venceu e já marca a próxima execução de cada agendamento.

    Chamada pelo loop do master (etapa 3). Automação desativada não entra na fila.
    Devolve os nomes das automações enfileiradas.
    """

    agora = agora or time.time()

    with closing(conectar(caminho)) as conexao:
        vencidos = conexao.execute(
            "SELECT g.*, a.etiquetas AS etq, a.simultaneos FROM agendamentos g"
            " JOIN automacoes a ON a.nome = g.automacao"
            " WHERE g.ativo = 1 AND a.ativa = 1 AND g.proxima <= ?",
            (agora,),
        ).fetchall()
        for agendamento in vencidos:
            conexao.execute(
                "UPDATE agendamentos SET proxima = ? WHERE id = ?",
                (calcular_proxima(agendamento, agora), agendamento["id"]),
            )

    for agendamento in vencidos:
        automacao = {
            "nome": agendamento["automacao"],
            "etiquetas": agendamento["etq"],
            "simultaneos": agendamento["simultaneos"],
        }
        enfileirar(caminho, automacao, [agendamento["parametro"]], "agendada", agora)

    return [a["automacao"] for a in vencidos]


# ------------------------------------------------------------- fila e workers


def enfileirar(caminho, automacao, parametros=(None,), origem="manual", agora=None):
    """Coloca na fila um job por parâmetro (ex.: um por cliente) e devolve os ids.

    `automacao` é o cadastro (dict do banco): etiquetas exigidas e limite simultâneo.
    Chamada pela rota POST /jobs e pelos agendamentos.
    """

    agora = agora or time.time()
    etiquetas = automacao.get("etiquetas", "[]")
    etiquetas = (
        etiquetas if isinstance(etiquetas, str) else json.dumps(sorted(etiquetas))
    )
    ids = []

    with closing(conectar(caminho)) as conexao:
        for parametro in parametros:
            cursor = conexao.execute(
                "INSERT INTO jobs"
                " (automacao, parametro, origem, etiquetas, limite, status, criado)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    automacao["nome"],
                    parametro,
                    origem,
                    etiquetas,
                    int(automacao.get("simultaneos", 1)),
                    FILA,
                    agora,
                ),
            )
            ids.append(cursor.lastrowid)

    return ids


def registrar_worker(caminho, nome, etiquetas, origem, agora=None):
    """Cadastra um worker que acabou de subir e devolve o id dele.

    Chamada pela rota POST /workers. `origem` é "processo", "docker" ou "maquina".
    """

    with closing(conectar(caminho)) as conexao:
        cursor = conexao.execute(
            "INSERT INTO workers (nome, etiquetas, origem, status, ultimo_sinal)"
            " VALUES (?, ?, ?, 'vivo', ?)",
            (nome, json.dumps(sorted(etiquetas)), origem, agora or time.time()),
        )

    return cursor.lastrowid


def receber_sinal(caminho, worker_id, prazo_s, agora=None):
    """Sinal de vida do worker: atualiza o horário e renova o prazo dos jobs dele.

    Chamada pela rota POST /workers/{id}/sinal. Devolve os ids dos jobs que ainda são
    deste worker: um job que não está na lista foi devolvido à fila (o worker ficou sem
    sinal) e o worker precisa parar de rodá-lo. Devolve None se o worker não existe
    (ex.: banco recriado), para ele se registrar de novo.
    """

    agora = agora or time.time()

    with closing(conectar(caminho)) as conexao:
        cursor = conexao.execute(
            "UPDATE workers SET ultimo_sinal = ?, status = 'vivo' WHERE id = ?",
            (agora, worker_id),
        )
        if cursor.rowcount != 1:
            return None
        conexao.execute(
            "UPDATE jobs SET prazo = ? WHERE worker_id = ? AND status = ?",
            (agora + prazo_s, worker_id, EXECUTANDO),
        )
        meus = conexao.execute(
            "SELECT id FROM jobs WHERE worker_id = ? AND status = ?",
            (worker_id, EXECUTANDO),
        ).fetchall()

    return [job["id"] for job in meus]


def pegar_job(caminho, worker_id, prazo_s, agora=None):
    """Entrega ao worker o job mais antigo que ele consegue fazer, ou None.

    Regras: o worker precisa ter todas as etiquetas do job, e cada automação respeita o
    seu limite de execuções simultâneas (padrão 1: nunca roda duas vezes ao mesmo tempo).
    Chamada pela rota POST /workers/{id}/proximo.
    """

    agora = agora or time.time()

    with closing(conectar(caminho)) as conexao:
        # IMMEDIATE trava a escrita: dois workers pedindo juntos não levam o mesmo job
        conexao.execute("BEGIN IMMEDIATE")
        worker = conexao.execute(
            "SELECT etiquetas FROM workers WHERE id = ?", (worker_id,)
        ).fetchone()

        if not worker:
            conexao.execute("COMMIT")
            return None

        tem = set(json.loads(worker["etiquetas"]))
        candidatos = conexao.execute(
            "SELECT * FROM jobs j WHERE status = ? AND (SELECT COUNT(*) FROM jobs e"
            " WHERE e.automacao = j.automacao AND e.status = ?) < j.limite ORDER BY id",
            (FILA, EXECUTANDO),
        ).fetchall()
        job = next(
            (j for j in candidatos if set(json.loads(j["etiquetas"])) <= tem), None
        )

        if job:
            conexao.execute(
                "UPDATE jobs SET status = ?, worker_id = ?, inicio = ?, prazo = ?"
                " WHERE id = ?",
                (EXECUTANDO, worker_id, agora, agora + prazo_s, job["id"]),
            )

        conexao.execute("COMMIT")

    return dict(job) if job else None


def concluir_job(caminho, job_id, worker_id, codigo, saida, agora=None):
    """Grava o fim do job pelo código de saída (0 = sucesso, outro = erro).

    Chamada pela rota POST /jobs/{id}/concluir. Ignora (devolve False) se o job já foi
    devolvido à fila e pego por outro worker: o resultado atrasado não vale mais.
    """

    status = SUCESSO if codigo == 0 else ERRO

    with closing(conectar(caminho)) as conexao:
        cursor = conexao.execute(
            "UPDATE jobs SET status = ?, fim = ?, codigo = ?, saida = ?, prazo = NULL"
            " WHERE id = ? AND worker_id = ? AND status = ?",
            (
                status,
                agora or time.time(),
                codigo,
                saida[-2000:],
                job_id,
                worker_id,
                EXECUTANDO,
            ),
        )

    return cursor.rowcount == 1


def marcar_desligado(caminho, nome):
    """Marca o worker como "desligado" (derrubado de propósito pelo escalador).

    Chamada pelo escalador. Separa de "morto", que é worker que parou de responder.
    """

    with closing(conectar(caminho)) as conexao:
        conexao.execute(
            "UPDATE workers SET status = 'desligado' WHERE nome = ? AND status = 'vivo'",
            (nome,),
        )


def marcar_workers_mortos(caminho, limite_s, agora=None):
    """Marca como "morto" o worker sem sinal há mais de `limite_s` segundos.

    Chamada pelo loop do master. Devolve os nomes marcados agora.
    """

    limite = (agora or time.time()) - limite_s

    with closing(conectar(caminho)) as conexao:
        mortos = conexao.execute(
            "SELECT nome FROM workers WHERE status = 'vivo' AND ultimo_sinal < ?",
            (limite,),
        ).fetchall()
        conexao.execute(
            "UPDATE workers SET status = 'morto'"
            " WHERE status = 'vivo' AND ultimo_sinal < ?",
            (limite,),
        )

    return [m["nome"] for m in mortos]


def recuperar_vencidos(caminho, max_tentativas, agora=None):
    """Devolve à fila os jobs cujo worker sumiu (prazo vencido); na última tentativa, erro.

    Chamada pelo loop do master, inclusive logo ao subir: recupera o que estava
    "executando" quando o próprio master caiu. Devolve (devolvidos, desistidos).
    """

    agora = agora or time.time()

    with closing(conectar(caminho)) as conexao:
        vencidos = conexao.execute(
            "SELECT id, tentativas FROM jobs WHERE status = ? AND prazo < ?",
            (EXECUTANDO, agora),
        ).fetchall()

        devolvidos = desistidos = 0
        # O WHERE repete o status e o prazo: entre o SELECT e o UPDATE o worker pode ter
        # concluído o job (ou renovado o prazo), e aí ele não pode voltar para a fila
        ainda_vencido = " WHERE id = ? AND status = ? AND prazo < ?"
        for job in vencidos:
            tentativas = job["tentativas"] + 1

            if tentativas >= max_tentativas:
                cursor = conexao.execute(
                    "UPDATE jobs SET status = ?, tentativas = ?, fim = ?, prazo = NULL,"
                    " saida = 'O worker parou de responder em todas as tentativas.'"
                    + ainda_vencido,
                    (ERRO, tentativas, agora, job["id"], EXECUTANDO, agora),
                )
                desistidos += cursor.rowcount
            else:
                cursor = conexao.execute(
                    "UPDATE jobs SET status = ?, tentativas = ?, worker_id = NULL,"
                    " inicio = NULL, prazo = NULL" + ainda_vencido,
                    (FILA, tentativas, job["id"], EXECUTANDO, agora),
                )
                devolvidos += cursor.rowcount

    return devolvidos, desistidos


def contar_fila(caminho):
    """Quantos jobs estão esperando na fila. Chamada pelo escalador."""

    with closing(conectar(caminho)) as conexao:
        return conexao.execute(
            "SELECT COUNT(*) FROM jobs WHERE status = ?", (FILA,)
        ).fetchone()[0]


def listar_ociosos(caminho, nomes):
    """Dos workers com esses nomes, devolve os vivos que não estão com job.

    Chamada pelo escalador antes de derrubar um extra: nunca derruba no meio de um job.
    """

    if not nomes:
        return []

    marcas = ",".join("?" * len(nomes))

    with closing(conectar(caminho)) as conexao:
        linhas = conexao.execute(
            f"SELECT nome FROM workers w WHERE nome IN ({marcas}) AND status = 'vivo'"
            " AND NOT EXISTS (SELECT 1 FROM jobs j"
            " WHERE j.worker_id = w.id AND j.status = ?)",
            (*nomes, EXECUTANDO),
        ).fetchall()

    return [linha["nome"] for linha in linhas]


def montar_estado(caminho, agora=None):
    """Resumo para o painel: contagem por status, workers e jobs recentes.

    Chamada pela rota GET /estado.
    """

    agora = agora or time.time()

    with closing(conectar(caminho)) as conexao:
        contagem = {
            linha["status"]: linha["total"]
            for linha in conexao.execute(
                "SELECT status, COUNT(*) AS total FROM jobs GROUP BY status"
            )
        }
        workers = conexao.execute(
            "SELECT w.*, (SELECT j.automacao || ' #' || j.id FROM jobs j"
            " WHERE j.worker_id = w.id AND j.status = ?) AS job_atual"
            " FROM workers w ORDER BY w.status DESC, w.id",
            (EXECUTANDO,),
        ).fetchall()
        jobs = conexao.execute(
            "SELECT j.id, j.automacao, j.parametro, j.origem, j.status, j.tentativas,"
            " j.codigo, w.nome AS worker FROM jobs j"
            " LEFT JOIN workers w ON w.id = j.worker_id ORDER BY j.id DESC LIMIT ?",
            (JOBS_NO_PAINEL,),
        ).fetchall()

    return {
        "contagem": {s: contagem.get(s, 0) for s in (FILA, EXECUTANDO, SUCESSO, ERRO)},
        "workers": [
            {**dict(w), "segundos_sem_sinal": round(agora - w["ultimo_sinal"], 1)}
            for w in workers
        ],
        "jobs": [dict(j) for j in jobs],
    }
