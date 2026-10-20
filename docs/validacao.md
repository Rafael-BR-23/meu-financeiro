# Validação

Os testes usam bancos SQLite em memória e diretórios temporários. Execute a descoberta a partir da raiz com `-t .`, garantindo a configuração de isolamento antes de importar o aplicativo.

```powershell
.\venv\Scripts\python.exe -m unittest discover -s tests -t . -q
node tests/test_currency.cjs
node tests/test_interface.cjs
node tests/test_pwa_share.cjs
.\venv\Scripts\python.exe -m pip check
.\venv\Scripts\python.exe -m pip_audit --no-deps --disable-pip -r requirements-lock.txt
```

Para auditar dependências, instale `pip-audit` em um ambiente de desenvolvimento. Em Linux/macOS, use o Python do ambiente virtual em `.venv/bin/python`.

Antes de uma atualização, preserve um backup consistente dos bancos e anexos, valide a configuração de produção e confirme login, isolamento de contas, formulários, comprovantes, HTTPS e cookies seguros. Testes automatizados não substituem a verificação de instalação do PWA e compartilhamento no dispositivo real.

Uma vulnerabilidade sem correção imediata precisa de registro de Impacto:, Mitigação: e Decisão:, com responsável e prazo. Não registre segredos, tokens ou dados financeiros nos relatórios.
