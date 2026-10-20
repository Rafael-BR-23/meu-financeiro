from datetime import date, timedelta
from calendar import monthrange
from models import ExtratoNubank, Pendencia, Meta, db


def obter_evolucao_saldo():
    """
    Retorna dados dos últimos 6 meses agrupados por mês.
    Calcula: entradas - saídas de cada mês do extrato importado.
    O período é baseado no último mês que tem dados no extrato.
    """
    # Encontrar o último mês que tem dados no extrato
    ultima_transacao = ExtratoNubank.query.order_by(ExtratoNubank.data.desc()).first()
    if not ultima_transacao:
        return {"labels": [], "entradas": [], "saidas": []}

    # Usar o último mês com dados como referência (não o mês atual)
    ultimo_mes_com_dado = ultima_transacao.data.replace(day=1)

    labels = []
    entradas = []
    saidas = []

    for i in range(5, -1, -1):
        # Calcular mês i meses antes do último mês com dado
        mes_offset = ultimo_mes_com_dado.month - i
        ano = ultimo_mes_com_dado.year
        while mes_offset <= 0:
            mes_offset += 12
            ano -= 1
        primeiro_dia = date(ano, mes_offset, 1)
        # Último dia do mês
        if mes_offset == 12:
            ultimo_dia = date(ano + 1, 1, 1) - timedelta(days=1)
        else:
            ultimo_dia = date(ano, mes_offset + 1, 1) - timedelta(days=1)

        labels.append(primeiro_dia.strftime('%b/%Y'))

        # Agregação SQL nativa (sem carregar instâncias de modelos para memória)
        total_entradas = db.session.query(
            db.func.coalesce(db.func.sum(ExtratoNubank.valor), 0.0)
        ).filter(
            ExtratoNubank.data >= primeiro_dia,
            ExtratoNubank.data <= ultimo_dia,
            ExtratoNubank.valor > 0
        ).scalar() or 0.0

        total_saidas = abs(db.session.query(
            db.func.coalesce(db.func.sum(ExtratoNubank.valor), 0.0)
        ).filter(
            ExtratoNubank.data >= primeiro_dia,
            ExtratoNubank.data <= ultimo_dia,
            ExtratoNubank.valor < 0
        ).scalar() or 0.0)

        entradas.append(round(total_entradas, 2))
        saidas.append(round(total_saidas, 2))

    return {"labels": labels, "entradas": entradas, "saidas": saidas}


def obter_distribuicao_gastos():
    """
    Retorna os gastos do último mês do extrato Nubank agrupados por categoria.
    """
    from datetime import date, timedelta

    # Encontrar o último mês que tem dados
    ultima_transacao = ExtratoNubank.query.order_by(ExtratoNubank.data.desc()).first()
    if not ultima_transacao:
        return {"labels": ["Sem dados"], "valores": [0]}

    ultimo_mes = ultima_transacao.data.replace(day=1)
    _, ultimo_dia = divmod(ultimo_mes.month, 12)
    ano = ultimo_mes.year + ultimo_dia // 12
    mes = (ultimo_mes.month % 12) + 1
    fim_mes = date(ano, mes, 1) - timedelta(days=1)

    transacoes = ExtratoNubank.query.filter(
        ExtratoNubank.data >= ultimo_mes,
        ExtratoNubank.data <= fim_mes,
        ExtratoNubank.valor < 0
    ).all()

    # Categorização baseada em palavras-chave da descrição
    CATEGORIAS = {
        'Alimentação': ['MERCADINHO', 'RESTAURANTE', 'BOBS', 'VIVENDA', 'LANCHE', 'PIZZA', 'AÇAÍ', 'CAFÉ', 'BAR', 'LANCHE', 'COMIDA', 'FARMÁCIA', 'DROGARIA', 'PANIFICADORA', 'PADARIA'],
        'Transporte': ['UBER', '99', 'TAXI', 'POSTO', 'COMBUSTÍVEL', 'ESTACIONAMENTO', 'METRÔ', 'ONIBUS', 'ÔNIBUS'],
        'Serviços': ['NETFLIX', 'SPOTIFY', 'INTERNET', 'TELEFONE', 'CELULAR', 'CLARO', 'VIVO', 'TIM', 'AWS', 'HOSTGATOR'],
        'Compras': ['AMAZON', 'MERCADO LIVRE', 'SHOPEE', 'MAGAZINE', 'CASAS BAHIA', 'Americanas'],
        'Saúde': ['HOSPITAL', 'CLÍNICA', 'MÉDICO', 'DENTISTA', 'LABORATÓRIO', 'FARMÁCIA', 'DROGARIA'],
        'Moradia': ['ALUGUEL', 'CONDOMÍNIO', 'IPTU', 'LUZ', 'ÁGUA', 'GÁS', 'IMÓVEL'],
        'Transferências': ['TRANSFERÊNCIA', 'PIX', 'TED', 'DOC', 'ENVIO'],
        'Outros': []
    }

    def categorizar(desc):
        desc_upper = desc.upper()
        for cat, keywords in CATEGORIAS.items():
            for kw in keywords:
                if kw.upper() in desc_upper:
                    return cat
        return 'Outros'

    # Agrupar por categoria
    gastos_categoria = {}
    for t in transacoes:
        cat = categorizar(t.descricao)
        gastos_categoria[cat] = gastos_categoria.get(cat, 0) + abs(t.valor)

    # Ordenar pelos maiores gastos
    gastos_ordenados = sorted(gastos_categoria.items(), key=lambda x: x[1], reverse=True)

    if not gastos_ordenados:
        return {"labels": ["Sem dados"], "valores": [0]}

    mes_nome = ultimo_mes.strftime('%B/%Y')

    return {
        "labels": [item[0] for item in gastos_ordenados],
        "valores": [round(item[1], 2) for item in gastos_ordenados],
        "mes": mes_nome
    }


def obter_progresso_metas():
    """
    Retorna o progresso de todas as metas para o gráfico de barras horizontais.
    """
    metas = Meta.query.order_by(Meta.valor_objetivo.desc()).limit(8).all()

    if not metas:
        return {"labels": [], "atuais": [], "objetivos": []}

    return {
        "labels": [m.nome[:20] for m in metas],
        "atuais": [round(m.valor_atual, 2) for m in metas],
        "objetivos": [round(m.valor_objetivo, 2) for m in metas]
    }


def obter_gastos_por_categoria(mes, ano):
    """
    Retorna os gastos de um mês específico agrupados por categoria.

    Args:
        mes: Mês (1-12)
        ano: Ano (ex: 2024)

    Returns:
        Lista ordenada por total de gastos descendente:
        [
            {'categoria': 'Alimentação', 'total': 1500.00},
            {'categoria': 'Transporte', 'total': 300.00},
            ...
        ]
    """
    # Primeiro e último dia do mês
    primeiro_dia = date(ano, mes, 1)
    ultimo_dia = date(ano, mes, monthrange(ano, mes)[1])

    # Query transações do mês que são gastos (valor < 0)
    transacoes = ExtratoNubank.query.filter(
        ExtratoNubank.data >= primeiro_dia,
        ExtratoNubank.data <= ultimo_dia,
        ExtratoNubank.valor < 0
    ).all()

    # Agrupar por categoria
    gastos_categoria = {}
    for t in transacoes:
        # Usar categoria.nome se existir relationship, senão 'Sem categoria'
        if t.categoria and t.categoria.nome:
            nome_categoria = t.categoria.nome
        else:
            nome_categoria = 'Sem categoria'

        gastos_categoria[nome_categoria] = gastos_categoria.get(nome_categoria, 0) + abs(t.valor)

    # Ordenar pelos maiores gastos (descendente)
    gastos_ordenados = sorted(
        [{'categoria': cat, 'total': round(total, 2)} for cat, total in gastos_categoria.items()],
        key=lambda x: x['total'],
        reverse=True
    )

    return gastos_ordenados
