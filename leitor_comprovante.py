import logging
import re
import os
from datetime import datetime, date
import pdfplumber

from importador import DocumentValidationError

MESES_PT = {
    'jan': 1, 'fev': 2, 'mar': 3, 'abr': 4, 'mai': 5, 'jun': 6,
    'jul': 7, 'ago': 8, 'set': 9, 'out': 10, 'nov': 11, 'dez': 12
}

def parse_data_texto(texto):
    """Tenta converter datas em formatos numéricos (17/08/2026) ou textuais (17 AGO 2026)."""
    # 1. Formato numérico 17/08/2026
    m_num = re.search(r'(\d{2})[/.-](\d{2})[/.-](\d{4})', texto)
    if m_num:
        try:
            return datetime.strptime(f"{m_num.group(1)}/{m_num.group(2)}/{m_num.group(3)}", "%d/%m/%Y").date()
        except Exception:
            pass

    # 2. Formato Nubank textual: "17 AGO 2026"
    m_txt = re.search(r'(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})', texto, re.IGNORECASE)
    if m_txt:
        dia = int(m_txt.group(1))
        mes_str = m_txt.group(2).lower()
        ano = int(m_txt.group(3))
        if mes_str in MESES_PT:
            try:
                return date(ano, MESES_PT[mes_str], dia)
            except Exception:
                pass

    return None

def _limpar_nome_comprovante(candidato):
    """Descarta valores e rótulos capturados como nome pelo PDF/OCR."""
    candidato = candidato.strip()
    if ('r$' in candidato.lower()
            or re.fullmatch(r'[\d.,]+(?:\s*reais)?', candidato, re.IGNORECASE)
            or not any(char.isalpha() for char in candidato)
            or parse_data_texto(candidato)
            or re.match(r'^(?:(?:Valor|Total|Pago|Transferido|Quantia)(?:$|[ \t]*:|[ \t]+\d)|(?:Pagador|Origem)\b)', candidato, re.IGNORECASE)
            or candidato.lower().rstrip(':') in {'nome', 'destino', 'para', 'conta'}
            or 'ajudar' in candidato.lower()):
        return ''
    candidato = re.sub(r'(CPF|CNPJ|Chave|Institui[çc][ãa]o|Origem|Banco|Ag[êe]ncia|C[óo]digo|NSU|Autentica[çc][ãa]o).*',
                       '', candidato, flags=re.IGNORECASE).strip()
    return candidato[:60] if len(candidato) > 2 else ''

def processar_texto_comprovante(texto, dados):
    """Aplica regras avançadas de parsing para extratos/comprovantes Nubank e outros."""
    if not texto:
        return dados

    texto_clean = re.sub(r'[ \t]+', ' ', texto)
    linhas = [l.strip() for l in texto_clean.split('\n') if l.strip()]
    texto_lower = texto_clean.lower()

    # 1. Extração do Valor
    # Ex: "Valor R$ 85,32" ou "R$ 441,22" ou "Valor: R$ 150,00"
    match_valor = re.search(r'(?:valor|total|pago|transferido|quantia)[^\d\n\r]*R\$\s*([\d\.,]+)', texto_clean, re.IGNORECASE)
    if not match_valor:
        # Se não achou com prefixo, procura o primeiro R$
        match_valor = re.search(r'R\$\s*([\d\.,]+)', texto_clean)
    
    if match_valor:
        v_str = match_valor.group(1).replace('.', '').replace(',', '.')
        try:
            dados['valor'] = float(v_str)
        except ValueError:
            pass

    # 2. Extração da Data
    # Prioridade para data de pagamento ou data do cabeçalho
    # Nubank comprovante: "17 AGO 2026 - 08:48:26" ou "Agendado para 17 AGO 2026"
    dt = parse_data_texto(texto_clean)
    if dt:
        dados['data'] = dt

    # 3. Nome na própria linha do rótulo ou no bloco seguinte (PDF/OCR).
    rotulo_destino = (r'Estabelecimento|Favorecido|Destinat[áa]rio|Destino|'
                      r'Nome do benefici[áa]rio|Benefici[áa]rio|Nome do recebedor|Recebedor|Para')
    for i, linha in enumerate(linhas):
        match_dest = re.match(rf'^(?:{rotulo_destino})\b[ \t:]*([^\r\n]*)', linha, re.IGNORECASE)
        if not match_dest:
            continue
        candidato = match_dest.group(1).strip()
        seguinte = i + 1
        if not candidato and seguinte < len(linhas):
            candidato = linhas[seguinte]
            seguinte += 1
        if candidato.lower().rstrip(':') == 'nome' and seguinte < len(linhas):
            candidato = linhas[seguinte]
        candidato = re.sub(r'^Nome[ \t:]+', '', candidato, flags=re.IGNORECASE)
        nome = _limpar_nome_comprovante(candidato)
        if nome:
            dados['destinatario'] = nome
            break

    # Pagamento de fatura identifica o documento, sem um destinatário.
    match_documento = re.search(r'(?im)^[ \t]*Documento[:\s]+([^\n\r]+)', texto_clean)
    if match_documento:
        documento = _limpar_nome_comprovante(match_documento.group(1))
        if documento:
            dados['descricao'] = documento

    # 4. Tipo de Operação
    if 'pix' in texto_lower:
        dados['tipo'] = 'Pix'
    elif 'boleto' in texto_lower or 'código de barras' in texto_lower or 'codigo de barras' in texto_lower:
        dados['tipo'] = 'Boleto'
    elif 'transferência' in texto_lower or 'transferencia' in texto_lower:
        dados['tipo'] = 'Transferência'
    elif 'comprovante de pagamento' in texto_lower or 'comprovante de agendamento' in texto_lower:
        dados['tipo'] = 'Pagamento'

    return dados

def extrair_dados_comprovante(caminho_arquivo):
    """
    Analisa um arquivo de comprovante (PDF ou imagem PNG/JPG) e extrai:
    - valor: float ou None
    - data: date ou None
    - destinatario: str ou None
    - tipo: 'Pix', 'Boleto', 'Pagamento', etc.
    """
    ext = os.path.splitext(caminho_arquivo)[1].lower()
    
    dados = {
        'valor': None,
        'data': date.today(),
        'destinatario': '',
        'descricao': '',
        'tipo': 'comprovante',
        'arquivo_nome': os.path.basename(caminho_arquivo)
    }

    if ext == '.pdf':
        return _extrair_dados_pdf(caminho_arquivo, dados)
    elif ext in ['.jpg', '.jpeg', '.png', '.webp']:
        return _extrair_dados_imagem(caminho_arquivo, dados)
    
    return dados

def _extrair_dados_pdf(caminho_arquivo, dados):
    texto_completo = ""
    try:
        with pdfplumber.open(caminho_arquivo) as pdf:
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    texto_completo += t + "\n"
    except Exception:
        logging.getLogger(__name__).exception("Falha ao ler comprovante PDF")
        raise DocumentValidationError('Não foi possível processar o documento.') from None

    return processar_texto_comprovante(texto_completo, dados)

def _extrair_dados_imagem(caminho_arquivo, dados):
    """Realiza OCR na imagem do comprovante via Tesseract."""
    try:
        from PIL import Image
        import pytesseract

        img = Image.open(caminho_arquivo)
        
        # Converte para tons de cinza para melhorar OCR
        img_gray = img.convert('L')
        
        # Tenta com português; se falhar, tenta sem idioma específico
        try:
            texto = pytesseract.image_to_string(img_gray, lang='por')
        except Exception:
            texto = pytesseract.image_to_string(img_gray)

        if texto:
            dados = processar_texto_comprovante(texto, dados)
            dados['tipo'] = dados.get('tipo') or 'Comprovante Imagem'
    except Exception:
        logging.getLogger(__name__).exception("Falha no OCR do comprovante")
        raise DocumentValidationError('Não foi possível processar o documento.') from None

    return dados
