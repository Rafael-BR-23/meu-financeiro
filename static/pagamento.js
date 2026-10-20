// Shared payment form for the dashboard and accounts page.
const paymentModal = document.getElementById('modalPagar');
paymentModal.addEventListener('show.bs.modal', event => {
    const button = event.relatedTarget;
    const today = new Date();
    const localDate = date => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
    document.getElementById('modalCredor').textContent = button.dataset.credor;
    document.getElementById('modalValor').textContent = Number(button.dataset.valor).toLocaleString('pt-BR', {style: 'currency', currency: 'BRL'});
    document.getElementById('formPagar').action = `/pendencias/pagar/${button.dataset.id}`;
    document.getElementById('data_pagamento').value = localDate(today);
    document.getElementById('comprovante').value = '';
    document.getElementById('repetirProximo').checked = false;
    document.getElementById('proximoMesFields').style.display = 'none';
    const nextMonth = new Date(today.getFullYear(), today.getMonth() + 2, 0);
    nextMonth.setDate(Math.min(today.getDate(), nextMonth.getDate()));
    document.getElementById('proximo_vencimento').value = localDate(nextMonth);
    document.getElementById('proximo_valor').value = Number(button.dataset.valor).toFixed(2);
    document.getElementById('proximo_valor').dispatchEvent(new Event('change', { bubbles: true }));
});
document.getElementById('repetirProximo').addEventListener('change', function () {
    document.getElementById('proximoMesFields').style.display = this.checked ? 'block' : 'none';
});
