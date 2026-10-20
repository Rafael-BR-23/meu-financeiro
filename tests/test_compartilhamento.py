import io
import os
import re
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from flask import g
from PIL import Image
from werkzeug.datastructures import FileStorage
import tests  # Configure disposable databases before importing the application.
from app import app, db, pasta_temporarios, limpar_temporarios_expirados, reservar_temporario
from tests import cliente_autenticado
from models import Pendencia
from importador import DocumentValidationError
from leitor_comprovante import processar_texto_comprovante


class CompartilhamentoTest(unittest.TestCase):
    def setUp(self):
        self.uploads = tempfile.TemporaryDirectory()
        self.config = {key: app.config[key] for key in ('TESTING', 'UPLOAD_FOLDER', 'WTF_CSRF_ENABLED')}
        app.config.update(TESTING=True, UPLOAD_FOLDER=self.uploads.name, WTF_CSRF_ENABLED=True)
        self.client = cliente_autenticado(app)
        with app.app_context():
            assert db.engine.url.database == ':memory:', 'Use python -m unittest tests.test_compartilhamento'
            db.create_all()
        from app import share_limiter
        share_limiter.storage.reset()
        image = io.BytesIO()
        Image.new('RGB', (20, 20), 'white').save(image, format='PNG')
        self.image = image.getvalue()

    def tearDown(self):
        with app.app_context():
            db.session.remove()
            db.drop_all()
        app.config.update(self.config)
        self.uploads.cleanup()

    def share(self, name, **mock_options):
        executar_direto = lambda func, args, deadline: func(*args)
        with patch('app.executar_cancelavel', side_effect=executar_direto), \
             patch('app.extrair_dados_comprovante', **mock_options):
            return self.client.post('/compartilhar-comprovante', data={
                'comprovante': (io.BytesIO(self.image), name, 'application/octet-stream')})

    def test_shared_image_prefills_and_saves_original_attachment(self):
        for name in ('comprovante.png', 'comprovante', 'imagem.bin'):
            with self.subTest(name=name):
                response = self.share(name, return_value={
                    'valor': 85.32, 'data': date(2026, 9, 25), 'destinatario': 'Teste compartilhamento'})
                self.assertEqual(response.status_code, 303)
                params = parse_qs(urlsplit(response.location).query)
                filename = params['comprovante_temp'][0]
                pending = list(Path(self.uploads.name, 'temporarios').glob(f'pending-{filename}.*'))
                self.assertEqual(len(pending), 1)
                self.assertEqual(pending[0].read_bytes(), self.image)
                page = self.client.get(response.location).get_data(as_text=True)
                self.assertIn('value="85.32"', page)
                self.assertIn('value="Teste compartilhamento"', page)
                self.assertIn('value="2026-09-25"', page)
                self.assertIn(f'name="comprovante_temp" value="{filename}"', page)
                token = re.search(r'name="csrf_token" value="([^"]+)"', page)[1]
                saved = self.client.post('/pendencias/nova', data={
                    'csrf_token': token, 'tipo_lancamento': 'pago', 'credor': 'Teste compartilhamento',
                    'valor': '85.32', 'data_pagamento': '2026-09-25', 'comprovante_temp': filename})
                self.assertTrue(saved.location.endswith('/historico'))
                with app.app_context():
                    row = Pendencia.query.order_by(Pendencia.id.desc()).first()
                    self.assertNotEqual(row.comprovante_url, filename)
                    self.assertEqual(Path(self.uploads.name, row.comprovante_url).read_bytes(), self.image)
                    self.assertEqual(row.status, 'pago')
                    self.assertEqual(row.valor, 85.32)

    def test_no_file_or_invalid_content_does_not_claim_success(self):
        for data in ({'text': 'Um link'}, {'comprovante': (io.BytesIO(b'invalid'), 'blob', 'image/png')}):
            response = self.client.post('/compartilhar-comprovante', data=data, follow_redirects=True)
            self.assertNotIn('Comprovante Recebido!', response.get_data(as_text=True))
            self.assertNotIn('name="comprovante_temp"', response.get_data(as_text=True))
        self.assertEqual(list(Path(self.uploads.name).iterdir()), [])

    def test_share_with_only_title_shows_missing_file_warning(self):
        response = self.client.post('/compartilhar-comprovante',
                                    data={'title': 'Comprovante_20260926.png'}, follow_redirects=True)
        self.assertEqual(len(response.history), 1)
        page = response.get_data(as_text=True)
        self.assertIn('Nenhum arquivo foi recebido.', page)
        self.assertIn('id="pago" role="tabpanel"', page)
        self.assertNotIn('name="comprovante_temp"', page)

    def test_ocr_failure_remove_attachment_and_does_not_return_partial_data(self):
        response = self.share(
            'blob', side_effect=DocumentValidationError(
                'Não foi possível processar o documento.'))
        self.assertEqual(response.status_code, 303)
        self.assertNotIn('comprovante_temp', parse_qs(urlsplit(response.location).query))
        self.assertEqual(list(Path(self.uploads.name).iterdir()), [])

    def test_fatura_uses_document_description_without_mistaking_payer_for_recipient(self):
        for documento in ('Documento Fatura do cartão Nubank', 'Documento:\nFatura do cartão Nubank'):
            with self.subTest(documento=documento):
                data = processar_texto_comprovante(
                    'Comprovante de pagamento\n26 SET 2026\nValor R$ 100,00\n'
                    + documento + '\nPagador PESSOA TESTE\nEstamos aqui para ajudar',
                    {'destinatario': '', 'descricao': ''})
                self.assertEqual(data['destinatario'], '')
                self.assertEqual(data['descricao'], 'Fatura do cartão Nubank')
                response = self.share('fatura.png', return_value=data)
                params = parse_qs(urlsplit(response.location).query)
                self.assertEqual(params['credor'], ['Fatura do cartão Nubank'])
                self.assertIn('comprovante_temp', params)

        data = processar_texto_comprovante('Documento\nPagador PESSOA TESTE',
                                          {'destinatario': '', 'descricao': ''})
        self.assertEqual(data['descricao'], '')

    def test_valor_extraido_nao_preenche_descricao_do_formulario(self):
        for final, nome in (('', ''), ('\nFavorecido: Loja Exemplo', 'Loja Exemplo')):
            with self.subTest(nome=nome):
                dados = processar_texto_comprovante(
                    'Comprovante Pix\nDestinatário\nR$ 85,32\nValor R$ 85,32' + final,
                    {'destinatario': '', 'descricao': ''})
                resposta = self.share('sintetico.png', return_value=dados)
                params = parse_qs(urlsplit(resposta.location).query, keep_blank_values=True)
                self.assertEqual(params['credor'], [nome])
                self.assertEqual(params['valor'], ['85.32'])
                pagina = self.client.get(resposta.location).get_data(as_text=True)
                self.assertIn('name="credor" required placeholder="Ex: Supermercado, Farmácia, Nome Pix" value="' + nome + '"', pagina)
                self.assertIn('name="valor" required placeholder="0,00" value="85.32"', pagina)

    def test_missing_or_outside_attachment_does_not_save_without_proof(self):
        page = self.client.get('/pendencias/nova?tipo=pago').get_data(as_text=True)
        token = re.search(r'name="csrf_token" value="([^"]+)"', page)[1]
        for filename in ('missing.png', '../app.py'):
            response = self.client.post('/pendencias/nova', data={
                'csrf_token': token, 'tipo_lancamento': 'pago', 'credor': 'Teste',
                'valor': '85.32', 'data_pagamento': '2026-09-25', 'comprovante_temp': filename})
            self.assertIn('/pendencias/nova?', response.location)
        with app.app_context():
            self.assertEqual(Pendencia.query.count(), 0)

    def test_pending_share_is_single_use_and_logout_removes_it(self):
        response = self.share('teste.png', return_value={'valor': 10, 'data': date(2026, 10, 20), 'destinatario': 'Teste'})
        pending_id = parse_qs(urlsplit(response.location).query)['comprovante_temp'][0]
        page = self.client.get(response.location).get_data(as_text=True)
        token = re.search(r'name="csrf_token" value="([^"]+)"', page)[1]
        payload = {'csrf_token': token, 'tipo_lancamento': 'pago', 'credor': 'Teste',
                   'valor': '10', 'data_pagamento': '2026-10-20', 'comprovante_temp': pending_id}
        self.client.post('/pendencias/nova', data=payload)
        replay = self.client.post('/pendencias/nova', data=payload)
        self.assertIn('/pendencias/nova?', replay.location)
        with app.app_context():
            self.assertEqual(Pendencia.query.count(), 1)
        self.share('teste.png', return_value={'valor': 10, 'data': date(2026, 10, 20), 'destinatario': 'Teste'})
        pending = Path(self.uploads.name, 'temporarios')
        self.assertTrue(list(pending.glob('pending-*')))
        self.client.post('/logout', data={'csrf_token': token})
        self.assertFalse(list(pending.glob('pending-*')))

    def test_opening_new_form_cancels_pending_share(self):
        self.share('teste.png', return_value={'valor': 10, 'data': date(2026, 10, 20), 'destinatario': 'Teste'})
        self.assertTrue(list(Path(self.uploads.name, 'temporarios').glob('pending-*')))
        response = self.client.get('/pendencias/nova')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(Path(self.uploads.name).iterdir()), [])
        with self.client.session_transaction() as current:
            self.assertNotIn('pending_share_id', current)

    def test_invalid_normal_upload_does_not_change_payment_or_old_attachment(self):
        original = Path(self.uploads.name, 'original.png')
        original.write_bytes(self.image)
        with app.app_context():
            bill = Pendencia(credor='Sintético', valor=10, data_vencimento=date.today(), status='aberto', comprovante_url='original.png')
            db.session.add(bill); db.session.commit(); bill_id = bill.id
        page = self.client.get('/pendencias/nova').get_data(as_text=True)
        token = re.search(r'name="csrf_token" value="([^"]+)"', page)[1]
        for route in (f'/pendencias/pagar/{bill_id}', '/pendencias/nova', f'/historico/editar/{bill_id}'):
            response = self.client.post(route, data={'csrf_token': token, 'tipo_lancamento': 'pago',
                'credor': 'Teste', 'valor': '10', 'data_pagamento': '2026-10-20',
                'comprovante': (io.BytesIO(b'nao e imagem'), 'falso.png')})
            self.assertEqual(response.status_code, 303)
        with app.app_context():
            self.assertEqual(db.session.get(Pendencia, bill_id).status, 'aberto')
            self.assertEqual(Pendencia.query.count(), 1)
        self.assertEqual(list(Path(self.uploads.name).iterdir()), [original])
        self.assertEqual(original.read_bytes(), self.image)

    def test_failed_payment_commit_removes_new_attachment(self):
        with app.app_context():
            bill = Pendencia(credor='Sintético', valor=10, data_vencimento=date.today(), status='aberto')
            db.session.add(bill); db.session.commit(); bill_id = bill.id
        page = self.client.get('/pendencias/nova').get_data(as_text=True)
        token = re.search(r'name="csrf_token" value="([^"]+)"', page)[1]
        with patch('app.executar_cancelavel', side_effect=lambda func, args, deadline: func(*args)), \
             patch('app.db.session.commit', side_effect=RuntimeError('synthetic failure')):
            response = self.client.post(f'/pendencias/pagar/{bill_id}', data={
                'csrf_token': token, 'data_pagamento': date.today().isoformat(),
                'comprovante': (io.BytesIO(self.image), 'sintetico.png')})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(Path(self.uploads.name).iterdir()), [])
        with app.app_context():
            self.assertEqual(db.session.get(Pendencia, bill_id).status, 'aberto')



class TemporariosTest(unittest.TestCase):

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.config_backup = {k: app.config[k] for k in ('TESTING', 'UPLOAD_FOLDER', 'WTF_CSRF_ENABLED',
                                                           'MAX_ACCOUNT_TEMP_BYTES', 'TEMP_MAX_AGE_SECONDS')}
        app.config.update(TESTING=True, UPLOAD_FOLDER=self.folder,
                          WTF_CSRF_ENABLED=False,
                          MAX_ACCOUNT_TEMP_BYTES=50 * 1024 * 1024,
                          TEMP_MAX_AGE_SECONDS=3600)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.folder, ignore_errors=True)
        app.config.update(self.config_backup)

    def test_limpeza_expirada_remove_arquivo_antigo(self):
        with app.test_request_context('/'):
            g.uploads_folder = self.folder
            temp_dir = Path(pasta_temporarios())
            temp_dir.mkdir(parents=True, exist_ok=True)
            old = temp_dir / 'antigo.bin'
            old.write_bytes(b'x')
            os.utime(old, (100, 100))
            removed = limpar_temporarios_expirados(now=3701)
            self.assertEqual(removed, 1)
            self.assertFalse(old.exists())

    def test_limpeza_preserva_arquivo_recente(self):
        with app.test_request_context('/'):
            g.uploads_folder = self.folder
            temp_dir = Path(pasta_temporarios())
            temp_dir.mkdir(parents=True, exist_ok=True)
            recente = temp_dir / 'recente.bin'
            recente.write_bytes(b'y')
            now = recente.stat().st_mtime + 100
            removed = limpar_temporarios_expirados(now=now)
            self.assertEqual(removed, 0)
            self.assertTrue(recente.exists())

    def test_quota_excedida_levanta_erro(self):
        with app.test_request_context('/'):
            g.uploads_folder = self.folder
            app.config['MAX_ACCOUNT_TEMP_BYTES'] = 4
            upload = FileStorage(stream=io.BytesIO(b'12345'), filename='x.png',
                                 content_type='image/png')
            from importador import DocumentValidationError
            with self.assertRaisesRegex(DocumentValidationError, '50 MB'):
                reservar_temporario(upload, 'comp')

    def test_reservar_temporario_salva_arquivo(self):
        with app.test_request_context('/'):
            g.uploads_folder = self.folder
            conteudo = b'PNGfake'
            upload = FileStorage(stream=io.BytesIO(conteudo), filename='foto.png',
                                 content_type='image/png')
            path = reservar_temporario(upload, 'comp')
            self.assertTrue(Path(path).exists())
            self.assertEqual(Path(path).read_bytes(), conteudo)
            # Deve estar dentro de temporarios/
            self.assertIn('temporarios', path)



class ShareTargetAuthLimitTest(unittest.TestCase):

    def setUp(self):
        self.uploads = tempfile.TemporaryDirectory()
        self.config_backup = {k: app.config[k] for k in ('TESTING', 'UPLOAD_FOLDER', 'WTF_CSRF_ENABLED')}
        app.config.update(TESTING=True, UPLOAD_FOLDER=self.uploads.name, WTF_CSRF_ENABLED=False)
        self.client = cliente_autenticado(app)
        with app.app_context():
            db.create_all()
        self.image_bytes = self._make_png()

    def tearDown(self):
        with app.app_context():
            db.session.remove()
            db.drop_all()
        app.config.update(self.config_backup)
        self.uploads.cleanup()
        # Limpa contadores de rate limit entre testes
        from app import share_limiter
        share_limiter.storage.reset()

    @staticmethod
    def _make_png():
        buf = io.BytesIO()
        Image.new('RGB', (10, 10), 'green').save(buf, format='PNG')
        return buf.getvalue()

    def _post_share(self, client, filename='comp.png'):
        return client.post('/compartilhar-comprovante',
                           data={'comprovante': (io.BytesIO(self.image_bytes), filename,
                                                  'application/octet-stream')})

    def test_anonimo_nao_pode_postar(self):
        anon = app.test_client()
        resp = anon.post('/compartilhar-comprovante',
                         data={'comprovante': (io.BytesIO(self.image_bytes), 'c.png',
                                               'application/octet-stream')})
        self.assertIn(resp.status_code, (401, 302))

    def test_11_tentativas_retornam_429(self):
        from app import share_limiter
        share_limiter.storage.reset()
        executar_direto = lambda func, args, deadline: func(*args)
        with patch('app.executar_cancelavel', side_effect=executar_direto), \
             patch('app.extrair_dados_comprovante', return_value={
                'valor': 10.0, 'data': date(2026, 1, 1), 'destinatario': 'X'}):
            for i in range(10):
                resp = self._post_share(self.client)
                self.assertNotEqual(resp.status_code, 429, f'Falhou na requisição {i+1}')
            resp = self._post_share(self.client)
            self.assertEqual(resp.status_code, 429)

    def test_constantes_no_app_py(self):
        """Verifica que as constantes de limite estão definidas exatamente no app.py."""
        with open('app.py', encoding='utf-8') as f:
            source = f.read()
        self.assertIn("SHARE_ACCOUNT_LIMIT='10 per 10 minutes'", source)
        self.assertIn("SHARE_IP_LIMIT='30 per hour'", source)
        self.assertIn('csrf.exempt(compartilhar_comprovante)', source)

    def test_limite_ip_compartilhado_entre_contas_e_instancias(self):
        from app import _share_target_rate_limit, share_limiter
        from limits.strategies import FixedWindowRateLimiter
        from flask import session
        share_limiter.storage.reset()
        other = FixedWindowRateLimiter(share_limiter.storage)
        for index in range(31):
            with app.test_request_context('/compartilhar-comprovante', method='POST',
                                          environ_base={'REMOTE_ADDR': '192.0.2.55'}):
                session['user'] = {'id': f'synthetic-{index // 10}'}
                with patch('app.share_limiter', other if index % 2 else share_limiter):
                    result = _share_target_rate_limit()
                if index < 30:
                    self.assertIsNone(result)
                else:
                    self.assertEqual(result[1], 429)
                    self.assertIn('IP', result[0].get_json()['error'])



class AnexoSeguroTest(unittest.TestCase):

    def setUp(self):
        self.uploads = tempfile.TemporaryDirectory()
        self.config_backup = {k: app.config[k] for k in ('TESTING', 'UPLOAD_FOLDER', 'WTF_CSRF_ENABLED')}
        app.config.update(TESTING=True, UPLOAD_FOLDER=self.uploads.name, WTF_CSRF_ENABLED=False)
        self.client = cliente_autenticado(app)
        with app.app_context():
            db.create_all()
        # Salva uma imagem PNG real na pasta de uploads do test
        buf = io.BytesIO()
        Image.new('RGB', (10, 10), 'blue').save(buf, format='PNG')
        self.png_bytes = buf.getvalue()
        self.filename = 'comprovante_teste_12345.png'
        Path(self.uploads.name, self.filename).write_bytes(self.png_bytes)

    def tearDown(self):
        with app.app_context():
            db.session.remove()
            db.drop_all()
        app.config.update(self.config_backup)
        self.uploads.cleanup()

    def _criar_pendencia_com_comprovante(self, url):
        with app.app_context():
            p = Pendencia(
                credor='Teste', valor=10.0,
                data_vencimento=date.today(),
                data_pagamento=date.today(),
                status='pago',
                comprovante_url=url
            )
            db.session.add(p)
            db.session.commit()
            return p.id

    def test_entrega_png_com_attachment_e_mime_correto(self):
        pid = self._criar_pendencia_com_comprovante(self.filename)
        response = self.client.get(f'/historico/{pid}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'image/png')
        self.assertIn('attachment', response.headers.get('Content-Disposition', ''))
        self.assertIn('no-store', response.headers.get('Cache-Control', ''))

    def test_comprovante_inexistente_redireciona(self):
        pid = self._criar_pendencia_com_comprovante('arquivo_inexistente.png')
        response = self.client.get(f'/historico/{pid}')
        self.assertIn(response.status_code, (302, 200))  # redirect ou flash

    def test_path_traversal_e_bloqueado(self):
        pid = self._criar_pendencia_com_comprovante('../app.py')
        response = self.client.get(f'/historico/{pid}')
        self.assertIn(response.status_code, (302, 404))
        self.assertNotIn(b'def ', response.data)  # nunca serve app.py



class AnexoSeguroTest(unittest.TestCase):

    def setUp(self):
        self.uploads = tempfile.TemporaryDirectory()
        self.config_backup = {k: app.config[k] for k in ('TESTING', 'UPLOAD_FOLDER', 'WTF_CSRF_ENABLED')}
        app.config.update(TESTING=True, UPLOAD_FOLDER=self.uploads.name, WTF_CSRF_ENABLED=False)
        self.client = cliente_autenticado(app)
        with app.app_context():
            db.create_all()
        # Salva uma imagem PNG real na pasta de uploads do test
        buf = io.BytesIO()
        Image.new('RGB', (10, 10), 'blue').save(buf, format='PNG')
        self.png_bytes = buf.getvalue()
        self.filename = 'comprovante_teste_12345.png'
        Path(self.uploads.name, self.filename).write_bytes(self.png_bytes)

    def tearDown(self):
        with app.app_context():
            db.session.remove()
            db.drop_all()
        app.config.update(self.config_backup)
        self.uploads.cleanup()

    def _criar_pendencia_com_comprovante(self, url):
        with app.app_context():
            p = Pendencia(
                credor='Teste', valor=10.0,
                data_vencimento=date.today(),
                data_pagamento=date.today(),
                status='pago',
                comprovante_url=url
            )
            db.session.add(p)
            db.session.commit()
            return p.id

    def test_entrega_png_com_attachment_e_mime_correto(self):
        pid = self._criar_pendencia_com_comprovante(self.filename)
        response = self.client.get(f'/historico/{pid}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'image/png')
        self.assertIn('attachment', response.headers.get('Content-Disposition', ''))
        self.assertEqual(response.headers.get('Cache-Control'), 'no-store')

    def test_comprovante_inexistente_redireciona(self):
        pid = self._criar_pendencia_com_comprovante('arquivo_inexistente.png')
        response = self.client.get(f'/historico/{pid}')
        self.assertIn(response.status_code, (302, 200))  # redirect ou flash

    def test_path_traversal_e_bloqueado(self):
        pid = self._criar_pendencia_com_comprovante('../app.py')
        response = self.client.get(f'/historico/{pid}')
        self.assertIn(response.status_code, (302, 404))
        self.assertNotIn(b'def ', response.data)  # nunca serve app.py
