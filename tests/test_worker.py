"""Testes do worker: o script para quando o job deixa de ser dele, e só por um sinal novo."""

import threading
import time

import worker

JOB = {"id": 7, "automacao": "demorada", "parametro": None}


def criar_automacao(tmp_path, monkeypatch, codigo):
    """Cria automacoes/demorada.py com o código dado e aponta o worker para essa pasta."""

    pasta = tmp_path / "automacoes"
    pasta.mkdir()
    (pasta / "demorada.py").write_text(codigo, encoding="utf-8")
    monkeypatch.setattr(worker, "PASTA_AUTOMACOES", pasta)


def mandar_sinal_sem_o_job(dados_worker, espera_s):
    """Simula a thread de sinais: depois de `espera_s`, o master não lista mais o job."""

    time.sleep(espera_s)
    dados_worker["sinal"] = {"enviado": time.monotonic(), "jobs": set()}


def test_script_e_interrompido_quando_o_job_vai_para_outro_worker(tmp_path, monkeypatch):
    """Sinal novo sem o job: o worker mata o script e não devolve resultado."""

    criar_automacao(tmp_path, monkeypatch, "import time\ntime.sleep(30)\n")
    dados_worker = {"sinal": None}
    threading.Thread(
        target=mandar_sinal_sem_o_job, args=(dados_worker, 1.0), daemon=True
    ).start()

    inicio = time.monotonic()
    codigo, motivo = worker.executar(dados_worker, JOB)

    assert codigo is None
    assert "outro worker" in motivo
    assert time.monotonic() - inicio < 5


def test_sinal_antigo_nao_interrompe_job_novo(tmp_path, monkeypatch):
    """Um sinal enviado antes de o job chegar não conhecia o job: não pode matar o script."""

    criar_automacao(tmp_path, monkeypatch, "print('feito')\n")
    dados_worker = {"sinal": {"enviado": time.monotonic(), "jobs": set()}}
    time.sleep(0.01)

    assert worker.executar(dados_worker, JOB) == (0, "feito\n")


def test_job_que_continua_do_worker_termina_normal(tmp_path, monkeypatch):
    """Sinal novo listando o job: o script roda até o fim e devolve o código dele."""

    criar_automacao(tmp_path, monkeypatch, "import time, sys\ntime.sleep(1.5)\nsys.exit(3)\n")
    dados_worker = {"sinal": {"enviado": time.monotonic() + 0.5, "jobs": {JOB["id"]}}}

    codigo, _ = worker.executar(dados_worker, JOB)

    assert codigo == 3
