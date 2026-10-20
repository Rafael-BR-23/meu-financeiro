from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, date
from flask import g, has_request_context
from flask_sqlalchemy.session import Session
import time


class AccountSession(Session):
    def get_bind(self, mapper=None, clause=None, bind=None, **kwargs):
        if bind is None and has_request_context() and getattr(g, 'finance_engine', None) is not None:
            if mapper is None or mapper.local_table.metadata.info.get('bind_key') != 'auth':
                return g.finance_engine
        return super().get_bind(mapper=mapper, clause=clause, bind=bind, **kwargs)

db = SQLAlchemy(session_options={'class_': AccountSession})


class Usuario(db.Model):
    __bind_key__ = 'auth'
    id = db.Column(db.String(32), primary_key=True)
    nome = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(254), nullable=False, unique=True)
    senha_hash = db.Column(db.String(256), nullable=True)
    google_sub = db.Column(db.String(255), nullable=True, unique=True)
    usa_dados_anteriores = db.Column(db.Boolean, nullable=False, default=False)


class VinculoSupabase(db.Model):
    __bind_key__ = 'auth'
    id = db.Column(db.String(36), primary_key=True)
    usuario_id = db.Column(db.String(32), db.ForeignKey('usuario.id'), nullable=False, unique=True)
    senha_definida = db.Column(db.Boolean, nullable=False, default=False)


class SessaoSupabase(db.Model):
    __bind_key__ = 'auth'
    id = db.Column(db.String(32), primary_key=True)
    supabase_id = db.Column(db.String(36), db.ForeignKey('vinculo_supabase.id'), nullable=False, index=True)
    access_token = db.Column(db.Text, nullable=False)
    refresh_token = db.Column(db.Text, nullable=False)
    expires_at = db.Column(db.Float, nullable=False)
    ultimo_acesso = db.Column(db.Float, nullable=False, default=time.time)

class CartaoCredito(db.Model):
    __tablename__ = 'cartao_credito'

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False)        # ex: "Cartão Nubank"
    dia_vencimento = db.Column(db.Integer, nullable=False)  # ex: 10 (dia do mês)
    cor = db.Column(db.String(20), default="#8b5cf6")        # cor visual do cartão

    def to_dict(self):
        return {
            "id": self.id,
            "nome": self.nome,
            "dia_vencimento": self.dia_vencimento,
            "cor": self.cor
        }


class Pendencia(db.Model):
    __tablename__ = 'pendencia'
    
    id = db.Column(db.Integer, primary_key=True)
    credor = db.Column(db.String(200), nullable=False)
    valor = db.Column(db.Float, nullable=False)
    data_vencimento = db.Column(db.Date, nullable=False, index=True)
    data_pagamento = db.Column(db.Date, nullable=True)
    status = db.Column(db.String(20), nullable=False, default="aberto", index=True)  # "aberto", "atrasado", "pago"
    observacao = db.Column(db.Text, nullable=True)
    comprovante_url = db.Column(db.String(500), nullable=True)

    # Relacionamento com Cartão de Crédito e Parcelamento
    cartao_id = db.Column(db.Integer, db.ForeignKey('cartao_credito.id'), nullable=True, index=True)
    cartao = db.relationship('CartaoCredito', backref=db.backref('pendencias', lazy=True, cascade="all, delete-orphan"))
    parcela_atual = db.Column(db.Integer, nullable=True, default=1)
    total_parcelas = db.Column(db.Integer, nullable=True, default=1)
    is_assinatura = db.Column(db.Boolean, nullable=False, default=False)
    assinatura_ativa = db.Column(db.Boolean, nullable=False, default=True)

    def to_dict(self):
        return {
            "id": self.id,
            "credor": self.credor,
            "valor": self.valor,
            "data_vencimento": self.data_vencimento.isoformat() if self.data_vencimento else None,
            "data_pagamento": self.data_pagamento.isoformat() if self.data_pagamento else None,
            "status": self.status,
            "observacao": self.observacao,
            "comprovante_url": self.comprovante_url,
            "cartao_id": self.cartao_id,
            "cartao_nome": self.cartao.nome if self.cartao else None,
            "parcela_atual": self.parcela_atual,
            "total_parcelas": self.total_parcelas,
            "is_assinatura": self.is_assinatura,
            "assinatura_ativa": self.assinatura_ativa
        }


class ExtratoNubank(db.Model):
    __tablename__ = 'extrato_nubank'
    
    id = db.Column(db.Integer, primary_key=True)
    data = db.Column(db.Date, nullable=False, index=True)
    descricao = db.Column(db.String(500), nullable=False)
    valor = db.Column(db.Float, nullable=False, index=True)
    tipo = db.Column(db.String(20), nullable=False)  # "entrada" ou "saida"
    arquivo_original = db.Column(db.String(500), nullable=True)
    conciliado = db.Column(db.Boolean, nullable=False, default=False, index=True)
    categoria_id = db.Column(db.Integer, db.ForeignKey('categoria.id'), nullable=True)
    categoria = db.relationship('Categoria', backref='transacoes', lazy=True)

    def to_dict(self):
        return {
            "id": self.id,
            "data": self.data.isoformat() if self.data else None,
            "descricao": self.descricao,
            "valor": self.valor,
            "tipo": self.tipo,
            "arquivo_original": self.arquivo_original,
            "conciliado": self.conciliado,
            "categoria_id": self.categoria_id
        }


class Categoria(db.Model):
    __tablename__ = 'categoria'

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False)
    credor = db.Column(db.String(200), nullable=False)
    # Restrição unique para evitar duplicidade de credor na mesma categoria
    __table_args__ = (
        db.UniqueConstraint('nome', 'credor', name='uq_categoria_nome_credor'),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "nome": self.nome,
            "credor": self.credor
        }


class Conciliacao(db.Model):
    __tablename__ = 'conciliacao'
    
    id = db.Column(db.Integer, primary_key=True)
    pendencia_id = db.Column(db.Integer, db.ForeignKey('pendencia.id'), nullable=False)
    extrato_id = db.Column(db.Integer, db.ForeignKey('extrato_nubank.id'), nullable=False)
    data_confirmacao = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    status = db.Column(db.String(20), nullable=False, default="sugerido")  # "sugerido", "confirmado", "rejeitado"

    # Relacionamentos para facilitar acesso
    pendencia = db.relationship('Pendencia', backref=db.backref('conciliacoes', lazy=True))
    extrato = db.relationship('ExtratoNubank', backref=db.backref('conciliacoes', lazy=True))

    def to_dict(self):
        return {
            "id": self.id,
            "pendencia_id": self.pendencia_id,
            "extrato_id": self.extrato_id,
            "data_confirmacao": self.data_confirmacao.isoformat() if self.data_confirmacao else None,
            "status": self.status
        }


class Meta(db.Model):
    __tablename__ = 'meta'
    
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(200), nullable=False)
    valor_objetivo = db.Column(db.Float, nullable=False)
    valor_atual = db.Column(db.Float, nullable=False, default=0.0)
    data_limite = db.Column(db.Date, nullable=False)
    data_criacao = db.Column(db.Date, nullable=False, default=date.today)
    prioridade = db.Column(db.String(20), nullable=False, default="media")  # "baixa", "media", "alta"
    status = db.Column(db.String(20), nullable=False, default="em_andamento", index=True)  # "em_andamento", "concluida", "atrasada"
    observacao = db.Column(db.Text, nullable=True)
    icone = db.Column(db.String(50), nullable=False, default="bi-star")

    def to_dict(self):
        return {
            "id": self.id,
            "nome": self.nome,
            "valor_objetivo": self.valor_objetivo,
            "valor_atual": self.valor_atual,
            "data_limite": self.data_limite.isoformat() if self.data_limite else None,
            "data_criacao": self.data_criacao.isoformat() if self.data_criacao else None,
            "prioridade": self.prioridade,
            "status": self.status,
            "observacao": self.observacao,
            "icone": self.icone
        }


class AporteMeta(db.Model):
    __tablename__ = 'aporte_meta'

    id = db.Column(db.Integer, primary_key=True)
    meta_id = db.Column(db.Integer, db.ForeignKey('meta.id'), nullable=False)
    valor = db.Column(db.Float, nullable=False)
    data = db.Column(db.Date, nullable=False, default=date.today)
    origem = db.Column(db.String(100), nullable=True)
    observacao = db.Column(db.Text, nullable=True)

    # Relacionamento para fácil acesso
    meta = db.relationship('Meta', backref=db.backref('aportes', lazy=True, cascade="all, delete-orphan"))

    def to_dict(self):
        return {
            "id": self.id,
            "meta_id": self.meta_id,
            "valor": self.valor,
            "data": self.data.isoformat() if self.data else None,
            "origem": self.origem,
            "observacao": self.observacao
        }


class FechamentoMes(db.Model):
    __tablename__ = 'fechamento_mes'

    id = db.Column(db.Integer, primary_key=True)
    ano = db.Column(db.Integer, nullable=False)
    mes = db.Column(db.Integer, nullable=False)  # 1-12
    saldo_inicial = db.Column(db.Float, nullable=False)
    saldo_final = db.Column(db.Float, nullable=False)
    total_entradas = db.Column(db.Float, nullable=False)
    total_saidas = db.Column(db.Float, nullable=False)
    arquivo_original = db.Column(db.String(500), nullable=True)
    data_importacao = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    # Constraint para não duplicar mês
    __table_args__ = (db.UniqueConstraint('ano', 'mes', name='uq_ano_mes'),)

    def to_dict(self):
        return {
            "id": self.id,
            "ano": self.ano,
            "mes": self.mes,
            "saldo_inicial": self.saldo_inicial,
            "saldo_final": self.saldo_final,
            "total_entradas": self.total_entradas,
            "total_saidas": self.total_saidas,
            "arquivo_original": self.arquivo_original,
            "data_importacao": self.data_importacao.isoformat() if self.data_importacao else None
        }
