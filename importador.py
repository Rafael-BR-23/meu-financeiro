import os
import re
import pandas as pd
import pdfplumber
from datetime import datetime, date

import csv as _csv
from pathlib import Path as _Path
from PIL import Image as _Image


class DocumentValidationError(ValueError):
    """Levantada quando um documento não passa na validação de formato ou limites."""
    pass


# Mapeamento: extensão declarada → format(s) PIL aceitos
_PIL_FORMATS = {
    'png': ('PNG',),
    'jpg': ('JPEG',),
    'jpeg': ('JPEG',),
    'webp': ('WEBP',),
}


def identificar_documento(path: str, extensao: str, limites: dict) -> str:
    """
    Valida o arquivo em `path` contra a `extensao` declarada e os `limites`.
    Retorna o formato canônico: 'csv', 'pdf', 'png', 'jpg' ou 'webp'.
    Levanta DocumentValidationError com mensagem segura em caso de falha.
    """
    ext = extensao.lower().lstrip('.')
    file_size = _Path(path).stat().st_size
    max_bytes = limites.get('MAX_FILE_BYTES', 10 * 1024 * 1024)

    if file_size > max_bytes:
        raise DocumentValidationError('O arquivo excede o limite de 10 MB.')

    # --- CSV ---
    if ext == 'csv':
        with open(path, 'rb') as fh:
            raw = fh.read(min(file_size, 65536))
        if b'\x00' in raw:
            raise DocumentValidationError('CSV inválido.')
        for enc in ('utf-8', 'latin-1'):
            try:
                decoded = raw.decode(enc)
                break
            except UnicodeDecodeError:
                decoded = None
        if decoded is None:
            raise DocumentValidationError('CSV inválido.')
        try:
            reader = _csv.reader(decoded.splitlines())
            next(reader)  # deve ter pelo menos uma linha
        except (StopIteration, _csv.Error):
            raise DocumentValidationError('CSV inválido.') from None
        return 'csv'

    # --- PDF ---
    if ext == 'pdf':
        with open(path, 'rb') as fh:
            magic = fh.read(5)
        if magic != b'%PDF-':
            raise DocumentValidationError('O conteúdo não corresponde à extensão declarada.')
        try:
            import pdfplumber as _pdfplumber
            with _pdfplumber.open(path) as pdf:
                _ = len(pdf.pages)  # força leitura do índice
        except Exception:
            raise DocumentValidationError('PDF inválido.') from None
        return 'pdf'

    # --- Imagens ---
    if ext in _PIL_FORMATS:
        expected_formats = _PIL_FORMATS[ext]
        try:
            with _Image.open(path) as img:
                actual_format = img.format
                if actual_format not in expected_formats:
                    raise DocumentValidationError(
                        'O conteúdo não corresponde à extensão declarada.'
                    )
                width, height = img.size
                max_pixels = limites.get('MAX_IMAGE_PIXELS', 25_000_000)
                max_dim = limites.get('MAX_IMAGE_DIMENSION', 10_000)
                if width > max_dim or height > max_dim:
                    raise DocumentValidationError(
                        f'A imagem excede 10.000 px em uma dimensão.'
                    )
                if width * height > max_pixels:
                    raise DocumentValidationError(
                        'A imagem excede 25 milhões de pixels.'
                    )
                img.verify()
        except DocumentValidationError:
            raise
        except Exception:
            raise DocumentValidationError('Imagem inválida.') from None
        return 'jpg' if ext in ('jpg', 'jpeg') else ext

    raise DocumentValidationError('Extensão não suportada.')


def validar_csv(path: str, max_rows: int, max_columns: int) -> None:
    """
    Verifica limites de linhas e colunas em streaming — sem materializar a lista completa.
    Levanta DocumentValidationError se os limites forem excedidos.
    """
    for enc in ('utf-8', 'latin-1'):
        try:
            with open(path, 'r', encoding=enc, newline='') as fh:
                reader = _csv.reader(fh)
                for row_num, row in enumerate(reader, start=1):
                    if len(row) > max_columns:
                        raise DocumentValidationError(
                            f'O CSV excede 100 colunas.'
                        )
                    if row_num > max_rows:
                        raise DocumentValidationError(
                            f'O CSV excede 100.000 linhas.'
                        )
            return
        except DocumentValidationError:
            raise
        except UnicodeDecodeError:
            continue
    raise DocumentValidationError('CSV inválido.')


def validar_pdf(path: str, max_pages: int) -> None:
    """
    Verifica o número de páginas antes da extração de texto.
    Levanta DocumentValidationError se o limite for excedido.
    """
    import pdfplumber as _pdfplumber
    with _pdfplumber.open(path) as pdf:
        n = len(pdf.pages)
    if n > max_pages:
        raise DocumentValidationError(f'O PDF excede 25 páginas.')


def validar_imagem(path: str, max_pixels: int, max_dimension: int) -> None:
    """
    Verifica dimensões e pixel count antes do OCR.
    Levanta DocumentValidationError se os limites forem excedidos.
    """
    with _Image.open(path) as img:
        width, height = img.size
    if width > max_dimension or height > max_dimension:
        raise DocumentValidationError(
            f'A imagem excede 10.000 px em uma dimensão.'
        )
    if width * height > max_pixels:
        raise DocumentValidationError(
            'A imagem excede 25 milhões de pixels.'
        )


import multiprocessing as _multiprocessing
import time as _time


class ProcessingTimeout(DocumentValidationError):
    """Levantada quando o processamento excede o deadline monotonônico."""
    pass


def _worker_subprocess(func, args, conn):
    """Executa func(*args) e envia somente resultado completo ou erro sanitizado."""
    try:
        conn.send(('ok', func(*args)))
    except DocumentValidationError as exc:
        conn.send(('error', type(exc).__name__, str(exc)))
    except Exception:
        conn.send(('error', 'Exception', 'Não foi possível processar o documento.'))
    finally:
        conn.close()


def executar_cancelavel(func, args: tuple, deadline: float):
    """Executa uma callable pickleable até o deadline monotônico absoluto."""
    remaining = deadline - _time.monotonic()
    if remaining <= 0:
        raise ProcessingTimeout('O processamento excedeu 30 segundos.')

    ctx = _multiprocessing.get_context('spawn')
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    process = ctx.Process(target=_worker_subprocess, args=(func, args, child_conn))
    try:
        process.start()
        child_conn.close()
        # Drain the pipe before join: a large result can otherwise block send().
        if not parent_conn.poll(max(0.0, deadline - _time.monotonic())):
            raise ProcessingTimeout('O processamento excedeu 30 segundos.')
        try:
            message = parent_conn.recv()
        except EOFError:
            raise DocumentValidationError('Não foi possível processar o documento.') from None
        process.join(max(0.0, deadline - _time.monotonic()))
        if process.is_alive():
            raise ProcessingTimeout('O processamento excedeu 30 segundos.')
        if message[0] == 'ok':
            return message[1]
        if message[0] == 'error':
            if message[1] in ('DocumentValidationError', 'ProcessingTimeout'):
                raise DocumentValidationError(message[2])
            raise DocumentValidationError('Não foi possível processar o documento.')
        raise DocumentValidationError('Não foi possível processar o documento.')
    finally:
        parent_conn.close()
        child_conn.close()
        if process.is_alive():
            process.terminate()
            process.join()
        if process.pid is not None:
            process.close()


# Mapeamento de meses em português para números
MESES_MAP = {
    'jan': 1, 'janeiro': 1,
    'fev': 2, 'fevereiro': 2,
    'mar': 3, 'março': 3,
    'abr': 4, 'abril': 4,
    'mai': 5, 'maio': 5,
    'jun': 6, 'junho': 6,
    'jul': 7, 'julho': 7,
    'ago': 8, 'agosto': 8,
    'set': 9, 'setembro': 9,
    'out': 10, 'outubro': 10,
    'nov': 11, 'novembro': 11,
    'dez': 12, 'dezembro': 12
}

def parse_valor(valor_str):
    """Converte string de valor monetário (ex: -R$ 46,90 ou -46.90) para float."""
    if not valor_str:
        return 0.0
    # Remove símbolos comuns
    limpo = str(valor_str).replace('R$', '').replace(' ', '').strip()

    # Formato brasileiro: 1.234,56 → 1234.56 (vírgula = decimal, ponto = milhar)
    if ',' in limpo and '.' in limpo:
        limpo = limpo.replace('.', '').replace(',', '.')
    # Só vírgula como decimal (formato brasileiro: 1234,56)
    elif ',' in limpo:
        limpo = limpo.replace(',', '.')
    # Só ponto - verificar se é decimal (ex: 1412.90) ou separador de milhar (ex: 1.234,00)
    elif '.' in limpo:
        partes = limpo.split('.')
        # Se a última parte tem 2 dígitos e o restante tem até 3 dígitos antes do ponto, é decimal
        # Ex: 1412.90 (decimal) vs 1.234.567 (milhar)
        if len(partes) == 2 and len(partes[-1]) == 2:
            # 1412.90 ou 46.90 - ponto é decimal
            pass
        else:
            # 1.234 ou 1.234.567 - ponto é separador de milhar
            limpo = limpo.replace('.', '')

    try:
        return float(limpo)
    except ValueError:
        return 0.0

def converter_data_csv(data_str):
    """Converte string de data no formato DD/MM/YYYY para objeto date."""
    try:
        return datetime.strptime(data_str.strip(), '%d/%m/%Y').date()
    except Exception:
        try:
            return datetime.strptime(data_str.strip(), '%Y-%m-%d').date()
        except Exception:
            return date.today()

def extrair_fechamento_pdf(file_path):
    """
    Extrai o fechamento do mês do PDF do Nubank.
    Retorna dict com: ano, mes, saldo_inicial, saldo_final, total_entradas, total_saidas
    ou None se não conseguir extrair.
    """
    with pdfplumber.open(file_path) as pdf:
        primeira_pagina = pdf.pages[0].extract_text()
        if not primeira_pagina:
            return None

        linhas = primeira_pagina.split('\n')

        # Procura pelo período (ex: "01 DE JUNHO DE 2026 a 30 DE JUNHO DE 2026")
        periodo_match = None
        for linha in linhas:
            match = re.search(r'(\d{2})\s+DE\s+(\w+)\s+DE\s+(\d{4})\s+a\s+\d{2}\s+DE\s+(\w+)', linha, re.IGNORECASE)
            if match:
                mes_nome = match.group(2).lower()
                ano = int(match.group(3))
                mes = MESES_MAP.get(mes_nome)
                if mes:
                    periodo_match = {'ano': ano, 'mes': mes}
                    break

        if not periodo_match:
            return None

        # Procura pelos valores na primeira página
        saldo_inicial = None
        saldo_final = None
        total_entradas = None
        total_saidas = None

        for linha in linhas:
            linha_lower = linha.lower()

            if 'saldo inicial' in linha_lower:
                match = re.search(r'saldo inicial\s+([\d\.,]+)', linha_lower)
                if match:
                    saldo_inicial = parse_valor(match.group(1))

            if 'saldo final do período' in linha_lower and saldo_final is None:
                match = re.search(r'saldo final do período\s+(?:r\$\s*)?([\d\.,]+)', linha_lower)
                if match:
                    saldo_final = parse_valor(match.group(1))

            if 'total de entradas' in linha_lower:
                match = re.search(r'total de entradas\s*\+?([\d\.,]+)', linha_lower)
                if match:
                    total_entradas = parse_valor(match.group(1))

            if 'total de saídas' in linha_lower:
                match = re.search(r'total de saídas\s*-?([\d\.,]+)', linha_lower)
                if match:
                    total_saidas = abs(parse_valor(match.group(1)))

        if None in [saldo_inicial, saldo_final, total_entradas, total_saidas]:
            return None

        return {
            'ano': periodo_match['ano'],
            'mes': periodo_match['mes'],
            'saldo_inicial': saldo_inicial,
            'saldo_final': saldo_final,
            'total_entradas': total_entradas,
            'total_saidas': total_saidas
        }


def importar_csv(file_path):
    """
    Importa transações de um arquivo CSV do Nubank.
    Esperado colunas: Data, Valor, Identificador, Descrição
    """
    transacoes = []
    import csv

    # Tentar ler com encoding utf-8, se falhar usar latin-1
    for encoding in ['utf-8', 'latin-1']:
        try:
            with open(file_path, 'r', encoding=encoding) as f:
                reader = csv.reader(f)
                linhas = list(reader)
            break
        except UnicodeDecodeError:
            continue

    if not linhas or len(linhas) < 2:
        return transacoes

    # Primeira linha é cabeçalho
    headers = [h.strip().lower() for h in linhas[0]]

    # Mapear índices das colunas
    col_data = 0  # Data
    col_valor = 1  # Valor
    col_desc = 3   # Descrição (4ª coluna)

    for i, linha in enumerate(linhas[1:], start=2):
        if len(linha) < 4:
            continue

        try:
            data_str = linha[col_data].strip()
            valor_str = linha[col_valor].strip()
            desc_str = linha[col_desc].strip()

            data_val = converter_data_csv(data_str)
            valor_val = parse_valor(valor_str)
            tipo_val = 'saida' if valor_val < 0 else 'entrada'

            transacoes.append({
                'data': data_val,
                'valor': valor_val,
                'descricao': desc_str,
                'tipo': tipo_val
            })
        except Exception as e:
            print(f'Erro na linha {i}: {e}')
            continue

    return transacoes

def importar_pdf(file_path):
    """
    Importa transações de um PDF de extrato do Nubank usando pdfplumber.
    Extrai descrição completa (ex: Compra no débito FILIAL SHOPPING GRAND), data e valor correto (+/-).
    """
    transacoes = []
    ano_corrente = date.today().year

    regex_date = re.compile(r'^(\d{2})\s+([A-Z]{3}|\w+)\s+(\d{4})', re.IGNORECASE)
    regex_date_short = re.compile(r'^(\d{2})\s+([A-Z]{3})\b', re.IGNORECASE)
    regex_date_barra = re.compile(r'^(\d{2}/\d{2}(?:/\d{4})?)\b')

    PREFIXOS_NUBANK = [
        'compra no débito', 'compra no debito', 'compra no crédito', 'compra no credito',
        'transferência enviada', 'transferencia enviada', 'transferência recebida', 'transferencia recebida',
        'pagamento de boleto', 'pagamento de fatura', 'crédito em conta', 'credito em conta',
        'resgate rdb', 'aplicação rdb', 'aplicacao rdb', 'reembolso', 'estorno', 'recarga',
        'empréstimo', 'emprestimo', 'rendimento', 'depósito', 'deposito', 'pix enviado', 'pix recebido',
        'transferência recebida pelo pix', 'transferencia recebida pelo pix', 'transferência enviada pelo pix',
        'transferencia enviada pelo pix', 'transferência recebida', 'transferência enviada'
    ]

    current_date = None

    with pdfplumber.open(file_path) as pdf:
        for page in pdf.pages:
            texto = page.extract_text()
            if not texto:
                continue

            linhas = texto.split('\n')
            for linha in linhas:
                l_str = linha.strip()
                if not l_str:
                    continue

                # 1) Verifica se a linha contém/começa com data
                m_date = regex_date.match(l_str)
                if m_date:
                    dia = int(m_date.group(1))
                    mes_str = m_date.group(2).lower()
                    ano = int(m_date.group(3))
                    mes = MESES_MAP.get(mes_str)
                    if mes:
                        try:
                            current_date = date(ano, mes, dia)
                        except ValueError:
                            pass
                else:
                    m_date_short = regex_date_short.match(l_str)
                    if m_date_short:
                        dia = int(m_date_short.group(1))
                        mes_str = m_date_short.group(2).lower()
                        mes = MESES_MAP.get(mes_str)
                        if mes:
                            try:
                                current_date = date(ano_corrente, mes, dia)
                            except ValueError:
                                pass
                    else:
                        m_date_barra = regex_date_barra.match(l_str)
                        if m_date_barra:
                            data_str = m_date_barra.group(1)
                            if len(data_str) == 5:
                                data_str += f"/{ano_corrente}"
                            try:
                                current_date = datetime.strptime(data_str, '%d/%m/%Y').date()
                            except ValueError:
                                pass

                # 2) Procura por linha de transação terminando em valor monetário com vírgula decimal
                m_val = re.search(r'(\d{1,3}(?:\.\d{3})*,\d{2})$', l_str)
                if m_val and current_date:
                    val_str = m_val.group(1)
                    val_num = parse_valor(val_str)
                    desc = l_str[:m_val.start()].strip()
                    desc = re.sub(r'[\s\-]+$', '', desc).strip()

                    desc_lower = desc.lower()

                    # Valida se a descrição começa com um prefixo conhecido de transação
                    is_valid_prefix = any(desc_lower.startswith(prefix) for prefix in PREFIXOS_NUBANK)

                    if is_valid_prefix and val_num > 0:
                        is_entrada = any(k in desc_lower for k in [
                            'recebida', 'crédito em conta', 'credito em conta', 'resgate rdb', 
                            'reembolso', 'rendimento', 'estorno', 'depósito', 'deposito'
                        ])
                        valor_final = val_num if is_entrada else -val_num
                        tipo = 'entrada' if is_entrada else 'saida'

                        transacoes.append({
                            'data': current_date,
                            'valor': valor_final,
                            'descricao': desc,
                            'tipo': tipo
                        })

    return transacoes
