import unittest

from leitor_comprovante import processar_texto_comprovante


class LeitorComprovanteTest(unittest.TestCase):
    def extrair(self, texto):
        return processar_texto_comprovante(texto, {'destinatario': '', 'descricao': ''})

    def test_valor_nao_e_destinatario(self):
        for valor in ('R$ 85,32', '85,32', '1.234,56 reais'):
            with self.subTest(valor=valor):
                dados = self.extrair('Comprovante Pix\nDestinatário\n' + valor + '\nValor R$ 85,32')
                self.assertEqual(dados['destinatario'], '')
                self.assertEqual(dados['valor'], 85.32)

    def test_continua_procurando_o_nome_apos_candidato_invalido(self):
        dados = self.extrair('Pix\nDestinatário R$ 85,32\nFavorecido: Loja Exemplo\nValor R$ 85,32')
        self.assertEqual(dados['destinatario'], 'Loja Exemplo')

    def test_nome_no_bloco_destino(self):
        for bloco in ('Destino\nNome\nPessoa Exemplo', 'Destinatário:\nPessoa Exemplo',
                      'Nome do beneficiário: Loja 123', 'Para: Pessoa Exemplo'):
            with self.subTest(bloco=bloco):
                dados = self.extrair('Comprovante Pix\nValor R$ 85,32\n' + bloco + '\nCPF 000.000.000-00')
                self.assertEqual(dados['destinatario'], 'Loja 123' if 'Loja' in bloco else 'Pessoa Exemplo')

    def test_preserva_empresa_com_total_no_nome(self):
        self.assertEqual(self.extrair('Favorecido: Total Express')['destinatario'], 'Total Express')

    def test_documento_nao_preenche_descricao_com_valor(self):
        for valor in ('R$ 85,32', '85,32'):
            with self.subTest(valor=valor):
                dados = self.extrair('Documento\n' + valor + '\nValor R$ 85,32')
                self.assertEqual(dados['descricao'], '')

    def test_nao_usa_data_ou_rotulo_como_nome(self):
        for linha in ('Valor', '20 OUT 2026', 'Nome', 'CPF 000.000.000-00'):
            with self.subTest(linha=linha):
                self.assertEqual(self.extrair('Para\n' + linha)['destinatario'], '')
