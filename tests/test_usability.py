"""Run in a fresh process: python -m unittest tests.test_usability.

Bind the application to an in-memory database BEFORE SQLAlchemy initialization.
No test data is written to the user's database or uploads.
"""
import io
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch
import tests  # Configure disposable databases before importing the application.
from app import app, db, formatar_moeda
from tests import cliente_autenticado
from models import ExtratoNubank, Categoria, Meta, FechamentoMes, Pendencia
from metas import calcular_sugestao_aporte


class UsabilityTests(unittest.TestCase):
    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        assert db.engine.url.database == ':memory:', 'Tests require an isolated database'
        db.create_all()
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.client = cliente_autenticado(app)
        self.files = tempfile.TemporaryDirectory()
        app.config['UPLOAD_FOLDER'] = self.files.name

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()
        self.files.cleanup()

    def test_pages_render_empty(self):
        for path in ['/', '/metas', '/metas/nova', '/pendencias', '/pendencias/nova', '/transacoes', '/historico', '/importar', '/relatorios', '/categorias']:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_rendered_modals_have_titles_and_named_close_buttons(self):
        from html.parser import HTMLParser
        from models import CartaoCredito
        db.session.add(Pendencia(credor='Sintético', valor=10, data_vencimento=date.today(),
                                 data_pagamento=date.today(), status='pago', comprovante_url='fixture.png'))
        db.session.add(CartaoCredito(nome='Sintético', dia_vencimento=10))
        db.session.commit()
        class Controls(HTMLParser):
            def __init__(self):
                super().__init__(); self.ids = set(); self.titles = []; self.closes = []
            def handle_starttag(self, tag, attributes):
                attrs = dict(attributes)
                if attrs.get('id'): self.ids.add(attrs['id'])
                if 'modal' in attrs.get('class', '').split(): self.titles.append(attrs.get('aria-labelledby'))
                if 'btn-close' in attrs.get('class', '').split(): self.closes.append(attrs.get('aria-label'))
        for path in ('/', '/historico', '/pendencias', '/metas', '/transacoes'):
            with self.subTest(path=path):
                parser = Controls(); parser.feed(self.client.get(path).get_data(as_text=True))
                self.assertTrue(parser.titles)
                self.assertTrue(all(title in parser.ids for title in parser.titles))
                self.assertEqual(len(parser.titles), len(set(parser.titles)))
                self.assertTrue(all(label == 'Fechar' for label in parser.closes))

    def test_search_category_totals_and_sort_preserve_filters(self):
        category = Categoria(nome='Alimentação', credor='Mercado')
        db.session.add(category)
        db.session.flush()
        db.session.add_all([
            ExtratoNubank(data=date(2026, 9, 1), descricao='Mercado teste', valor=-1234.56, tipo='saida', categoria_id=category.id),
            ExtratoNubank(data=date(2026, 9, 2), descricao='Outra compra', valor=-50, tipo='saida'),
        ])
        db.session.commit()
        response = self.client.get(f'/transacoes?q=Mercado&categoria={category.id}&tipo=saida&mes=2026-09')
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('Mercado teste', html)
        self.assertNotIn('Outra compra', html)
        self.assertIn('1.234,56', html)
        self.assertIn('sort=valor', html)
        self.assertIn('q=Mercado', html)
        self.assertIn('01/09/2026', html)
        self.assertNotIn('Mercado teste', self.client.get('/transacoes?q=%25').get_data(as_text=True))

    def test_goal_suggestion_and_currency(self):
        meta = Meta(nome='Viagem', valor_objetivo=1000, valor_atual=100, data_limite=date.today()+timedelta(days=10))
        self.assertEqual(calcular_sugestao_aporte(meta)['mensal'], 900)
        self.assertEqual(formatar_moeda(-1234.56), '-1.234,56')
        db.session.add(meta)
        db.session.commit()
        html = self.client.get('/metas').get_data(as_text=True)
        self.assertIn('Adicionar valor', html)
        self.assertIn('900,00 / mês', html)

    def test_dashboard_uses_statement_balance_not_history_sum(self):
        db.session.add(ExtratoNubank(data=date(2026, 1, 1), descricao='Entrada antiga', valor=59079.08, tipo='entrada'))
        db.session.commit()
        self.assertIn('Resultado das transações', self.client.get('/').get_data(as_text=True))
        db.session.add_all([
            FechamentoMes(ano=2026, mes=6, saldo_inicial=962.33, saldo_final=916.73, total_entradas=0, total_saidas=0),
            FechamentoMes(ano=2026, mes=8, saldo_inicial=998.07, saldo_final=705.57, total_entradas=9989.04, total_saidas=10281.54),
        ])
        db.session.commit()
        html = self.client.get('/').get_data(as_text=True)
        self.assertIn('Saldo do último extrato', html)
        self.assertIn('Fechamento de 08/2026', html)
        self.assertIn('R$ 705,57', html)

    def test_dashboard_projection_and_upcoming_payments(self):
        today = date.today()
        bills = [
            Pendencia(credor='Conta distante', valor=100, data_vencimento=today + timedelta(days=8), status='aberto'),
            Pendencia(credor='Conta em sete dias', valor=200, data_vencimento=today + timedelta(days=7), status='aberto'),
            Pendencia(credor='Conta vencida', valor=50, data_vencimento=today - timedelta(days=1), status='aberto'),
            Pendencia(credor='Conta quitada', valor=900, data_vencimento=today, status='pago'),
        ]
        db.session.add_all(bills)
        db.session.commit()
        html = self.client.get('/').get_data(as_text=True)
        self.assertIn('Importe um extrato com saldo final', html)
        db.session.add(FechamentoMes(ano=2026, mes=8, saldo_inicial=0,
                                    saldo_final=300, total_entradas=300, total_saidas=0))
        db.session.commit()
        html = self.client.get('/').get_data(as_text=True)
        self.assertIn('R$ -50,00', html)
        upcoming = html.split('id="proximos-vencimentos"', 1)[1].split('</section>', 1)[0]
        self.assertLess(upcoming.index('Conta vencida'), upcoming.index('Conta em sete dias'))
        self.assertNotIn('Conta distante', upcoming)
        self.assertNotIn('Conta quitada', upcoming)
        self.assertIn('Registrar pagamento', upcoming)
        self.assertLess(html.index('id="proximos-vencimentos"'), html.index('id="graficoEvolucao"'))
        response = self.client.post(f'/pendencias/pagar/{bills[2].id}',
                                    data={'data_pagamento': today.isoformat()}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(db.session.get(Pendencia, bills[2].id).status, 'pago')
        self.assertIn('Concluído:', response.get_data(as_text=True))
        self.assertIn('role="status" aria-live="polite"', response.get_data(as_text=True))
        self.assertIn('R$ 0,00', self.client.get('/').get_data(as_text=True))

    def test_import_reports_duplicates_and_empty_file(self):
        transaction = dict(data=date(2026, 9, 1), descricao='Compra de teste', valor=-10, tipo='saida')
        csv_valido = b'Data,Valor,Identificador,Descricao\n01/09/2026,-10,id,Compra de teste\n'
        executar_direto = lambda func, args, deadline: func(*args)
        with patch('app.executar_cancelavel', side_effect=executar_direto), \
             patch('app.importar_csv', return_value=[transaction, transaction]), \
             patch('app.executar_conciliacao_automatica', return_value=0):
            response = self.client.post('/importar', data={'extrato': (io.BytesIO(csv_valido), 'test.csv')}, content_type='multipart/form-data', follow_redirects=True)
        html = response.get_data(as_text=True)
        self.assertIn('1 adicionadas', html)
        self.assertIn('1 identificadas como duplicadas', html)
        self.assertEqual(ExtratoNubank.query.count(), 1)
        with patch('app.executar_cancelavel', side_effect=executar_direto), \
             patch('app.importar_csv', return_value=[]), \
             patch('app.executar_conciliacao_automatica', return_value=0):
            response = self.client.post('/importar', data={'extrato': (io.BytesIO(csv_valido), 'empty.csv')}, content_type='multipart/form-data', follow_redirects=True)
        self.assertIn('Nenhuma transação foi identificada', response.get_data(as_text=True))

    def test_semantic_controls_labels_and_live_regions(self):
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        base = (root / 'templates' / 'base.html').read_text(encoding='utf-8')
        transacoes = (root / 'templates' / 'transacoes.html').read_text(encoding='utf-8')
        nova_pendencia = (root / 'templates' / 'nova_pendencia.html').read_text(encoding='utf-8')
        usability = (root / 'static' / 'usability.js').read_text(encoding='utf-8')

        # Live region in base
        self.assertIn('id="async-status"', base)
        self.assertIn('href="#conteudo"', base)
        self.assertIn('id="conteudo" tabindex="-1"', base)
        self.assertIn('role="status"', base)
        self.assertIn('aria-live="polite"', base)

        # Tab keyboard navigation in usability.js
        self.assertIn("event.key === 'ArrowRight'", usability)
        self.assertIn("event.key === 'ArrowLeft'", usability)

        # Transaction filter date inputs with explicit IDs and matching label for
        self.assertIn('id="filtro-data-inicio"', transacoes)
        self.assertIn('for="filtro-data-inicio"', transacoes)
        self.assertIn('id="filtro-data-fim"', transacoes)
        self.assertIn('for="filtro-data-fim"', transacoes)

        # Tab ARIA contracts in nova_pendencia
        self.assertIn('id="tab-credito"', nova_pendencia)
        self.assertIn('role="tab"', nova_pendencia)
        self.assertIn('aria-controls="credito"', nova_pendencia)
        self.assertIn('aria-selected=', nova_pendencia)
        self.assertIn('role="tabpanel"', nova_pendencia)
        self.assertIn('aria-labelledby="tab-credito"', nova_pendencia)


if __name__ == '__main__':
    unittest.main()
