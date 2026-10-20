"""Testes sintéticos de segurança de documentos — sem leitura de arquivos reais do repositório."""
import io
import os
import time
import tempfile
import unittest
import unittest.mock
from datetime import date
from pathlib import Path
from PIL import Image

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DATABASE_URL', 'sqlite:///:memory:')
os.environ.setdefault('AUTH_DATABASE_URL', 'sqlite:///:memory:')
os.environ.setdefault('AUTH_PROVIDER', 'local')

from importador import (DocumentValidationError, ProcessingTimeout, executar_cancelavel,
                        identificar_documento, validar_csv, validar_pdf, validar_imagem)

LIMITES = {
    'MAX_FILE_BYTES': 10 * 1024 * 1024,
    'MAX_PDF_PAGES': 25,
    'MAX_IMAGE_PIXELS': 25_000_000,
    'MAX_IMAGE_DIMENSION': 10_000,
    'MAX_CSV_ROWS': 100_000,
    'MAX_CSV_COLUMNS': 100,
}


class DocumentoSeguroTest(unittest.TestCase):

    # --- Task 1: identificar_documento ---

    def test_rejeita_extensao_enganosa_e_aceita_imagem_real(self):
        with tempfile.TemporaryDirectory() as folder:
            fake = Path(folder, 'falso.pdf')
            fake.write_bytes(b'nao-e-pdf')
            with self.assertRaisesRegex(DocumentValidationError, 'conteúdo não corresponde'):
                identificar_documento(str(fake), 'pdf', LIMITES)
            image = Path(folder, 'foto.png')
            Image.new('RGB', (20, 20), 'white').save(image)
            self.assertEqual(identificar_documento(str(image), 'png', LIMITES), 'png')

    def test_rejeita_pdf_malformado(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'quebrado.pdf')
            path.write_bytes(b'%PDF-\nquebrado')
            with self.assertRaisesRegex(DocumentValidationError, 'PDF inválido'):
                identificar_documento(str(path), 'pdf', LIMITES)

    def test_rejeita_arquivo_acima_de_10mb(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'grande.csv')
            path.write_bytes(b'x' * (10 * 1024 * 1024 + 1))
            with self.assertRaisesRegex(DocumentValidationError, '10 MB'):
                identificar_documento(str(path), 'csv', LIMITES)

    def test_aceita_arquivo_exatamente_10mb(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'exato.csv')
            # CSV mínimo válido com padding para chegar em 10MB exato
            header = b'Data,Valor,Identificador,Descricao\n'
            row = b'01/01/2026,-1.00,ID1,Teste\n'
            padding = b'# ' + b'x' * (10 * 1024 * 1024 - len(header) - len(row) - 3) + b'\n'
            path.write_bytes(header + row + padding)
            # Não deve levantar; deve retornar 'csv'
            result = identificar_documento(str(path), 'csv', LIMITES)
            self.assertEqual(result, 'csv')

    def test_rejeita_csv_com_bytes_nulos(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'nulo.csv')
            path.write_bytes(b'Data,Valor\n01/01/2026\x001.00\n')
            with self.assertRaisesRegex(DocumentValidationError, 'CSV inválido'):
                identificar_documento(str(path), 'csv', LIMITES)

    def test_rejeita_imagem_com_extensao_errada(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'foto.png')
            Image.new('RGB', (10, 10), 'red').save(path, format='JPEG')
            # Conteúdo é JPEG mas extensão declarada é png
            with self.assertRaisesRegex(DocumentValidationError, 'conteúdo não corresponde'):
                identificar_documento(str(path), 'png', LIMITES)

    # --- Task 2: validar_csv, validar_pdf, validar_imagem ---

    def test_csv_limita_linhas_e_colunas_sem_lista_completa(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'extrato.csv')
            path.write_text(
                'Data,Valor,Id,Descricao\n01/01/2026,1,x,a\n02/01/2026,2,y,b\n',
                encoding='utf-8'
            )
            with self.assertRaisesRegex(DocumentValidationError, '100.000 linhas'):
                validar_csv(str(path), max_rows=1, max_columns=100)
            path.write_text(','.join(str(i) for i in range(101)), encoding='utf-8')
            with self.assertRaisesRegex(DocumentValidationError, '100 colunas'):
                validar_csv(str(path), max_rows=100_000, max_columns=100)

    def test_pdf_e_imagem_respeitam_complexidade(self):
        fake_pdf = unittest.mock.MagicMock()
        fake_pdf.__enter__ = unittest.mock.Mock(return_value=fake_pdf)
        fake_pdf.__exit__ = unittest.mock.Mock(return_value=False)
        fake_pdf.pages = [object()] * 26
        with unittest.mock.patch('importador.pdfplumber.open', return_value=fake_pdf):
            with self.assertRaisesRegex(DocumentValidationError, '25 páginas'):
                validar_pdf('sintetico.pdf', 25)
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder, 'grande.png')
            Image.new('RGB', (10001, 1), 'white').save(image)
            with self.assertRaisesRegex(DocumentValidationError, '10.000 px'):
                validar_imagem(str(image), 25_000_000, 10_000)

    def test_imagem_excede_pixels_totais(self):
        with tempfile.TemporaryDirectory() as folder:
            # 5001x5001 = 25.010.001 pixels > 25M
            image = Path(folder, 'enorme.png')
            Image.new('RGB', (5001, 5001), 'white').save(image)
            with self.assertRaisesRegex(DocumentValidationError, '25 milh'):
                validar_imagem(str(image), 25_000_000, 10_000)

    def test_imagem_valida_passa(self):
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder, 'ok.png')
            Image.new('RGB', (100, 100), 'blue').save(image)
            validar_imagem(str(image), 25_000_000, 10_000)  # não deve levantar

    def test_csv_valido_passa(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'ok.csv')
            path.write_text('Data,Valor\n01/01/2026,100.00\n', encoding='utf-8')
            validar_csv(str(path), max_rows=100_000, max_columns=100)  # não deve levantar
    def test_falha_na_extracao_nao_retorna_dados_parciais(self):
        from leitor_comprovante import extrair_dados_comprovante

        with unittest.mock.patch('leitor_comprovante.pdfplumber.open',
                                 side_effect=RuntimeError('detalhe interno')):
            with self.assertRaisesRegex(DocumentValidationError,
                                        'Não foi possível processar o documento'):
                extrair_dados_comprovante('sintetico.pdf')

    def test_importacao_timeout_nao_persiste_e_remove_temporario(self):
        from app import app, db
        from models import ExtratoNubank
        from tests import cliente_autenticado

        transaction = dict(data=date(2026, 1, 1), descricao='Parcial', valor=-1, tipo='saida')
        with tempfile.TemporaryDirectory() as folder:
            previous = {key: app.config[key] for key in
                        ('TESTING', 'WTF_CSRF_ENABLED', 'UPLOAD_FOLDER')}
            app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, UPLOAD_FOLDER=folder)
            client = cliente_autenticado(app)
            with app.app_context():
                db.create_all()
            try:
                with unittest.mock.patch('app.executar_cancelavel',
                                         side_effect=ProcessingTimeout('O processamento excedeu 30 segundos.')):
                    response = client.post('/importar', data={
                        'extrato': (io.BytesIO(b'Data,Valor,Id,Descricao\n01/01/2026,-1,x,Parcial\n'),
                                    'teste.csv')}, follow_redirects=True)
                self.assertIn('O processamento excedeu 30 segundos.', response.get_data(as_text=True))
                with app.app_context():
                    self.assertEqual(ExtratoNubank.query.count(), 0)
                self.assertEqual(list(Path(folder).iterdir()), [])
            finally:
                with app.app_context():
                    db.session.remove()
                    db.drop_all()
                app.config.update(previous)


# ---- Helpers de topo de módulo para multiprocessing (devem ser pickleable) ----

def _bloquear(event):
    """Bloqueia por até 60s; usado nos testes de timeout."""
    event.set()
    time.sleep(60)


def _retornar_valor(x):
    """Retorna x imediatamente; usado para verificar resultado válido."""
    return x * 2


def _esperar(segundos):
    """Espera o tempo indicado; usado para provar o deadline absoluto compartilhado."""
    time.sleep(segundos)
    return segundos


class TimeoutCancelaveTest(unittest.TestCase):

    def test_timeout_termina_subprocesso(self):
        from importador import ProcessingTimeout, executar_cancelavel
        import multiprocessing
        ctx = multiprocessing.get_context('spawn')
        event = ctx.Event()
        previous = {process.pid for process in multiprocessing.active_children()}
        with self.assertRaisesRegex(ProcessingTimeout, '30 segundos'):
            executar_cancelavel(_bloquear, (event,), time.monotonic() + 5)
        self.assertTrue(event.is_set(), 'Worker must start before cancellation is tested')
        self.assertFalse([process for process in multiprocessing.active_children() if process.pid not in previous])

    def test_resultado_valido_retornado(self):
        from importador import executar_cancelavel
        resultado = executar_cancelavel(_retornar_valor, (21,), time.monotonic() + 10.0)
        self.assertEqual(resultado, 42)
        grande = executar_cancelavel(_retornar_valor, ('x' * 100_000,), time.monotonic() + 10.0)
        self.assertEqual(len(grande), 200_000)

    def test_deadline_compartilhada_entre_estagios(self):
        """O segundo estágio recebe apenas o orçamento restante."""
        # O orçamento inclui o custo do spawn, que no Windows é muito maior que 30 ms.
        deadline = time.monotonic() + 8.0
        self.assertEqual(executar_cancelavel(_esperar, (0.02,), deadline), 0.02)
        restante = max(0.02, deadline - time.monotonic() + 0.02)
        with self.assertRaises(ProcessingTimeout):
            executar_cancelavel(_esperar, (restante,), deadline)


if __name__ == '__main__':
    unittest.main()
