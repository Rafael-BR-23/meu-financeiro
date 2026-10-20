from models import db, Pendencia, ExtratoNubank, Conciliacao

def executar_conciliacao_automatica():
    """
    Executa a lógica de Smart Match entre as transações do extrato não conciliadas
    e as pendências financeiras em aberto ou atrasadas.
    """
    # 1. Obter pendências ativas (abertas ou atrasadas)
    pendencias = Pendencia.query.filter(Pendencia.status.in_(['aberto', 'atrasado'])).all()
    
    # 2. Obter transações do extrato não conciliadas
    extratos = ExtratoNubank.query.filter_by(conciliado=False).all()
    
    sugestoes_adicionadas = 0

    for ext in extratos:
        for pend in pendencias:
            # Critério 1: VALOR (diferença absoluta do valor absoluto <= 0.01)
            valor_match = abs(abs(ext.valor) - pend.valor) <= 0.01
            
            # Critério 2: DATA (data do extrato >= data de vencimento da pendência)
            # Geralmente o pagamento ocorre na data de vencimento ou depois
            data_match = ext.data >= pend.data_vencimento
            
            # Critério 3: TEXTO (nome do credor contido na descrição da transação)
            credor_clean = pend.credor.strip().lower()
            desc_clean = ext.descricao.strip().lower()
            texto_match = credor_clean in desc_clean

            # Classificação
            confianca = None
            if valor_match and data_match and texto_match:
                confianca = 'alta'
            elif valor_match and data_match:
                confianca = 'média'
            # Se for apenas valor, consideramos baixa confiança e não sugerimos automaticamente.

            if confianca:
                # Verifica se já existe um registro de conciliação para este par
                existe = Conciliacao.query.filter_by(
                    pendencia_id=pend.id,
                    extrato_id=ext.id
                ).first()

                if not existe:
                    # Cria a sugestão
                    nova_sugestao = Conciliacao(
                        pendencia_id=pend.id,
                        extrato_id=ext.id,
                        status='sugerido'
                    )
                    db.session.add(nova_sugestao)
                    sugestoes_adicionadas += 1

    if sugestoes_adicionadas > 0:
        db.session.commit()

    return sugestoes_adicionadas
