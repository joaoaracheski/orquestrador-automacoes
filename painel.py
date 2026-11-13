"""Painel do orquestrador em Flet: automações, agendamentos, workers e fila, atualizando sozinho.

Uso: python painel.py   (ou python painel.py --web para abrir no navegador)
"""

import argparse
import asyncio
import functools
import json
import os
import urllib.error
import urllib.request
from datetime import datetime

import flet as ft

# De quanto em quanto tempo o painel busca o estado no master
ATUALIZAR_S = 1

# Largura máxima do conteúdo: em tela cheia, tabelas de ponta a ponta ficam ruins de ler
LARGURA = 1080

# Nomes dos dias como o master conta (segunda = 0)
DIAS = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]

# Cor de cada status, a mesma em todas as tabelas
CORES = {
    "fila": ft.Colors.AMBER_400,
    "executando": ft.Colors.LIGHT_BLUE_300,
    "sucesso": ft.Colors.GREEN_400,
    "erro": ft.Colors.RED_400,
    "vivo": ft.Colors.GREEN_400,
    "morto": ft.Colors.RED_400,
    "desligado": ft.Colors.GREY_500,
    "ativa": ft.Colors.GREEN_400,
    "desativada": ft.Colors.RED_400,
}

# Cores de fundo e borda dos cartões (tema escuro)
FUNDO_CARTAO, BORDA = "#161B22", "#30363D"


def main():
    """Lê as opções e abre o painel no app (padrão) ou no navegador (--web)."""

    args = ler_argumentos()
    iniciar = functools.partial(montar_tela, args=args)

    if args.web:
        ft.run(iniciar, view=ft.AppView.WEB_BROWSER, port=args.porta)
    else:
        ft.run(iniciar)


def ler_argumentos():
    """Endereço do master e modo de abertura. Chamada por main."""

    parser = argparse.ArgumentParser(description="Painel do orquestrador.")
    parser.add_argument("--master", default="http://127.0.0.1:8000")
    parser.add_argument("--web", action="store_true", help="abre no navegador")
    parser.add_argument("--porta", type=int, default=8550, help="porta do modo --web")

    return parser.parse_args()


def montar_tela(page, args):
    """Monta os controles no dict `tela`, mostra a página e liga a atualização automática.

    Chamada pelo Flet quando a janela abre.
    """

    page.title = "Orquestrador de automações"
    page.theme_mode = ft.ThemeMode.DARK
    page.bgcolor = "#0D1117"
    page.padding = 24
    page.scroll = ft.ScrollMode.AUTO

    tela = {
        "page": page,
        "master": args.master.rstrip("/"),
        "token": ft.TextField(
            label="Token para alterar",
            value=os.environ.get("ORQ_TOKEN", ""),
            password=True,
            can_reveal_password=True,
            width=230,
            dense=True,
        ),
        "numeros": {
            s: ft.Text("0", size=28, weight=ft.FontWeight.BOLD, color=CORES[s])
            for s in ("fila", "executando", "sucesso", "erro")
        },
        "automacoes": criar_tabela(
            [
                "Automação",
                "Precisa de",
                "Agendamentos",
                "Próxima execução",
                "Status",
                "",
            ],
            altura=64,
        ),
        "workers": criar_tabela(
            ["Nome", "Origem", "Etiquetas", "Status", "Fazendo agora"]
        ),
        "jobs": criar_tabela(
            ["#", "Automação", "Parâmetro", "Origem", "Status", "Worker", "Tentativas"]
        ),
        "aviso": ft.Text("Conectando ao master...", color=ft.Colors.GREY_500),
    }
    montar_formularios(tela)

    conteudo = ft.Column(
        [
            ft.Row(
                [
                    ft.Column(
                        [
                            ft.Text(
                                "Orquestrador de automações",
                                size=22,
                                weight=ft.FontWeight.BOLD,
                            ),
                            ft.Text(
                                "Roda as automações cadastradas no horário certo, "
                                "distribuindo entre os workers.",
                                color=ft.Colors.GREY_500,
                            ),
                        ],
                        spacing=2,
                    ),
                    tela["token"],
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            ),
            ft.Row(
                [
                    cartao_numero(tela["numeros"][s], rotulo)
                    for s, rotulo in (
                        ("fila", "na fila"),
                        ("executando", "executando"),
                        ("sucesso", "sucesso"),
                        ("erro", "erro"),
                    )
                ],
                spacing=12,
            ),
            titulo("Automações cadastradas"),
            moldura(tela["automacoes"]),
            # Cadastro logo abaixo das automações: as tabelas de baixo crescem a cada
            # segundo e empurrariam o formulário enquanto a pessoa preenche
            ft.Row(
                [tela["form_automacao"], tela["form_agendamento"]],
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.START,
            ),
            tela["aviso"],
            titulo("Workers"),
            moldura(tela["workers"]),
            titulo("Últimos jobs"),
            moldura(tela["jobs"]),
        ],
        spacing=12,
        width=LARGURA,
    )

    page.add(ft.Row([conteudo], alignment=ft.MainAxisAlignment.CENTER))
    page.run_task(atualizar_sempre, tela)


def montar_formularios(tela):
    """Cria os formulários de nova automação e novo agendamento. Chamada por montar_tela."""

    tela["f_nome"] = ft.TextField(label="nome_do_script", dense=True, width=170)
    tela["f_descricao"] = ft.TextField(label="O que ela faz", dense=True, width=230)
    tela["f_etiquetas"] = ft.TextField(
        label="Etiquetas: tela,navegador", dense=True, width=230
    )
    tela["f_simultaneos"] = ft.TextField(
        label="Simultâneos", value="1", dense=True, width=110
    )
    tela["form_automacao"] = cartao(
        [
            ft.Text("Nova automação (ou atualizar)", weight=ft.FontWeight.BOLD),
            ft.Row([tela["f_nome"], tela["f_descricao"]], wrap=True),
            ft.Row([tela["f_etiquetas"], tela["f_simultaneos"]], wrap=True),
            ft.FilledButton(
                "Salvar", on_click=functools.partial(salvar_automacao, tela)
            ),
        ]
    )

    tela["g_automacao"] = ft.Dropdown(label="Automação", dense=True, width=185)
    tela["g_tipo"] = ft.Dropdown(
        label="Tipo",
        value="intervalo",
        dense=True,
        width=175,
        options=[
            ft.DropdownOption(key="intervalo", text="a cada (min)"),
            ft.DropdownOption(key="diario", text="todo dia às"),
        ],
    )
    # Rótulo curto e o exemplo como dica: rótulo longo quebra dentro do campo
    tela["g_valor"] = criar_campo("Valor", "15 ou 08:00", 100)
    tela["g_dias"] = criar_campo("Dias", "0-4 = seg a sex", 130)
    tela["g_validade"] = criar_campo("Válido até", "AAAA-MM-DD", 140)
    tela["g_parametro"] = criar_campo("Parâmetro", "opcional", 180)
    tela["form_agendamento"] = cartao(
        [
            ft.Text("Novo agendamento", weight=ft.FontWeight.BOLD),
            ft.Row([tela["g_automacao"], tela["g_tipo"], tela["g_valor"]], wrap=True),
            ft.Row(
                [tela["g_dias"], tela["g_validade"], tela["g_parametro"]], wrap=True
            ),
            ft.FilledButton("Agendar", on_click=functools.partial(agendar, tela)),
        ]
    )


def criar_campo(rotulo, dica, largura):
    """Campo de texto compacto com rótulo e exemplo. Chamada por montar_formularios."""

    return ft.TextField(label=rotulo, hint_text=dica, dense=True, width=largura)


def criar_tabela(colunas, altura=38):
    """Tabela vazia com os títulos das colunas. Chamada por montar_tela.

    `altura`: altura máxima da linha (a de automações é maior: nome + descrição).
    """

    return ft.DataTable(
        columns=[ft.DataColumn(ft.Text(c, color=ft.Colors.GREY_500)) for c in colunas],
        rows=[],
        heading_row_height=36,
        data_row_min_height=36,
        data_row_max_height=altura,
        column_spacing=24,
        horizontal_margin=14,
    )


def moldura(tabela):
    """Cartão com rolagem lateral em volta da tabela. Chamada por montar_tela."""

    return cartao([ft.Row([tabela], scroll=ft.ScrollMode.AUTO)], padding=0)


def cartao(controles, padding=14):
    """Container com fundo e borda do tema. Chamada pelas funções de montagem."""

    return ft.Container(
        content=ft.Column(controles, spacing=10),
        bgcolor=FUNDO_CARTAO,
        border=ft.Border.all(1, BORDA),
        border_radius=10,
        padding=padding,
        expand=True,
    )


def cartao_numero(numero, rotulo):
    """Cartão com um número grande e o rótulo embaixo. Chamada por montar_tela."""

    return ft.Container(
        content=ft.Column(
            [numero, ft.Text(rotulo, color=ft.Colors.GREY_500)], spacing=0
        ),
        bgcolor=FUNDO_CARTAO,
        border=ft.Border.all(1, BORDA),
        border_radius=10,
        padding=ft.Padding.symmetric(horizontal=16, vertical=10),
        expand=True,
    )


def titulo(texto):
    """Título de seção, discreto. Chamada por montar_tela."""

    return ft.Text(
        texto.upper(), size=12, weight=ft.FontWeight.BOLD, color=ft.Colors.GREY_500
    )


async def atualizar_sempre(tela):
    """Busca o estado no master a cada segundo e redesenha as tabelas.

    Chamada via run_task por montar_tela. A busca roda em outra thread para não travar a tela.
    """

    while True:
        try:
            estado, automacoes = await asyncio.to_thread(buscar_dados, tela["master"])
            preencher_numeros(tela, estado)
            preencher_automacoes(tela, automacoes)
            preencher_workers(tela, estado)
            preencher_jobs(tela, estado)
            if tela["aviso"].value == "Conectando ao master..." or "Sem resposta" in (
                tela["aviso"].value or ""
            ):
                avisar(tela, "")
        except (urllib.error.URLError, OSError, ValueError):
            avisar(tela, "Sem resposta do master. Tentando de novo...", erro=True)

        tela["page"].update()
        await asyncio.sleep(ATUALIZAR_S)


def buscar_dados(master):
    """Devolve (estado, automações) do master. Chamada em thread por atualizar_sempre."""

    return chamar_api(master, "/estado"), chamar_api(master, "/automacoes")


def chamar_api(master, caminho, dados=None, token=""):
    """GET (sem dados) ou POST em JSON no master; devolve a resposta convertida.

    Levanta ValueError com a mensagem do master quando ele recusa (token, validação).
    """

    requisicao = urllib.request.Request(
        master + caminho,
        data=json.dumps(dados).encode() if dados is not None else None,
        headers={"Content-Type": "application/json", "X-Token": token},
        method="POST" if dados is not None else "GET",
    )

    try:
        with urllib.request.urlopen(requisicao, timeout=10) as resposta:
            return json.loads(resposta.read())
    except urllib.error.HTTPError as erro:
        if erro.code == 401:
            raise ValueError("token inválido")
        detalhe = json.loads(erro.read() or b"{}").get("detail", erro.code)
        raise ValueError(str(detalhe))


def preencher_numeros(tela, estado):
    """Atualiza os quatro números do topo. Chamada por atualizar_sempre."""

    for status, numero in tela["numeros"].items():
        numero.value = str(estado["contagem"][status])


def preencher_automacoes(tela, automacoes):
    """Redesenha a tabela de automações e a lista do formulário de agendamento.

    Chamada por atualizar_sempre.
    """

    linhas = []
    for a in automacoes:
        proximas = [
            g["proxima"] for g in a["agendamentos"] if g["ativo"] and g["proxima"]
        ]
        agendas = [descrever_agendamento(g) for g in a["agendamentos"]] or ["só manual"]
        botao = ft.OutlinedButton(
            "Executar agora",
            on_click=functools.partial(executar_agora, tela, a["nome"]),
        )
        linhas.append(
            [
                ft.Column(
                    [
                        ft.Text(a["nome"]),
                        ft.Text(a["descricao"], size=12, color=ft.Colors.GREY_500),
                    ],
                    spacing=0,
                    tight=True,
                    alignment=ft.MainAxisAlignment.CENTER,
                ),
                celula_etiquetas(a["etiquetas"]),
                ft.Column(
                    [ft.Text(t) for t in agendas],
                    spacing=0,
                    tight=True,
                    alignment=ft.MainAxisAlignment.CENTER,
                ),
                ft.Text(formatar_hora(min(proximas)) if proximas else "—"),
                celula_status("ativa" if a["ativa"] else "desativada"),
                botao,
            ]
        )

    tela["automacoes"].rows = [montar_linha(celulas) for celulas in linhas]

    nomes = [a["nome"] for a in automacoes]
    if [o.key for o in tela["g_automacao"].options] != nomes:
        tela["g_automacao"].options = [ft.DropdownOption(key=n) for n in nomes]


def preencher_workers(tela, estado):
    """Redesenha a tabela de workers. Chamada por atualizar_sempre."""

    tela["workers"].rows = [
        montar_linha(
            [
                ft.Text(w["nome"]),
                ft.Text(w["origem"]),
                celula_etiquetas(w["etiquetas"]),
                celula_status(w["status"]),
                ft.Text(w["job_atual"] or "—"),
            ]
        )
        for w in estado["workers"]
    ]


def preencher_jobs(tela, estado):
    """Redesenha a tabela dos últimos jobs. Chamada por atualizar_sempre."""

    tela["jobs"].rows = [
        montar_linha(
            [
                ft.Text(f"#{j['id']}"),
                ft.Text(j["automacao"]),
                ft.Text(j["parametro"] or "—"),
                ft.Text(j["origem"]),
                celula_status(j["status"]),
                ft.Text(j["worker"] or "—"),
                ft.Text(str(j["tentativas"])),
            ]
        )
        for j in estado["jobs"]
    ]


def montar_linha(controles):
    """Transforma uma lista de controles numa linha de tabela. Chamada pelos preencher_*."""

    return ft.DataRow(cells=[ft.DataCell(c) for c in controles])


def celula_status(status):
    """Texto do status na cor dele. Chamada pelos preencher_*."""

    return ft.Text(status, color=CORES.get(status, ft.Colors.GREY_500))


def celula_etiquetas(etiquetas_json):
    """Etiquetas como pequenas pílulas; sem etiqueta = "qualquer worker"."""

    etiquetas = json.loads(etiquetas_json)

    if not etiquetas:
        return ft.Text("qualquer worker", size=12, color=ft.Colors.GREY_500)

    return ft.Row(
        [
            ft.Container(
                content=ft.Text(e, size=12, color=ft.Colors.GREY_400),
                border=ft.Border.all(1, BORDA),
                border_radius=6,
                padding=ft.Padding.symmetric(horizontal=6, vertical=1),
            )
            for e in etiquetas
        ],
        spacing=4,
    )


def descrever_agendamento(agendamento):
    """Texto legível: "às 08:00 (seg, ter...) até 31/12/2026 · parâmetro".

    Chamada por preencher_automacoes.
    """

    dias = json.loads(agendamento["dias"])

    if agendamento["tipo"] == "intervalo":
        texto = f"a cada {agendamento['a_cada_min']} min"
    else:
        texto = f"às {agendamento['horario']}"

    if len(dias) < 7:
        texto += " (" + ", ".join(DIAS[d] for d in dias) + ")"
    if agendamento["valido_ate"]:
        texto += " até " + "/".join(reversed(agendamento["valido_ate"].split("-")))
    if agendamento["parametro"]:
        texto += f" · {agendamento['parametro']}"
    if not agendamento["ativo"]:
        texto += " · desativado"

    return texto


def formatar_hora(timestamp):
    """Timestamp -> "13/11 14:39:39". Chamada por preencher_automacoes."""

    return datetime.fromtimestamp(timestamp).strftime("%d/%m %H:%M:%S")


async def executar_agora(tela, nome, e):
    """Botão "Executar agora": coloca a automação na fila. Pede o token."""

    try:
        await asyncio.to_thread(
            chamar_api,
            tela["master"],
            "/jobs",
            {"automacao": nome},
            tela["token"].value,
        )
        avisar(tela, f"{nome} foi para a fila.")
    except (ValueError, urllib.error.URLError, OSError) as erro:
        avisar(tela, f"Não deu para executar: {erro}", erro=True)

    tela["page"].update()


async def salvar_automacao(tela, e):
    """Botão Salvar do cadastro: cria ou atualiza a automação. Pede o token."""

    etiquetas = [t.strip() for t in tela["f_etiquetas"].value.split(",") if t.strip()]
    dados = {
        "nome": tela["f_nome"].value.strip(),
        "descricao": tela["f_descricao"].value.strip(),
        "etiquetas": etiquetas,
        "simultaneos": int(tela["f_simultaneos"].value or 1),
    }

    try:
        await asyncio.to_thread(
            chamar_api, tela["master"], "/automacoes", dados, tela["token"].value
        )
        avisar(tela, f"Automação {dados['nome']} salva.")
        tela["f_nome"].value = tela["f_descricao"].value = tela["f_etiquetas"].value = (
            ""
        )
    except (ValueError, urllib.error.URLError, OSError) as erro:
        avisar(tela, f"Não deu para salvar: {erro}", erro=True)

    tela["page"].update()


async def agendar(tela, e):
    """Botão Agendar: cria o agendamento da automação escolhida. Pede o token."""

    nome = tela["g_automacao"].value
    dados = {
        "tipo": tela["g_tipo"].value,
        "valido_ate": tela["g_validade"].value.strip(),
        "parametro": tela["g_parametro"].value.strip(),
    }
    valor = tela["g_valor"].value.strip()

    try:
        if not nome:
            raise ValueError("escolha a automação")
        if dados["tipo"] == "intervalo":
            dados["a_cada_min"] = int(valor)
        else:
            dados["horario"] = valor
        if tela["g_dias"].value.strip():
            dados["dias"] = ler_dias(tela["g_dias"].value)

        await asyncio.to_thread(
            chamar_api,
            tela["master"],
            f"/automacoes/{nome}/agendamentos",
            dados,
            tela["token"].value,
        )
        avisar(tela, f"Agendado: {nome}.")
    except (ValueError, urllib.error.URLError, OSError) as erro:
        avisar(tela, f"Não deu para agendar: {erro}", erro=True)

    tela["page"].update()


def ler_dias(texto):
    """'0-4,6' -> [0, 1, 2, 3, 4, 6]. Chamada por agendar. ValueError se mal escrito."""

    dias = []
    for parte in texto.split(","):
        inicio, _, fim = parte.strip().partition("-")
        dias.extend(range(int(inicio), int(fim or inicio) + 1))

    return dias


def avisar(tela, texto, erro=False):
    """Mensagem curta embaixo do painel (vermelha se for erro). Chamada pelos handlers."""

    tela["aviso"].value = texto
    tela["aviso"].color = ft.Colors.RED_400 if erro else ft.Colors.GREY_500


if __name__ == "__main__":
    main()
