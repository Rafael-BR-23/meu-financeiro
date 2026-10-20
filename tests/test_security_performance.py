import os
import unittest
import io
import re
from datetime import date
from unittest.mock import patch
import tests  # Configure disposable databases before importing the application.
from app import app, db, gerar_nome_arquivo_seguro, production_config_from_env
from tests import cliente_autenticado
from models import Pendencia, ExtratoNubank, Meta, CartaoCredito, Usuario
from werkzeug.security import generate_password_hash

class TestSecurityAndPerformance(unittest.TestCase):
    def test_production_rejects_process_local_rate_limit_storage(self):
        from flask import Flask
        from autenticacao import criar_limiter
        for uri in ('', 'memory://'):
            probe = Flask(__name__)
            probe.config['IS_PRODUCTION'] = True
            with patch.dict(os.environ, {'RATELIMIT_STORAGE_URI': uri}):
                with self.assertRaisesRegex(RuntimeError, 'RATELIMIT_STORAGE_URI'):
                    criar_limiter(probe)

    @classmethod
    def setUpClass(cls):
        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = False
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        cls.client = cliente_autenticado(app)
        with app.app_context():
            if db.engine.url.database != ':memory:':
                raise RuntimeError('Tests must never write to the real database')
            db.create_all()

    @classmethod
    def tearDownClass(cls):
        with app.app_context():
            db.session.remove()
            db.drop_all()

    def test_security_headers_present(self):
        """Verifica se os headers de segurança HTTP estão presentes nas respostas."""
        response = self.client.get('/')
        self.assertIn('X-Content-Type-Options', response.headers)
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        self.assertIn('X-Frame-Options', response.headers)
        self.assertEqual(response.headers['X-Frame-Options'], 'SAMEORIGIN')
        self.assertIn('Referrer-Policy', response.headers)
        self.assertEqual(response.headers['Referrer-Policy'], 'strict-origin-when-cross-origin')
        self.assertIn('Permissions-Policy', response.headers)

    def test_session_cookie_security_config(self):
        """Verifica se as diretivas de cookies de sessão estão configuradas com segurança."""
        self.assertTrue(app.config.get('SESSION_COOKIE_HTTPONLY'))
        self.assertEqual(app.config.get('SESSION_COOKIE_SAMESITE'), 'Lax')

    def test_gerar_nome_arquivo_seguro(self):
        """Verifica se a função de nomeação segura gera nomes únicos e sanitizados com extensão correta."""
        nome1 = gerar_nome_arquivo_seguro("comprovante", "minha foto / com espaco.jpg")
        nome2 = gerar_nome_arquivo_seguro("comprovante", "minha foto / com espaco.jpg")
        
        self.assertTrue(nome1.startswith("comprovante_"))
        self.assertTrue(nome1.endswith(".jpg"))
        self.assertNotIn("/", nome1)
        self.assertNotIn("\\", nome1)
        self.assertNotIn(" ", nome1)
        # Nomes devem ser únicos mesmo para o mesmo arquivo original
        self.assertNotEqual(nome1, nome2)

    def test_models_indexes_defined(self):
        """Verifica se os índices de performance estão definidos nas colunas mais consultadas."""
        self.assertTrue(Pendencia.status.property.columns[0].index)
        self.assertTrue(Pendencia.data_vencimento.property.columns[0].index)
        self.assertTrue(Pendencia.cartao_id.property.columns[0].index)
        self.assertTrue(ExtratoNubank.data.property.columns[0].index)
        self.assertTrue(ExtratoNubank.valor.property.columns[0].index)
        self.assertTrue(ExtratoNubank.conciliado.property.columns[0].index)
        self.assertTrue(Meta.status.property.columns[0].index)

    def test_dashboard_sql_aggregation(self):
        """Verifica se o dashboard agrega saldo e despesas corretamente com SQL nativo."""
        with app.app_context():
            t1 = ExtratoNubank(data=date(2026, 9, 1), descricao="Salário", valor=5000.0, tipo="entrada")
            t2 = ExtratoNubank(data=date(2026, 9, 5), descricao="Mercado", valor=-350.0, tipo="saida")
            db.session.add_all([t1, t2])
            db.session.commit()

        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)

    def test_production_config_requires_real_secret_https_secure_cookie_and_oauth_redirect(self):
        valid = {'APP_ENV': 'production', 'SECRET_KEY': 'x' * 32,
                 'APP_PUBLIC_URL': 'https://financeiro.example',
                 'GOOGLE_REDIRECT_URI': 'https://financeiro.example/auth/google/callback',
                 'SESSION_COOKIE_SECURE': '1'}
        config = production_config_from_env(valid)
        self.assertTrue(config['SESSION_COOKIE_SECURE'])
        self.assertEqual(config['APP_PUBLIC_URL'], 'https://financeiro.example')
        for change in ({'SECRET_KEY': ''}, {'SECRET_KEY': 'dev-secret-key-12345'},
                       {'APP_PUBLIC_URL': 'http://financeiro.example'},
                       {'GOOGLE_REDIRECT_URI': 'http://financeiro.example/auth/google/callback'},
                       {'SESSION_COOKIE_SECURE': '0'}):
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                production_config_from_env({**valid, **change})

    def test_development_keeps_generated_secret_and_insecure_local_cookie(self):
        config = production_config_from_env({'APP_ENV': 'development'})
        self.assertFalse(config['IS_PRODUCTION'])
        self.assertFalse(config['SESSION_COOKIE_SECURE'])

    def test_production_login_sets_secure_cookie_attributes(self):
        antiga_config = dict(app.config)
        try:
            app.config.update(
                SESSION_COOKIE_SECURE=True,
                SESSION_COOKIE_HTTPONLY=True,
                SESSION_COOKIE_SAMESITE='Lax',
                TESTING=True,
                WTF_CSRF_ENABLED=False
            )
            with app.app_context():
                user = db.session.get(Usuario, 'prod-user')
                if not user:
                    user = Usuario(id='prod-user', nome='Prod User', email='prod@example.com',
                                   senha_hash=generate_password_hash('senha-123'), usa_dados_anteriores=False)
                    db.session.add(user)
                    db.session.commit()
            client = app.test_client()
            res = client.post('/login', data={'email': 'prod@example.com', 'senha': 'senha-123'},
                              base_url='https://financeiro.example')
            self.assertEqual(res.status_code, 302)
            cookies = res.headers.getlist('Set-Cookie')
            session_cookie = next((c for c in cookies if c.startswith('session=')), None)
            self.assertIsNotNone(session_cookie)
            self.assertIn('Secure', session_cookie)
            self.assertIn('HttpOnly', session_cookie)
            self.assertIn('SameSite=Lax', session_cookie)
        finally:
            app.config.clear()
            app.config.update(antiga_config)


    def test_headers_csp_report_only_hsts_gate_and_private_no_store(self):
        original = {key: app.config.get(key) for key in ('ENABLE_HSTS', 'TRUSTED_PROXY')}
        try:
            app.config.update(ENABLE_HSTS=False, TRUSTED_PROXY='')
            response = self.client.get('/', base_url='https://financeiro.example')
            self.assertIn('Content-Security-Policy-Report-Only', response.headers)
            self.assertNotIn('Strict-Transport-Security', response.headers)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            app.config['ENABLE_HSTS'] = True
            response = self.client.get('/', base_url='https://financeiro.example')
            self.assertEqual(response.headers['Strict-Transport-Security'],
                             'max-age=31536000; includeSubDomains')
            self.assertNotIn('Strict-Transport-Security',
                             self.client.get('/', base_url='http://financeiro.example').headers)
        finally:
            app.config.update(original)

    def test_untrusted_client_forwarded_headers_are_ignored(self):
        from app import TrustedProxyMiddleware
        from werkzeug.test import Client
        from werkzeug.wrappers import Request, Response
        import json

        @Request.application
        def probe(request):
            return Response(json.dumps({'remote_addr': request.remote_addr,
                                        'scheme': request.scheme, 'host': request.host}),
                            mimetype='application/json')
        client = Client(TrustedProxyMiddleware(probe, app), Response)
        original = {key: app.config.get(key) for key in ('TRUSTED_PROXY', 'TRUSTED_PROXY_NETWORKS')}
        try:
            app.config['TRUSTED_PROXY'] = '1'
            app.config['TRUSTED_PROXY_NETWORKS'] = ('10.0.0.10/32',)
            # Requisição de peer não confiável com headers forjados
            res_untrusted = client.get('/', base_url='http://financeiro.example', headers={
                'X-Forwarded-For': '198.51.100.1',
                'X-Forwarded-Proto': 'https',
                'X-Forwarded-Host': 'attacker.example'
            }, environ_base={'REMOTE_ADDR': '203.0.113.20'})
            data_untrusted = res_untrusted.get_json()
            self.assertEqual(data_untrusted['remote_addr'], '203.0.113.20')
            self.assertEqual(data_untrusted['scheme'], 'http')
            self.assertEqual(data_untrusted['host'], 'financeiro.example')

            # Requisição de peer confiável (10.0.0.10)
            res_trusted = client.get('/', base_url='http://financeiro.example', headers={
                'X-Forwarded-For': '198.51.100.1',
                'X-Forwarded-Proto': 'https',
                'X-Forwarded-Host': 'attacker.example'
            }, environ_base={'REMOTE_ADDR': '10.0.0.10'})
            data_trusted = res_trusted.get_json()
            self.assertEqual(data_trusted['remote_addr'], '198.51.100.1')
            self.assertEqual(data_trusted['scheme'], 'https')
            self.assertEqual(data_trusted['host'], 'attacker.example')
        finally:
            app.config.update(original)

    def test_unexpected_import_error_is_sanitized_in_ui_and_log(self):
        marker = 'token-super-secreto-documento-123'
        with patch('app.importar_csv', side_effect=RuntimeError(marker)), \
             self.assertLogs(app.logger, level='ERROR') as captured:
            response = self.client.post('/importar', data={
                'extrato': (io.BytesIO(b'Data,Valor,Id,Descricao\n'), 'teste.csv')
            }, content_type='multipart/form-data', follow_redirects=True)
        page = response.get_data(as_text=True)
        log = '\n'.join(captured.output)
        self.assertNotIn(marker, page)
        self.assertNotIn(marker, log)
        self.assertIn('Não foi possível processar o arquivo', page)
        correlation = re.search(r'Referência: ([0-9a-f]{12})', page).group(1)
        self.assertIn('Falha ao processar importação', log)
        self.assertIn(f'correlation_id={correlation}', log)
        self.assertIn('Traceback (most recent call last)', log)

if __name__ == '__main__':
    unittest.main()
