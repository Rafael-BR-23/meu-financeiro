document.addEventListener('DOMContentLoaded', () => {
    const input = document.getElementById('comprovante_leitura');
    if (!input) return;
    const form = input.form;
    const status = document.getElementById('comprovante_status');
    form.addEventListener('submit', event => {
        if (form.getAttribute('aria-busy') === 'true') event.preventDefault();
    });
    input.addEventListener('change', async () => {
        const file = input.files[0];
        if (!file) return;
        if (form.elements.sem_anexo) form.elements.sem_anexo.checked = false;
        const body = new FormData();
        body.append('comprovante', file);
        const button = form.querySelector('[type=submit]');
        input.disabled = button.disabled = true;
        form.setAttribute('aria-busy', 'true');
        status.textContent = 'Lendo e anexando comprovante…';
        try {
            const response = await fetch('/compartilhar-comprovante', { method: 'POST', body });
            const data = new URL(response.url).searchParams;
            const attachment = data.get('comprovante_temp');
            if (!response.ok || !attachment) throw new Error('Arquivo não recebido');
            let temp = form.elements.namedItem('comprovante_temp');
            if (!temp) {
                temp = document.createElement('input');
                temp.type = 'hidden';
                temp.name = 'comprovante_temp';
                form.appendChild(temp);
            }
            temp.value = attachment;
            for (const name of ['credor', 'valor']) {
                if (!form.elements[name].value && data.get(name)) {
                    form.elements[name].value = data.get(name);
                    form.elements[name].dispatchEvent(new Event('change', { bubbles: true }));
                }
            }
            if (data.get('data')) form.elements.data_pagamento.value = data.get('data');
            if (form.elements.sem_anexo) form.elements.sem_anexo.checked = false;
            input.value = '';
            status.textContent = `Anexado: ${file.name}. Confira os dados antes de salvar.`;
        } catch (error) {
            status.textContent = 'Não foi possível ler o comprovante. O arquivo continua selecionado; confira a conexão e tente novamente.';
        } finally {
            input.disabled = button.disabled = false;
            form.removeAttribute('aria-busy');
        }
    });
});
