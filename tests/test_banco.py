"""Testes das regras: agendamentos, etiquetas, limite simultâneo, prazo e retentativas."""

import functools
from datetime import date, datetime
from types import SimpleNamespace

import banco

BAIXAR = {"nome": "baixar", "etiquetas": ["navegador"], "simultaneos": 10}
LEGADO = {"nome": "legado", "etiquetas": ["tela"]}
CONCILIAR = {"nome": "conciliar", "etiquetas": []}


def criar(tmp_path):
    """Banco novo e vazio para cada teste."""

    caminho = tmp_path / "teste.db"
    banco.criar_tabelas(caminho)

    return caminho


def momento(texto):
    """'2026-11-13 08:30' -> timestamp, para os testes de horário ficarem legíveis."""

    return datetime.strptime(texto, "%Y-%m-%d %H:%M").timestamp()


# ------------------------------------------------------------- agendamentos


def test_intervalo_soma_os_minutos():
    """A cada 15 min: a próxima é 15 min depois de agora."""

    agendamento = {"tipo": banco.INTERVALO, "a_cada_min": 15}

    assert banco.calcular_proxima(agendamento, 1000) == 1000 + 15 * 60


def test_diario_roda_hoje_se_o_horario_ainda_nao_passou():
    """Sexta 07:00, agendado para 08:00 todo dia: roda hoje às 08:00."""

    agendamento = {"tipo": banco.DIARIO, "horario": "08:00", "dias": "[0,1,2,3,4,5,6]"}

    proxima = banco.calcular_proxima(agendamento, momento("2026-11-13 07:00"))
    assert proxima == momento("2026-11-13 08:00")


def test_diario_pula_para_o_proximo_dia_permitido():
    """Sexta 09:00, agendado de segunda a sexta às 08:00: só na segunda."""

    agendamento = {"tipo": banco.DIARIO, "horario": "08:00", "dias": "[0,1,2,3,4]"}

    proxima = banco.calcular_proxima(agendamento, momento("2026-11-13 09:00"))
    assert proxima == momento("2026-11-16 08:00")


def test_agendamento_vencido_e_desativado(tmp_path):
    """Validade até ontem: o loop desativa e ele não roda mais."""

    caminho = criar(tmp_path)
    banco.salvar_automacao(caminho, CONCILIAR)
    banco.salvar_agendamento(
        caminho,
        {
            "automacao": "conciliar",
            "tipo": banco.INTERVALO,
            "a_cada_min": 1,
            "valido_ate": "2026-11-12",
        },
    )

    assert banco.desativar_vencidos(caminho, hoje=date(2026, 11, 13)) == 1
    assert banco.calcular_novos(caminho) == 0


def test_loop_enfileira_o_que_venceu_e_marca_a_proxima(tmp_path):
    """Calcula a próxima, e quando ela chega o job entra na fila uma vez só."""

    caminho = criar(tmp_path)
    banco.salvar_automacao(caminho, BAIXAR)
    banco.salvar_agendamento(
        caminho,
        {
            "automacao": "baixar",
            "tipo": banco.INTERVALO,
            "a_cada_min": 10,
            "parametro": "cliente",
        },
    )

    assert banco.calcular_novos(caminho, agora=1000) == 1
    assert banco.enfileirar_agendados(caminho, agora=1000 + 300) == []
    assert banco.enfileirar_agendados(caminho, agora=1000 + 600) == ["baixar"]
    assert banco.enfileirar_agendados(caminho, agora=1000 + 601) == []

    job = banco.montar_estado(caminho)["jobs"][0]
    assert (job["parametro"], job["origem"]) == ("cliente", "agendada")


def test_automacao_desativada_nao_entra_na_fila(tmp_path):
    """Desligar a automação suspende todos os agendamentos dela."""

    caminho = criar(tmp_path)
    banco.salvar_automacao(caminho, {**CONCILIAR, "ativa": False})
    banco.salvar_agendamento(
        caminho, {"automacao": "conciliar", "tipo": banco.INTERVALO, "a_cada_min": 1}
    )
    banco.calcular_novos(caminho, agora=1000)

    assert banco.enfileirar_agendados(caminho, agora=1000 + 120) == []


def test_exemplos_so_entram_em_banco_vazio(tmp_path):
    """A carga de exemplo não mexe em banco que já tem cadastro."""

    caminho = criar(tmp_path)
    exemplos = {"automacoes": [CONCILIAR], "agendamentos": []}

    assert banco.semear_exemplos(caminho, exemplos) == 1
    assert banco.semear_exemplos(caminho, exemplos) == 0


# ------------------------------------------------------------- fila e workers


def test_job_so_vai_para_worker_com_as_etiquetas(tmp_path):
    """Job que exige "tela" não vai para worker só de navegador."""

    caminho = criar(tmp_path)
    banco.enfileirar(caminho, LEGADO)
    sem_tela = banco.registrar_worker(caminho, "w1", ["navegador"], "processo")
    com_tela = banco.registrar_worker(caminho, "w2", ["tela", "windows"], "maquina")

    assert banco.pegar_job(caminho, sem_tela, 10) is None
    assert banco.pegar_job(caminho, com_tela, 10)["automacao"] == "legado"


def test_automacao_exclusiva_nao_roda_duas_vezes_ao_mesmo_tempo(tmp_path):
    """Limite padrão 1: o segundo job da mesma automação espera o primeiro terminar."""

    caminho = criar(tmp_path)
    banco.enfileirar(caminho, CONCILIAR, ["a", "b"])
    w1 = banco.registrar_worker(caminho, "w1", [], "processo")
    w2 = banco.registrar_worker(caminho, "w2", [], "processo")

    primeiro = banco.pegar_job(caminho, w1, 10)
    assert banco.pegar_job(caminho, w2, 10) is None

    banco.concluir_job(caminho, primeiro["id"], w1, 0, "ok")
    assert banco.pegar_job(caminho, w2, 10)["parametro"] == "b"


def test_automacao_paralela_respeita_o_limite(tmp_path):
    """Com simultaneos=2, o terceiro job espera."""

    caminho = criar(tmp_path)
    banco.enfileirar(caminho, {"nome": "baixar", "simultaneos": 2}, ["a", "b", "c"])
    workers = [
        banco.registrar_worker(caminho, f"w{i}", [], "processo") for i in range(3)
    ]
    pegos = [banco.pegar_job(caminho, w, 10) for w in workers]

    assert [p["parametro"] if p else None for p in pegos] == ["a", "b", None]


def test_worker_que_sumiu_devolve_o_job_para_a_fila(tmp_path):
    """Prazo vencido sem sinal de vida: o job volta para a fila e outro worker termina."""

    caminho = criar(tmp_path)
    banco.enfileirar(caminho, BAIXAR, ["cliente"])
    caiu = banco.registrar_worker(caminho, "caiu", ["navegador"], "docker")
    job = banco.pegar_job(caminho, caiu, prazo_s=10, agora=1000)

    assert banco.recuperar_vencidos(caminho, 3, agora=1005) == (0, 0)
    assert banco.recuperar_vencidos(caminho, 3, agora=1011) == (1, 0)

    outro = banco.registrar_worker(caminho, "outro", ["navegador"], "docker")
    assert banco.pegar_job(caminho, outro, 10, agora=1012)["id"] == job["id"]


def test_sinal_de_vida_renova_o_prazo(tmp_path):
    """Job longo de worker vivo não é devolvido."""

    caminho = criar(tmp_path)
    banco.enfileirar(caminho, BAIXAR)
    vivo = banco.registrar_worker(caminho, "vivo", ["navegador"], "processo")
    banco.pegar_job(caminho, vivo, prazo_s=10, agora=1000)
    banco.receber_sinal(caminho, vivo, prazo_s=10, agora=1008)

    assert banco.recuperar_vencidos(caminho, 3, agora=1015) == (0, 0)


def test_sinal_devolve_os_jobs_que_ainda_sao_do_worker(tmp_path):
    """O sinal lista o job do worker; depois de devolvido à fila, a lista vem vazia."""

    caminho = criar(tmp_path)
    banco.enfileirar(caminho, BAIXAR)
    lento = banco.registrar_worker(caminho, "lento", ["navegador"], "docker")
    job = banco.pegar_job(caminho, lento, prazo_s=10, agora=1000)

    assert banco.receber_sinal(caminho, lento, 10, agora=1002) == [job["id"]]

    banco.recuperar_vencidos(caminho, 3, agora=1020)
    assert banco.receber_sinal(caminho, lento, 10, agora=1021) == []
    assert banco.receber_sinal(caminho, 999, 10) is None


def executar_e_concluir(conexao, corrida, sql, parametros=()):
    """execute que, logo depois do SELECT dos vencidos, conclui o job (o worker terminou)."""

    resultado = conexao.execute(sql, parametros)
    if sql.startswith("SELECT id, tentativas") and not corrida["feito"]:
        corrida["feito"] = True
        banco.concluir_job(corrida["caminho"], corrida["job"], corrida["worker"], 0, "ok")
    return resultado


def conectar_com_corrida(conectar_original, corrida, caminho):
    """Conexão de verdade com o execute trocado por executar_e_concluir."""

    conexao = conectar_original(caminho)
    return SimpleNamespace(
        execute=functools.partial(executar_e_concluir, conexao, corrida),
        close=conexao.close,
    )


def test_job_concluido_durante_a_recuperacao_nao_volta_para_a_fila(
    tmp_path, monkeypatch
):
    """Worker que conclui entre a leitura e a devolução: o sucesso vale e o job não roda de novo."""

    caminho = criar(tmp_path)
    banco.enfileirar(caminho, BAIXAR)
    worker = banco.registrar_worker(caminho, "w", ["navegador"], "docker")
    job = banco.pegar_job(caminho, worker, prazo_s=10, agora=1000)
    corrida = {"caminho": caminho, "job": job["id"], "worker": worker, "feito": False}
    monkeypatch.setattr(
        banco,
        "conectar",
        functools.partial(conectar_com_corrida, banco.conectar, corrida),
    )

    assert banco.recuperar_vencidos(caminho, 3, agora=1020) == (0, 0)
    monkeypatch.undo()
    assert banco.montar_estado(caminho)["contagem"]["sucesso"] == 1


def test_desiste_depois_do_maximo_de_tentativas(tmp_path):
    """Na última tentativa o job vira erro em vez de voltar à fila para sempre."""

    caminho = criar(tmp_path)
    banco.enfileirar(caminho, BAIXAR)
    agora = 1000

    for _ in range(2):
        w = banco.registrar_worker(caminho, "w", ["navegador"], "docker")
        banco.pegar_job(caminho, w, prazo_s=10, agora=agora)
        agora += 20
        resultado = banco.recuperar_vencidos(caminho, max_tentativas=2, agora=agora)

    assert resultado == (0, 1)
    assert banco.montar_estado(caminho)["contagem"]["erro"] == 1


def test_resultado_atrasado_de_worker_antigo_e_ignorado(tmp_path):
    """Se o job foi devolvido e pego por outro, o primeiro worker não pode concluí-lo."""

    caminho = criar(tmp_path)
    banco.enfileirar(caminho, BAIXAR)
    lento = banco.registrar_worker(caminho, "lento", ["navegador"], "docker")
    job = banco.pegar_job(caminho, lento, prazo_s=10, agora=1000)
    banco.recuperar_vencidos(caminho, 3, agora=1020)
    novo = banco.registrar_worker(caminho, "novo", ["navegador"], "docker")
    banco.pegar_job(caminho, novo, 10, agora=1021)

    assert banco.concluir_job(caminho, job["id"], lento, 0, "atrasado") is False
    assert banco.concluir_job(caminho, job["id"], novo, 0, "ok") is True


def test_codigo_de_saida_define_sucesso_ou_erro(tmp_path):
    """0 = sucesso; qualquer outro código = erro."""

    caminho = criar(tmp_path)
    banco.enfileirar(caminho, BAIXAR, ["a", "b"])
    w = banco.registrar_worker(caminho, "w", ["navegador"], "processo")
    banco.concluir_job(caminho, banco.pegar_job(caminho, w, 10)["id"], w, 0, "ok")
    banco.concluir_job(caminho, banco.pegar_job(caminho, w, 10)["id"], w, 1, "falhou")

    contagem = banco.montar_estado(caminho)["contagem"]
    assert (contagem["sucesso"], contagem["erro"]) == (1, 1)


def test_extra_derrubado_fica_desligado_e_nao_morto(tmp_path):
    """Derrubado pelo escalador = "desligado"; o loop não o confunde com queda."""

    caminho = criar(tmp_path)
    banco.registrar_worker(caminho, "extra-1", [], "processo", agora=1000)
    banco.marcar_desligado(caminho, "extra-1")

    assert banco.marcar_workers_mortos(caminho, limite_s=8, agora=2000) == []
    assert banco.montar_estado(caminho)["workers"][0]["status"] == "desligado"


def test_worker_sem_sinal_e_marcado_como_morto(tmp_path):
    """Sem sinal além do limite, o worker aparece como morto no painel."""

    caminho = criar(tmp_path)
    banco.registrar_worker(caminho, "w", [], "processo", agora=1000)

    assert banco.marcar_workers_mortos(caminho, limite_s=8, agora=1005) == []
    assert banco.marcar_workers_mortos(caminho, limite_s=8, agora=1010) == ["w"]
