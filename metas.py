from datetime import date
from models import Meta

def atualizar_status_metas():
    """Atualiza o status das metas com base no progresso e na data limite."""
    hoje = date.today()
    metas = Meta.query.filter(Meta.status.in_(['em_andamento', 'atrasada'])).all()
    
    from models import db
    alterado = False
    for meta in metas:
        if meta.valor_atual >= meta.valor_objetivo:
            meta.status = 'concluida'
            alterado = True
        elif meta.data_limite < hoje and meta.status != 'concluida':
            meta.status = 'atrasada'
            alterado = True
        elif meta.data_limite >= hoje and meta.status == 'atrasada':
            meta.status = 'em_andamento'
            alterado = True
    
    if alterado:
        db.session.commit()


def calcular_progresso(meta):
    """Calcula o percentual de progresso da meta."""
    if meta.valor_objetivo <= 0:
        return 100.0
    pct = (meta.valor_atual / meta.valor_objetivo) * 100
    return min(round(pct, 1), 100.0)


def calcular_sugestao_aporte(meta):
    """
    Retorna uma mensagem formatada de sugestão de aporte para a meta.
    """
    hoje = date.today()
    faltante = max(meta.valor_objetivo - meta.valor_atual, 0)
    dias_restantes = (meta.data_limite - hoje).days

    if faltante <= 0:
        return None  # Meta já atingida

    if dias_restantes <= 0:
        return {
            "tipo": "urgente",
            "mensagem": "O prazo chegou. Adicione um valor ou ajuste a data para continuar seu plano."
        }

    por_dia = faltante / dias_restantes
    por_mes = por_dia * 30

    return {
        "tipo": "sugestao",
        "mensal": min(por_mes, faltante),
        "dias_restantes": dias_restantes,
        "mensagem": "Sugestão proporcional ao tempo restante, considerando meses de 30 dias."
    }


def obter_cor_prioridade(prioridade):
    """Retorna a cor RGBA baseada na prioridade."""
    cores = {
        "alta": "rgba(220,53,69,0.85)",
        "media": "rgba(255,193,7,0.85)",
        "baixa": "rgba(23,162,184,0.85)"
    }
    return cores.get(prioridade, "rgba(108,117,125,0.85)")


def obter_cor_status(status):
    """Retorna a classe de cor Bootstrap baseada no status."""
    cores = {
        "em_andamento": "primary",
        "concluida": "success",
        "atrasada": "danger"
    }
    return cores.get(status, "secondary")
